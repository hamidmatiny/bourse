"""Activation wait helpers (exam_timeout buffer, EXAM_FAILED detection)."""

from bourse.config import Settings
from bourse.stack.activation import EXAM_WAIT_BUFFER_S, exam_wait_s


def test_exam_wait_s_is_timeout_plus_buffer() -> None:
    settings = Settings(bourse_exam_timeout_s=900.0)
    assert exam_wait_s(settings) == 900.0 + EXAM_WAIT_BUFFER_S


def test_exam_wait_s_tracks_settings() -> None:
    settings = Settings(bourse_exam_timeout_s=600.0)
    assert exam_wait_s(settings) == 720.0
