"""Latency percentile helpers and exam deadline = max(base, 3*p95)."""

from bourse.llm.latency import LatencySample, percentile, summarize, timings_from_ollama


def test_percentile_nearest_rank() -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 10.0]
    assert percentile(vals, 50) == 3.0
    assert percentile(vals, 95) == 10.0


def test_summarize_exam_deadline() -> None:
    samples = [
        LatencySample(1.0, 0.1, 0.2, 0.3, 10, 2),
        LatencySample(2.0, 0.1, 0.2, 0.4, 10, 2),
        LatencySample(3.0, 0.1, 0.2, 0.5, 10, 2),
        LatencySample(4.0, 0.1, 0.2, 0.6, 10, 2),
        LatencySample(10.0, 9.0, 0.2, 0.7, 10, 2),
    ]
    s = summarize("m", samples)
    assert s.p95_total_s == 10.0
    assert s.exam_deadline_s(300.0) == 300.0
    assert s.exam_deadline_s(20.0) == 30.0  # 3 * 10


def test_timings_from_ollama_reload_flag() -> None:
    cold = timings_from_ollama(
        {
            "total_duration": 10_000_000_000,
            "load_duration": 9_900_000_000,
            "prompt_eval_duration": 330_000_000,
            "eval_duration": 100_000_000,
        }
    )
    assert cold["total_duration_ms"] == 10_000.0
    assert cold["load_duration_ms"] == 9_900.0
    assert cold["reload"] is True
    warm = timings_from_ollama({"load_duration": 50_000_000})
    assert warm["reload"] is False
