"""Activation wait helpers (exam_timeout buffer, EXAM_FAILED detection)."""

import pytest

from bourse.config import Settings
from bourse.stack.activation import EXAM_WAIT_BUFFER_S, exam_wait_s, wait_agents_active


def test_exam_wait_s_is_computed_from_task_counts() -> None:
    """Budget = sum/concurrency + max of per-exam bounds + buffer (not a fixed 1020s)."""
    settings = Settings(bourse_exam_timeout_s=900.0, bourse_exam_deadline_s=300.0)
    cap = 900.0 + 300.0 + 60.0  # hard cap per exam; 5 tasks x 3 attempts x 305s exceeds it
    assert exam_wait_s(settings, [5, 5]) == 2 * cap / 2 + cap + EXAM_WAIT_BUFFER_S
    # Honest roster default: 5 exams (profiler has 2 skills) at concurrency 2.
    assert exam_wait_s(settings) == 5 * cap / 2 + cap + EXAM_WAIT_BUFFER_S


def test_exam_wait_s_tracks_settings() -> None:
    settings = Settings(bourse_exam_timeout_s=600.0, bourse_exam_deadline_s=300.0)
    cap = 600.0 + 300.0 + 60.0
    assert exam_wait_s(settings, [5]) == cap / 2 + cap + EXAM_WAIT_BUFFER_S


def test_wait_active_fails_immediately_when_no_agents_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bourse.stack.activation as act

    monkeypatch.setattr(act, "_sample_ollama_resident", lambda *a, **k: None)
    monkeypatch.setattr(act, "registry_agent_rows", lambda *a, **k: [])
    lines: list[str] = []
    settings = Settings(bourse_registry_url="http://127.0.0.1:59999")
    code = wait_agents_active(settings, timeout_s=5.0, echo=lines.append)
    assert code == 1
    assert any("no agents registered; run stack register" in line for line in lines)
