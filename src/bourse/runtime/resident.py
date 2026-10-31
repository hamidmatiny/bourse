"""Resident memory guard (ADR-0033).

Samples Ollama /api/ps (sum of `size` over loaded models) every interval during runs, writes
one JSON line per sample, and tracks the peak. A total above the limit continuously for at
least `sustain_s` calls `on_trip` once; callers trip the run breaker with it, so the run is
ABORTED with zero further delegations (the breaker path, ADR-0026). A shorter spike only
raises the peak.
"""

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import anyio
import httpx

RESIDENT_LIMIT_BYTES = 26_000_000_000
METRICS_DIR = Path("data/metrics")


class ResidentMonitor:
    def __init__(
        self,
        sample: Callable[[], Awaitable[int]],
        on_trip: Callable[[str], None],
        *,
        limit_bytes: int = RESIDENT_LIMIT_BYTES,
        sustain_s: float = 30.0,
        interval_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        out: Path | None = None,
    ) -> None:
        self.sample = sample
        self.on_trip = on_trip
        self.limit_bytes = limit_bytes
        self.sustain_s = sustain_s
        self.interval_s = interval_s
        self.clock = clock
        self.out = out
        self.peak = 0
        self.samples = 0
        self.tripped = False
        self._over_since: float | None = None

    def observe(self, now: float, total: int) -> None:
        self.samples += 1
        self.peak = max(self.peak, total)
        if total <= self.limit_bytes:
            self._over_since = None
            return
        if self._over_since is None:
            self._over_since = now
        over_for = now - self._over_since
        if over_for >= self.sustain_s and not self.tripped:
            self.tripped = True
            self.on_trip(f"resident memory {total} bytes > {self.limit_bytes} for {over_for:.0f}s")

    async def run(self) -> None:
        while True:
            try:
                total = await self.sample()
            except (httpx.HTTPError, ValueError, KeyError):
                total = None
            now = self.clock()
            if total is not None:
                self.observe(now, total)
                if self.out is not None:
                    line = {"at": datetime.now(UTC).isoformat(), "resident_bytes": total}
                    async with await anyio.open_file(self.out, "a") as fh:
                        await fh.write(json.dumps(line) + "\n")
            await anyio.sleep(self.interval_s)

    def summary(self) -> str:
        return f"resident peak {self.peak} (limit {self.limit_bytes})"


def ollama_sampler(client: httpx.AsyncClient, base_url: str) -> Callable[[], Awaitable[int]]:
    async def sample() -> int:
        resp = await client.get(f"{base_url.rstrip('/')}/api/ps", timeout=5.0)
        resp.raise_for_status()
        return sum(int(m.get("size") or 0) for m in resp.json().get("models") or [])

    return sample


@asynccontextmanager
async def monitoring(monitor: ResidentMonitor) -> AsyncIterator[ResidentMonitor]:
    """Run the sampler in the background for the duration of the block."""
    if monitor.out is not None:
        await anyio.Path(monitor.out.parent).mkdir(parents=True, exist_ok=True)
    async with anyio.create_task_group() as tg:
        tg.start_soon(monitor.run)
        try:
            yield monitor
        finally:
            tg.cancel_scope.cancel()


def metrics_path() -> Path:
    return METRICS_DIR / f"resident-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
