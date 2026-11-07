"""Bench preconditions and fail-loud stack up / launch (ADR-0051 fix round 3)."""

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from bourse.bench.preflight import agent_problems
from bourse.contracts.bench import BenchConfig
from bourse.contracts.registry import AgentRecord

NOW = datetime(2026, 11, 6, tzinfo=UTC)
PII = ["vendor-c-pii-fast@vendor-c.local", "vendor-b-pii-precise@vendor-b.local"]


def rec(agent_id: str, state: str = "ACTIVE") -> AgentRecord:
    return AgentRecord.model_validate(
        {
            "agent_id": agent_id,
            "vendor_domain": agent_id.split("@")[1],
            "name": agent_id,
            "card_url": None,
            "state": state,
            "current_version": "1.0.0",
            "current_digest": "0" * 64,
            "skills": ["pii.detect"],
            "tags": [],
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


HONEST = [*PII, "vendor-a-profiler@vendor-a.local", "vendor-a-reporter@vendor-a.local",
          "vendor-b-normalizer@vendor-b.local"]  # fmt: skip


def cfg(**kw: Any) -> BenchConfig:
    base: dict[str, Any] = {
        "name": "pre",
        "configs": [{"id": "B2", "baseline": "B2"}],
        "pii_separation": {"agents": PII},
    }
    return BenchConfig.model_validate(base | kw)


def test_empty_registry_is_refused_with_reasons() -> None:
    problems = agent_problems(cfg(), [], expected_honest=5)
    assert problems[0].startswith("registered honest agents 0/5")
    assert f"pii_separation agent {PII[0]} is not registered" in problems


def test_pending_agent_is_named_once() -> None:
    agents = [rec(a) for a in HONEST if a != PII[1]] + [rec(PII[1], "PENDING")]
    problems = agent_problems(cfg(), agents, expected_honest=5)
    assert problems == [f"{PII[1]} is PENDING, not ACTIVE"]


def test_all_active_passes_and_adversaries_do_not_count_as_honest() -> None:
    agents = [rec(a) for a in HONEST] + [rec("sleeper-loud@vendor-x.local", "QUARANTINED")]
    assert agent_problems(cfg(), agents, expected_honest=5) == []
    assert agent_problems(cfg(), agents[:4], expected_honest=5)[0].startswith(
        "registered honest agents 4/5"
    )


def test_b0_only_needs_no_agents() -> None:
    only_b0 = cfg(configs=[{"id": "B0", "baseline": "B0"}], pii_separation=None)
    assert agent_problems(only_b0, [], expected_honest=5) == []


def _ac_host(monkeypatch: pytest.MonkeyPatch, **over: Any) -> None:
    """Launch tests never call pmset: a fixed AC sample (or `over` fields)."""
    from datetime import UTC, datetime

    from bourse.bench import host, preflight
    from bourse.contracts.bench import HostSample

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
    } | over
    monkeypatch.setattr(
        host, "sample", lambda phase, **k: HostSample(**(fields | {"phase": phase}))
    )
    monkeypatch.setattr(preflight, "fetch_profile", lambda url, timeout_s=10.0: "full")
    # CI runs on Linux without caffeinate; the launch check looks it up with shutil.which.
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **k: "/usr/bin/caffeinate" if name == "caffeinate" else real_which(name),
    )


def test_bench_launch_refuses_and_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bourse.bench import cli as bench_cli
    from bourse.bench import preflight

    _ac_host(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text("name: pre\nconfigs: [{id: B2, baseline: B2}]\n")
    monkeypatch.setattr(preflight, "fetch_agents", lambda url, timeout_s=10.0: [])
    popen: list[Any] = []
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: popen.append(a))
    res = CliRunner().invoke(bench_cli.bench_app, ["launch", "--config", "c.yaml"])
    assert res.exit_code == 2
    assert "REFUSED: bench launch preconditions failed" in res.output
    assert "registered honest agents 0/5" in res.output
    assert popen == []  # nothing was started


def test_bench_launch_unreachable_registry_is_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from bourse.bench import cli as bench_cli
    from bourse.bench import preflight

    def down(url: str, timeout_s: float = 10.0) -> list[AgentRecord]:
        raise httpx.ConnectError("connection refused")

    _ac_host(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.yaml").write_text("name: pre\nconfigs: [{id: B3, baseline: B3}]\n")
    monkeypatch.setattr(preflight, "fetch_agents", down)
    res = CliRunner().invoke(bench_cli.bench_app, ["launch", "--config", "c.yaml"])
    assert res.exit_code == 2 and "unreachable" in res.output


class _Proc:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def _stub_stack(monkeypatch: pytest.MonkeyPatch, calls: list[str], register: Any) -> None:
    import bourse.cli as cli
    from bourse.stack import activation, image_freshness, launcher
    from bourse.stack import register as reg

    monkeypatch.setattr(image_freshness, "stale_compose_images", lambda: [])
    monkeypatch.setattr(launcher, "set_mode", lambda m: None)
    monkeypatch.setattr(launcher, "verify_bourse_gone", lambda: ([], []))
    monkeypatch.setattr(launcher, "write_compose_env", lambda *a: ["x.env"])

    def compose(*a: str, **k: Any) -> _Proc:
        calls.append("compose:" + a[0])
        return _Proc()

    def wait(s: Any, echo: Any) -> int:
        calls.append("wait")
        return 0

    monkeypatch.setattr(launcher, "compose", compose)
    monkeypatch.setattr(cli, "_wait_compose_healthy", lambda timeout_s: 0)
    monkeypatch.setattr(activation, "_sample_ollama_resident", lambda *a, **k: None)
    monkeypatch.setattr(
        activation, "dump_activation_diagnostics", lambda echo: calls.append("dump")
    )
    monkeypatch.setattr(reg, "register_all", register)
    monkeypatch.setattr(activation, "wait_agents_active", wait)


def test_stack_up_compose_registers_and_waits_without_adversaries_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bourse.cli as cli

    calls: list[str] = []

    def register(s: Any, m: str) -> Any:
        calls.append("register")
        return [["h"]], 5, 5

    _stub_stack(monkeypatch, calls, register)
    res = CliRunner().invoke(cli.app, ["stack", "up", "--mode", "compose"])
    assert res.exit_code == 0, res.output
    assert calls == ["compose:up", "register", "wait"]  # no adversaries profile
    assert "registered 5/5 ACCEPTED" in res.output


@pytest.mark.parametrize(
    ("register", "message"),
    [
        (lambda s, m: ([["h"]], 4, 5), "STACK UP FAILED: registered 4/5 ACCEPTED"),
        (lambda s, m: ([["h"]], 0, 0), "STACK UP FAILED: registered 0/0 ACCEPTED"),
    ],
)
def test_stack_up_registration_failure_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, register: Any, message: str
) -> None:
    import bourse.cli as cli

    calls: list[str] = []
    _stub_stack(monkeypatch, calls, register)
    res = CliRunner().invoke(cli.app, ["stack", "up", "--mode", "compose"])
    assert res.exit_code == 1 and message in res.output
    assert "wait" not in calls and "dump" in calls


def test_stack_up_registry_error_and_exam_failure_exit_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bourse.cli as cli
    from bourse.stack import activation

    def boom(s: Any, m: str) -> Any:
        raise ConnectionError("registry down")

    calls: list[str] = []
    _stub_stack(monkeypatch, calls, boom)
    res = CliRunner().invoke(cli.app, ["stack", "up", "--mode", "compose"])
    assert (
        res.exit_code == 1 and "STACK UP FAILED: registration error: ConnectionError" in res.output
    )

    calls.clear()
    _stub_stack(monkeypatch, calls, lambda s, m: ([["h"]], 5, 5))
    monkeypatch.setattr(activation, "wait_agents_active", lambda s, echo: 1)
    res = CliRunner().invoke(cli.app, ["stack", "up", "--mode", "compose"])
    assert res.exit_code == 1 and "STACK UP FAILED: not every agent is ACTIVE" in res.output


def test_run_where_every_cell_errored_is_reported_as_failed() -> None:
    from bourse.bench.cli import _all_error_line
    from bourse.contracts.bench import PiiCaseResult

    def pii(state: str) -> PiiCaseResult:
        return PiiCaseResult(
            key=f"pii/a/{state}", agent_id="a", case_id=state, state=state, passed=False,
            f1=0.0, tp=0, N=1, detail="", wall_s=0.0, finished_at=NOW,
        )  # fmt: skip

    assert _all_error_line([pii("ERROR"), pii("ERROR")], ran=2) == [
        "BENCH RUN FAILED: all 2 cells run this invocation ended in ERROR"
    ]
    assert _all_error_line([pii("ERROR"), pii("COMPLETED")], ran=2) == []
    assert _all_error_line([pii("ERROR")], ran=0) == []  # nothing ran (pure resume)


def test_stack_up_writes_the_stack_profile_and_rejects_unknown_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bourse.cli as cli
    from bourse.stack import launcher

    calls: list[str] = []
    _stub_stack(monkeypatch, calls, lambda s, m: ([["h"]], 5, 5))
    profiles: list[str] = []

    def write_env(s: Any, p: str = "full") -> list[str]:
        profiles.append(p)
        return ["x.env"]

    monkeypatch.setattr(launcher, "write_compose_env", write_env)
    res = CliRunner().invoke(
        cli.app, ["stack", "up", "--mode", "compose", "--stack-profile", "no-exam"]
    )
    assert res.exit_code == 0, res.output
    assert profiles == ["no-exam"] and "stack profile no-exam" in res.output
    bad = CliRunner().invoke(cli.app, ["stack", "up", "--stack-profile", "no-broker"])
    # Rich wraps the error panel at the terminal width (narrow in CI) and adds ANSI codes.
    plain = " ".join(re.sub(r"\x1b\[[0-9;]*m|[│╭╮╰╯─]", " ", bad.output).split())
    assert bad.exit_code != 0 and "--stack-profile must be one of" in plain


def test_agent_state_line_flags_honest_quarantine() -> None:
    from bourse.bench.preflight import agent_state_line

    agents = [rec(a) for a in HONEST] + [rec("sleeper-loud@vendor-x.local", "QUARANTINED")]
    ok = agent_state_line(agents)
    assert ok == "honest agents at end: ACTIVE 5/5; not ACTIVE: none; adversaries QUARANTINED 1"
    agents[0] = rec(HONEST[0], "QUARANTINED")
    assert f"ACTIVE 4/5; not ACTIVE: {HONEST[0]}=QUARANTINED" in agent_state_line(agents)
