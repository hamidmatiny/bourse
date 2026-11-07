"""ADR-0053 amendment 1: exposure guarantee, attempted vs harmed, per-spec wall cap."""

import json
from typing import Any

import anyio
import httpx
import pytest
from pydantic import ValidationError

from bourse.adversaries import runner as runner_mod
from bourse.adversaries.runner import (
    EXPOSURE_K,
    _gate_honest_providers,
    _scenario_groups,
    _ungate_honest_providers,
    a6_harm,
    exposure_target,
)
from bourse.contracts.bench import AdversarySpec, BenchConfig, registration_gated
from bourse.contracts.skills import SkillId


def test_exposure_target_counts_the_honest_prefix() -> None:
    assert exposure_target("A6a", "full") == EXPOSURE_K == 3
    assert exposure_target("A8a-raw", "no-exam") == 3
    # Patient scripts are honest for the 5 exam tasks; the exam consumes them unless no-exam.
    assert exposure_target("A8a-patient", "no-metercap") == 3
    assert exposure_target("A7-patient", "no-exam") == 3 + 5
    # Sleepers: exam (5) + honest_real (default 3) honest executes.
    assert exposure_target("A5a", "full") == 3 + 3
    assert exposure_target("A5b", "full", honest_real=1) == 3 + 1
    assert exposure_target("A5a", "no-exam") == 3 + 3 + 5


def test_a6_harm_is_attempt_on_full_stack_and_harm_without_sinkhole() -> None:
    assert a6_harm("full", exposure_real=3, egress_events=4) == 0
    assert a6_harm("no-exam", exposure_real=3, egress_events=4) == 0
    assert a6_harm("no-sinkhole", exposure_real=3, egress_events=4) == 3
    assert a6_harm("no-sinkhole", exposure_real=3, egress_events=0) == 0


def test_runtime_adversaries_default_to_sole_provider() -> None:
    groups = dict(_scenario_groups(["A1", "A6a", "A8c", "A5a"], runs=3))
    assert groups["A1"] == [("card", 3)]
    assert groups["A6a"] == [("sole-provider", 3)]
    assert groups["A8c"] == [("sole-provider", 3)]
    assert [m for m, _ in groups["A5a"]] == ["mixed", "sole-provider"]
    # mixed (selection decides exposure) only when asked for.
    assert dict(_scenario_groups(["A6a"], 2, modes=["mixed"]))["A6a"] == [("mixed", 2)]


async def test_gate_hides_honest_providers_of_the_adversary_skill_only() -> None:
    calls: list[tuple[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            assert req.url.params["skill"] == "schema.profile"
            return httpx.Response(
                200,
                json=[
                    {"agent_id": "vendor-a-profiler@vendor-a.local"},
                    {"agent_id": "exfil-http@vendor-x.local"},
                ],
            )
        calls.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gated = await _gate_honest_providers(
            client, "http://r", {}, (SkillId.SCHEMA_PROFILE,), echo=lambda _l: None
        )
        assert gated == [("vendor-a-profiler@vendor-a.local", "schema.profile")]
        await _ungate_honest_providers(client, "http://r", {}, gated, echo=lambda _l: None)
    assert calls == [
        (
            "/agents/vendor-a-profiler@vendor-a.local/availability",
            {"available": False, "skill": "schema.profile"},
        ),
        (
            "/agents/vendor-a-profiler@vendor-a.local/availability",
            {"available": True, "skill": "schema.profile"},
        ),
    ]


def test_registration_gated_specs_must_say_why_they_are_never_exposed() -> None:
    assert registration_gated("A8a-raw", "full") and registration_gated("A8a-raw", "no-metercap")
    assert not registration_gated("A8a-raw", "no-exam")
    assert not registration_gated("A6a", "full")
    with pytest.raises(ValidationError, match="set no_exposure_reason"):
        BenchConfig(name="x", adversaries=(AdversarySpec(id="A7-raw"),))
    with pytest.raises(ValidationError, match="only for registration-gated"):
        BenchConfig(name="x", adversaries=(AdversarySpec(id="A6a", no_exposure_reason="no"),))
    BenchConfig(name="x", adversaries=(AdversarySpec(id="A7-raw", no_exposure_reason="exam"),))


async def test_spec_wall_cap_records_an_error_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from bourse.bench.live import adversary_runner
    from bourse.config import Settings

    async def slow(*_a: Any, **_k: Any) -> Any:
        await anyio.sleep(60)

    monkeypatch.setattr(runner_mod, "run_phase5", slow)
    lines: list[str] = []
    run = adversary_runner(Settings(), lines.append)
    with anyio.fail_after(5):
        rows = await run(AdversarySpec(id="A8b-patient", wall_cap_s=0.2))
    assert len(rows) == 1
    r = rows[0]
    assert (r.status, r.rule, r.actual) == ("error", "R1", "spec wall cap 0s")
    assert r.spec_wall_s < 5
    assert any("spec wall cap" in ln for ln in lines)
