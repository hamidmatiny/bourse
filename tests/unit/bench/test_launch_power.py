"""`bench launch`: caffeinate wrapper and the power-state refusal (Phase 6b)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bourse.bench import host
from bourse.contracts.bench import HostSample


def _sample(**over: Any) -> HostSample:
    fields: dict[str, Any] = {
        "at": datetime.now(UTC),
        "phase": "start",
        "power_source": "AC Power",
        "battery_pct": 100,
        "battery_state": "charged",
        "powermode": 0,
        "low_power_mode": False,
        "thermal_warning": False,
        "cpu_speed_limit": None,
        "load_1m": 1.0,
        "load_5m": 1.0,
        "load_15m": 1.0,
    }
    return HostSample(**(fields | over))


def test_power_problems() -> None:
    assert host.power_problems(_sample()) == []
    batt = host.power_problems(_sample(power_source="Battery Power", battery_pct=80))
    assert batt == ["on Battery Power (battery 80%)"]
    low = host.power_problems(_sample(powermode=1, low_power_mode=True))
    assert low == ["Low Power Mode is on (powermode 1)"]
    unknown = host.power_problems(
        _sample(power_source=None, powermode=None, low_power_mode=None, error="pmset missing")
    )
    assert len(unknown) == 2 and "unknown" in unknown[0] and "unknown" in unknown[1]


def test_with_caffeinate() -> None:
    assert host.with_caffeinate(["docker", "x"]) == ["caffeinate", "-dimsu", "docker", "x"]


class _Proc:
    def wait(self, timeout: float | None = None) -> int:
        return 0


def _launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sample: HostSample, *args: str
) -> tuple[Any, list[list[str]]]:
    from bourse.bench import cli as bench_cli
    from bourse.bench import preflight
    from tests.unit.bench.test_preflight import HONEST, rec

    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text("name: pw\nconfigs: [{id: B3, baseline: B3}]\n")
    monkeypatch.setattr(
        host, "sample", lambda phase, **k: sample.model_copy(update={"phase": phase})
    )
    monkeypatch.setattr(
        preflight, "fetch_agents", lambda url, timeout_s=10.0: [rec(a) for a in HONEST]
    )
    monkeypatch.setattr(preflight, "fetch_profile", lambda url, timeout_s=10.0: "full")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    started: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> _Proc:
        assert kwargs["stdin"] is not None  # /dev/null, never the terminal
        started.append(argv)
        return _Proc()

    monkeypatch.setattr("subprocess.Popen", popen)
    res = CliRunner().invoke(bench_cli.bench_app, ["launch", "--config", "c.yaml", *args])
    return res, started


def test_launch_runs_under_caffeinate_on_ac(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    res, started = _launch(tmp_path, monkeypatch, _sample())
    assert res.exit_code == 0, res.output
    assert started and started[0][:2] == ["caffeinate", "-dimsu"]
    assert started[0][2:6] == ["docker", "compose", "run", "--rm"]


def test_launch_refuses_on_battery_and_logs_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    res, started = _launch(
        tmp_path, monkeypatch, _sample(power_source="Battery Power", battery_pct=55)
    )
    assert res.exit_code == 2 and started == []
    log = (tmp_path / "data/logs/bench-pw.log").read_text()
    assert "REFUSED: bench launch on this power state" in log
    assert "on Battery Power (battery 55%)" in log and "--allow-battery" in log


def test_launch_refuses_in_low_power_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res, started = _launch(tmp_path, monkeypatch, _sample(powermode=1, low_power_mode=True))
    assert res.exit_code == 2 and started == []
    assert "Low Power Mode is on" in res.output


def test_allow_battery_overrides_and_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res, started = _launch(
        tmp_path,
        monkeypatch,
        _sample(power_source="Battery Power", battery_pct=90),
        "--allow-battery",
    )
    assert res.exit_code == 0 and started and started[0][0] == "caffeinate"
    assert "WARNING: --allow-battery: on Battery Power" in res.output


def test_launch_refuses_without_caffeinate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bourse.bench import cli as bench_cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text("name: pw\nconfigs: [{id: B3, baseline: B3}]\n")
    monkeypatch.setattr(host, "sample", lambda phase, **k: _sample(phase=phase))
    monkeypatch.setattr("shutil.which", lambda name: None)
    res = CliRunner().invoke(bench_cli.bench_app, ["launch", "--config", "c.yaml"])
    assert res.exit_code == 2 and "caffeinate not found" in res.output


def test_launch_refuses_on_profile_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bourse.bench import preflight

    res, _started = _launch(tmp_path, monkeypatch, _sample())
    assert res.exit_code == 0
    monkeypatch.setattr(preflight, "fetch_profile", lambda url, timeout_s=10.0: "no-exam")
    from bourse.bench import cli as bench_cli

    res2 = CliRunner().invoke(bench_cli.bench_app, ["launch", "--config", "c.yaml"])
    assert res2.exit_code == 2
    assert "registry runs stack profile 'no-exam'" in res2.output
    log = (tmp_path / "data/logs/bench-pw.log").read_text()
    assert "REFUSED: bench launch preconditions failed" in log
