"""Run watchdog thread (ADR-0007): kills a run whose heartbeat is stale or wall budget is spent."""

import threading
import time
from collections.abc import Callable
from types import TracebackType


class Watchdog:
    def __init__(
        self,
        heartbeat_timeout_s: float,
        wall_budget_s: float,
        on_kill: Callable[[str], None],
        poll_interval_s: float = 0.05,
    ) -> None:
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._wall_budget_s = wall_budget_s
        self._on_kill = on_kill
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started_at = 0.0
        self._last_beat = 0.0
        self._kill_reason: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def kill_reason(self) -> str | None:
        with self._lock:
            return self._kill_reason

    def start(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._started_at = now
            self._last_beat = now
        self._thread = threading.Thread(target=self._run, name="bourse-watchdog", daemon=True)
        self._thread.start()

    def heartbeat(self) -> None:
        with self._lock:
            self._last_beat = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def __enter__(self) -> "Watchdog":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.wait(self._poll_interval_s):
            now = time.monotonic()
            with self._lock:
                elapsed = now - self._started_at
                stale = now - self._last_beat
            if elapsed > self._wall_budget_s:
                reason = f"wall budget exceeded: {elapsed:.2f}s > {self._wall_budget_s:.2f}s"
            elif stale > self._heartbeat_timeout_s:
                reason = f"heartbeat stale: {stale:.2f}s > {self._heartbeat_timeout_s:.2f}s"
            else:
                continue
            with self._lock:
                self._kill_reason = reason
            self._on_kill(reason)
            return
