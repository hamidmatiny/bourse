import json
from pathlib import Path

import anyio

from bourse.runtime.resident import (
    RESIDENT_LIMIT_BYTES,
    LoadedModel,
    ResidentMonitor,
    format_bytes_units,
    format_limit,
    format_resident_peak,
    monitoring,
    unexpected_loaded_models,
    warn_unexpected_loaded_models,
)

LIMIT = 26_000_000_000
OVER = LIMIT + 1_000_000
UNDER = LIMIT - 1_000_000


def test_resident_limit_bytes_is_26e9_not_26_gib() -> None:
    assert RESIDENT_LIMIT_BYTES == 26_000_000_000
    assert RESIDENT_LIMIT_BYTES == LIMIT
    assert RESIDENT_LIMIT_BYTES != 26 * (1024**3)
    assert format_limit(RESIDENT_LIMIT_BYTES) == "limit 26.00 GB / 24.21 GiB"
    assert format_bytes_units(RESIDENT_LIMIT_BYTES) == "26.00 GB / 24.21 GiB"
    line = format_resident_peak(23_216_881_898, RESIDENT_LIMIT_BYTES)
    assert "23216881898 bytes" in line
    assert "21.62 GiB" in line
    assert "limit 26.00 GB / 24.21 GiB" in line


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
    assert len(trips) == 1 and "over_s=30" in trips[0] and trips[0].startswith("RESIDENT_LIMIT:")
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
    from bourse.runtime.resident import format_resident_peak

    assert m.summary() == format_resident_peak(OVER, LIMIT)


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


async def test_jsonl_includes_per_model_fields(tmp_path: Path) -> None:
    from bourse.runtime.resident import LoadedModel

    calls = 0
    done = anyio.Event()

    async def sample() -> list[LoadedModel]:
        nonlocal calls
        calls += 1
        if calls >= 2:
            done.set()
        return [
            LoadedModel("gemma4:12b", 7_000_000_000, runner="mlx", context_length=4096),
            LoadedModel(
                "qwen3.5:27b-q4_K_M", 15_000_000_000, runner="llamacpp", context_length=4096
            ),
        ]

    out = tmp_path / "resident-models.jsonl"
    m = ResidentMonitor(sample, lambda _r: None, limit_bytes=LIMIT, interval_s=0.01, out=out)
    async with monitoring(m):
        with anyio.fail_after(5):
            await done.wait()
    line = json.loads(out.read_text().splitlines()[0])
    assert line["resident_bytes"] == 22_000_000_000
    assert line["models"][0]["runner"] == "mlx"
    assert line["models"][1]["context_length"] == 4096


def test_model_reloads_counted() -> None:
    from bourse.runtime.resident import LoadedModel, ReloadKind

    m = _monitor([])
    m.observe_models([LoadedModel("gemma", 10), LoadedModel("qwen", 20)])  # first sample
    m.observe_models([LoadedModel("gemma", 10), LoadedModel("qwen", 20)])
    assert m.counts_by_kind()[ReloadKind.INITIAL_LOAD.value] == 2
    # INITIAL_LOAD is per-session baseline, not a reload (ADR-0042 exposure fix).
    assert m.reloads == 0
    m.observe_models([LoadedModel("gemma", 12), LoadedModel("qwen", 20)])  # size changed
    m.observe_models([LoadedModel("qwen", 20)])  # unloaded: not a reload
    m.observe_models([LoadedModel("qwen", 20), LoadedModel("gemma", 12)])  # loaded again
    m.observe_models([LoadedModel("qwen", 20), LoadedModel("other", 5)])  # third model, gemma gone
    counts = m.counts_by_kind()
    assert counts[ReloadKind.INITIAL_LOAD.value] == 2
    assert counts[ReloadKind.PARAM_CHANGE.value] == 1
    assert counts[ReloadKind.KEEP_ALIVE_EXPIRY.value] == 1
    assert counts[ReloadKind.EVICTION.value] == 1
    assert m.reloads == 3  # PARAM_CHANGE + KEEP_ALIVE_EXPIRY + EVICTION
    assert "INITIAL_LOAD=2" in m.reload_summary()


def test_initial_load_is_per_session_not_a_reload() -> None:
    """Eleven phase5 sessions each seeing an already-resident model must not count 11 reloads."""
    from bourse.runtime.resident import LoadedModel, ReloadKind

    total_reloads = 0
    for _ in range(11):
        m = _monitor([])
        m.observe_models([LoadedModel("qwen3.5:27b-q4_K_M", 15_000_000_000)])
        assert m.counts_by_kind()[ReloadKind.INITIAL_LOAD.value] == 1
        assert m.reloads == 0
        total_reloads += m.reloads
    assert total_reloads == 0


def test_warn_unexpected_loaded_models_outside_roster() -> None:
    allowed = frozenset({"qwen3.5:27b-q4_K_M"})
    models = [
        LoadedModel("qwen3.5:27b-q4_K_M", 15_000_000_000),
        LoadedModel("gemma4:12b", 7_000_000_000),
    ]
    assert unexpected_loaded_models(models, allowed) == [("gemma4:12b", 7_000_000_000)]
    lines: list[str] = []
    warn_unexpected_loaded_models(models, allowed, echo=lines.append)
    assert len(lines) == 1
    assert "WARNING" in lines[0]
    assert "gemma4:12b=7000000000" in lines[0]
    assert (
        warn_unexpected_loaded_models(
            [LoadedModel("qwen3.5:27b-q4_K_M", 1)], allowed, echo=lines.append
        )
        == []
    )
    assert len(lines) == 1
