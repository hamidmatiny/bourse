"""Host CLI must refuse compose-only commands quickly (ADR-0048)."""

from pathlib import Path

import pytest

from bourse.stack import launcher
from bourse.stack.compose_guard import COMPOSE_HINT, refuse_if_compose_host


def test_refuse_if_compose_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "RUN_DIR", tmp_path)
    monkeypatch.setattr(launcher, "MODE_FILE", tmp_path / "mode")
    launcher.set_mode("compose")
    monkeypatch.delenv("BOURSE_IN_COMPOSE", raising=False)
    with pytest.raises(SystemExit) as ei:
        refuse_if_compose_host("attacks phase5")
    assert ei.value.code == 2


def test_no_refuse_inside_compose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher, "RUN_DIR", tmp_path)
    monkeypatch.setattr(launcher, "MODE_FILE", tmp_path / "mode")
    launcher.set_mode("compose")
    monkeypatch.setenv("BOURSE_IN_COMPOSE", "1")
    refuse_if_compose_host("attacks phase5")  # no raise


def test_hint_text() -> None:
    assert "docker compose run --rm broker bourse" in COMPOSE_HINT
