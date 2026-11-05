"""Resident memory guard (ADR-0033, ADR-0044).

Samples Ollama /api/ps (sum of `size` over loaded models) every interval during runs, writes
one JSON line per sample, and tracks the peak. A total above the limit continuously for at
least `sustain_s` calls `on_trip` once; callers trip the run breaker with it, so the run is
ABORTED with zero further delegations (the breaker path, ADR-0026). A shorter spike only
raises the peak.

Each model appearance after the monitor starts is classified: INITIAL_LOAD (first sample;
baseline only — not a reload), KEEP_ALIVE_EXPIRY (reappear after unload), PARAM_CHANGE
(size change), EVICTION (load that displaced another resident model). Only KEEP_ALIVE_EXPIRY,
PARAM_CHANGE, and EVICTION increment `reloads` (ADR-0044 / ADR-0042 exposure fix).
"""

import json
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import anyio
import httpx

# AGENTS.md / ADR-0019: hard cap is 26e9 bytes (26.00 GB ≈ 24.21 GiB), not 26 GiB.
RESIDENT_LIMIT_BYTES = 26_000_000_000
METRICS_DIR = Path("data/metrics")


def format_bytes_units(n: int) -> str:
    """Decimal GB and binary GiB for the same byte count."""
    return f"{n / 1e9:.2f} GB / {n / (1024**3):.2f} GiB"


def format_limit(limit_bytes: int = RESIDENT_LIMIT_BYTES) -> str:
    """Human limit line: 'limit 26.00 GB / 24.21 GiB'."""
    return f"limit {format_bytes_units(limit_bytes)}"


def format_resident_peak(peak_bytes: int, limit_bytes: int = RESIDENT_LIMIT_BYTES) -> str:
    return (
        f"resident peak {peak_bytes} bytes ({format_bytes_units(peak_bytes)}) "
        f"({format_limit(limit_bytes)})"
    )


class ReloadKind(StrEnum):
    INITIAL_LOAD = "INITIAL_LOAD"
    KEEP_ALIVE_EXPIRY = "KEEP_ALIVE_EXPIRY"
    PARAM_CHANGE = "PARAM_CHANGE"
    EVICTION = "EVICTION"


@dataclass(frozen=True)
class LoadedModel:
    name: str
    size: int
    runner: str = ""
    context_length: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "size": self.size,
            "runner": self.runner,
            "context_length": self.context_length,
        }


def unexpected_loaded_models(
    models: Sequence[LoadedModel] | Sequence[dict[str, object]],
    allowed: frozenset[str],
) -> list[tuple[str, int]]:
    """Return (name, size) for /api/ps entries whose name is not in the roster set."""
    out: list[tuple[str, int]] = []
    for m in models:
        if isinstance(m, LoadedModel):
            name, size = m.name, int(m.size)
        else:
            name, size = str(m.get("name") or ""), int(m.get("size") or 0)
        if name and name not in allowed:
            out.append((name, size))
    return out


def warn_unexpected_loaded_models(
    models: Sequence[LoadedModel] | Sequence[dict[str, object]],
    allowed: frozenset[str],
    *,
    echo: Callable[[str], None] = print,
) -> list[tuple[str, int]]:
    """ADR-0047: warn if /api/ps has models outside the honest roster."""
    unexpected = unexpected_loaded_models(models, allowed)
    if unexpected:
        parts = ", ".join(f"{n}={s}" for n, s in unexpected)
        echo(
            f"WARNING: /api/ps has models outside roster {sorted(allowed)}: {parts}"
        )
    return unexpected


@dataclass(frozen=True)
class ReloadEvent:
    kind: ReloadKind
    name: str


class ResidentMonitor:
    """`sample` returns the resident total, or the loaded models (then reloads are counted:
    a model that appears after the first sample, or whose size changes, is one reload).
    First-sample INITIAL_LOAD is recorded for diagnostics but does not increment `reloads`.
    """

    def __init__(
        self,
        sample: Callable[[], Awaitable[int | Sequence[LoadedModel]]],
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
        self.reloads = 0
        self._over_since: float | None = None
        self._models: dict[str, int] | None = None
        self.events: list[ReloadEvent] = []
        self.last_models: tuple[LoadedModel, ...] = ()

    def observe_models(self, models: Sequence[LoadedModel]) -> None:
        current = {m.name: m.size for m in models}
        self.last_models = tuple(models)
        if self._models is None:
            for name in current:
                self._record(ReloadKind.INITIAL_LOAD, name, counts_as_reload=False)
            self._models = current
            return
        disappeared = set(self._models) - set(current)
        for name, size in current.items():
            if name in self._models and self._models[name] != size:
                self._record(ReloadKind.PARAM_CHANGE, name)
            elif name not in self._models:
                kind = ReloadKind.EVICTION if disappeared else ReloadKind.KEEP_ALIVE_EXPIRY
                self._record(kind, name)
        self._models = current

    def _record(
        self, kind: ReloadKind, name: str, *, counts_as_reload: bool = True
    ) -> None:
        self.events.append(ReloadEvent(kind, name))
        if counts_as_reload:
            self.reloads += 1

    def counts_by_kind(self) -> dict[str, int]:
        counts: Counter[str] = Counter(e.kind.value for e in self.events)
        return {k.value: counts.get(k.value, 0) for k in ReloadKind}

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
            from bourse.broker.abort import resident_limit_reason

            self.on_trip(
                resident_limit_reason(
                    peak_bytes=total, over_s=over_for, limit_bytes=self.limit_bytes
                )
            )

    def _sample_line(
        self, total: int, models: Sequence[LoadedModel] | None
    ) -> dict[str, object]:
        line: dict[str, object] = {
            "at": datetime.now(UTC).isoformat(),
            "resident_bytes": total,
        }
        if models is not None:
            line["models"] = [m.as_dict() for m in models]
        return line

    async def run(self) -> None:
        while True:
            total: int | None
            models: Sequence[LoadedModel] | None = None
            try:
                sampled = await self.sample()
            except (httpx.HTTPError, ValueError, KeyError):
                sampled = None
            if sampled is None:
                total = None
            elif isinstance(sampled, int):
                total = sampled
            else:
                models = sampled
                self.observe_models(sampled)
                total = sum(m.size for m in sampled)
            now = self.clock()
            if total is not None:
                self.observe(now, total)
                if self.out is not None:
                    async with await anyio.open_file(self.out, "a") as fh:
                        await fh.write(json.dumps(self._sample_line(total, models)) + "\n")
            await anyio.sleep(self.interval_s)

    def summary(self) -> str:
        return format_resident_peak(self.peak, self.limit_bytes)

    def reload_summary(self) -> str:
        parts = " ".join(f"{k}={n}" for k, n in self.counts_by_kind().items())
        return f"model reloads {self.reloads} ({parts})"


def ollama_sampler(
    client: httpx.AsyncClient, ps_url: str, key: str = ""
) -> Callable[[], Awaitable[Sequence[LoadedModel]]]:
    """GET <ps_url> (Ollama /api/ps, or the meter's passthrough in compose with a key)."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    async def sample() -> Sequence[LoadedModel]:
        resp = await client.get(ps_url, timeout=5.0, headers=headers)
        resp.raise_for_status()
        out: list[LoadedModel] = []
        for m in resp.json().get("models") or []:
            ctx = m.get("context_length")
            out.append(
                LoadedModel(
                    name=str(m.get("name") or m.get("model") or ""),
                    size=int(m.get("size") or 0),
                    runner=str(m.get("runner") or (m.get("details") or {}).get("runner") or ""),
                    context_length=int(ctx) if ctx is not None else None,
                )
            )
        return out

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
