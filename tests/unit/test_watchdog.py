import threading
import time

from bourse.runtime.watchdog import Watchdog


class KillRecorder:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.reason = ""
        self.at = 0.0

    def __call__(self, reason: str) -> None:
        self.reason = reason
        self.at = time.monotonic()
        self.event.set()


def test_stale_heartbeat_triggers_kill() -> None:
    kill = KillRecorder()
    timeout = 0.3
    with Watchdog(
        heartbeat_timeout_s=timeout, wall_budget_s=60.0, on_kill=kill, poll_interval_s=0.02
    ):
        started = time.monotonic()
        assert kill.event.wait(timeout + 1.0)
    assert "heartbeat stale" in kill.reason
    assert kill.at - started <= timeout + 0.5


def test_heartbeats_keep_run_alive() -> None:
    kill = KillRecorder()
    with Watchdog(
        heartbeat_timeout_s=0.3, wall_budget_s=60.0, on_kill=kill, poll_interval_s=0.02
    ) as wd:
        for _ in range(10):
            time.sleep(0.1)
            wd.heartbeat()
    assert not kill.event.is_set()
    assert wd.kill_reason is None


def test_wall_budget_exceeded_triggers_kill() -> None:
    kill = KillRecorder()
    budget = 0.4
    with Watchdog(
        heartbeat_timeout_s=60.0, wall_budget_s=budget, on_kill=kill, poll_interval_s=0.02
    ) as wd:
        started = time.monotonic()
        deadline = started + budget + 1.0
        while not kill.event.is_set() and time.monotonic() < deadline:
            wd.heartbeat()
            time.sleep(0.02)
    assert "wall budget" in kill.reason
    assert budget <= kill.at - started <= budget + 0.5
