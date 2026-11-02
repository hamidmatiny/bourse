import json
from pathlib import Path

import anyio

from bourse.runtime.resident import ResidentMonitor, monitoring

LIMIT = 26_000_000_000
OVER = LIMIT + 1_000_000
UNDER = LIMIT - 1_000_000


def _monitor(trips: list[str]) -> ResidentMonitor:
    async def never() -> int:
        raise AssertionError("not sampled in this test")

    return ResidentMonitor(never, trips.append, limit_bytes=LIMIT, sustain_s=30.0, interval_s=5.0)


def test_aborts_after_30s_continuously_over() -> None:
    trips: list[str] = []
    m = _monitor(trips)
    for t in range(0, 30, 5):  # 0..25 s over: not yet 30 s
        m.observe(float(t), OVER)
    assert trips == []
    m.observe(30.0, OVER)
    assert len(trips) == 1 and "for 30s" in trips[0]
    m.observe(35.0, OVER)
    assert len(trips) == 1  # trips once
    assert m.peak == OVER


def test_20s_spike_does_not_abort() -> None:
    trips: list[str] = []
    m = _monitor(trips)
    for t in range(0, 25, 5):  # over at 0, 5, 10, 15, 20 s
        m.observe(float(t), OVER)
    m.observe(25.0, UNDER)
    for t in range(30, 60, 5):  # over again, restarted at 30 s; 50 s is only 20 s later
        m.observe(float(t), OVER if t <= 50 else UNDER)
    assert trips == []
    assert m.peak == OVER
    assert m.summary() == f"resident peak {OVER} (limit {LIMIT})"


async def test_background_sampling_writes_jsonl(tmp_path: Path) -> None:
    values = [UNDER, OVER, UNDER]
    calls = 0
    three_written = anyio.Event()

    async def sample() -> int:
        nonlocal calls
        calls += 1
        if calls > len(values):  # the first 3 samples were observed and written
            three_written.set()
            return UNDER
        return values[calls - 1]

    out = tmp_path / "resident.jsonl"
    m = ResidentMonitor(sample, lambda _r: None, limit_bytes=LIMIT, interval_s=0.01, out=out)
    async with monitoring(m):
        with anyio.fail_after(5):
            await three_written.wait()
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [x["resident_bytes"] for x in lines[:3]] == [UNDER, OVER, UNDER]
    assert m.peak == OVER


def test_model_reloads_counted() -> None:
    from bourse.runtime.resident import LoadedModel

    m = _monitor([])
    m.observe_models([LoadedModel("gemma", 10), LoadedModel("qwen", 20)])  # first sample
    m.observe_models([LoadedModel("gemma", 10), LoadedModel("qwen", 20)])
    assert m.reloads == 0
    m.observe_models([LoadedModel("gemma", 12), LoadedModel("qwen", 20)])  # size changed
    m.observe_models([LoadedModel("qwen", 20)])  # unloaded: not a reload
    m.observe_models([LoadedModel("qwen", 20), LoadedModel("gemma", 12)])  # loaded again
    assert m.reloads == 2
    assert m.reload_summary() == "model reloads 2"
