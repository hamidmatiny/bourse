"""Test-only fakes for BOURSE-Bench: a truth-answering fake LLM (B0) and scripted runners."""

import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from bourse.bench.harness import CellOutcome, CellRunner, SubtaskOutcome, adversary_key
from bourse.contracts.bench import (
    AdversaryResult,
    AdversarySpec,
    BenchCase,
    ConfigSpec,
    PiiCaseResult,
)
from bourse.contracts.metering import Usage
from bourse.contracts.skills import SkillId
from bourse.llm.base import ChatMessage, ChatResult, ToolCall, ToolSpec

TRUTH_KEY = {
    SkillId.SCHEMA_PROFILE: "profile",
    SkillId.STATS_COMPUTE: "stats",
    SkillId.PII_DETECT: "pii",
    SkillId.UNITS_NORMALIZE: "units",
}


def truth_output(skill: SkillId, truth: dict[str, Any]) -> dict[str, Any]:
    if skill is SkillId.REPORT_COMPOSE:
        cited = [
            {"column": s["column"], "stat": stat, "value": s[stat]}
            for s in truth["stats"]["stats"]
            for stat in ("mean", "min", "max")
            if s[stat] is not None
        ]
        return {"summary": "Numeric columns summarised.", "cited": cited}
    out: dict[str, Any] = truth[TRUTH_KEY[skill]]
    return out


class TruthChat:
    """Fake LLM for B0: answers every submit with the case truth for the asked skill."""

    def __init__(self, truth: dict[str, Any], fail_skills: Sequence[SkillId] = ()) -> None:
        self.truth = truth
        self.fail = set(fail_skills)
        self.calls: list[str] = []

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        deadline_s: float,
    ) -> ChatResult:
        text = messages[-1].content or ""
        found = re.search(r"Task: ([a-z.]+)\.", text) or re.search(
            r"Task: ([a-z.]+)\.", messages[1].content or ""
        )
        assert found is not None
        skill = SkillId(found.group(1))
        self.calls.append(skill.value)
        usage = Usage(tokens_in=100, tokens_out=20, wall_ms=5.0, model="fake", provider="fake")
        if skill in self.fail:
            return ChatResult(text="I cannot.", usage=usage)
        call = ToolCall(name="submit", arguments=truth_output(skill, self.truth))
        return ChatResult(text="", tool_calls=(call,), usage=usage, finish_reason="tool_calls")


class ScriptedRunner:
    """CellRunner that answers from truth; `crash_on` raises to simulate a process crash."""

    def __init__(self, spec: ConfigSpec, crash_on: str | None = None) -> None:
        self.spec = spec
        self.crash_on = crash_on
        self.ran: list[str] = []

    async def run_cell(self, case: BenchCase, repeat: int) -> CellOutcome:
        key = f"{self.spec.id}/{case.case_id}/{repeat}"
        if key == self.crash_on:
            raise RuntimeError(f"simulated crash in {key}")
        self.ran.append(key)
        subs = tuple(
            SubtaskOutcome(s.value, s, "COMPLETED", "a@x", truth_output(s, case.truth))
            for s in SkillId
        )
        wall = 10.0 / self.spec.subtask_slots
        return CellOutcome(
            run_id=f"run-{key}",
            status="SUCCEEDED",
            planner_used="template",
            subtasks=subs,
            tokens_in=1000,
            tokens_out=100,
            compute_ms=4000.0,
            wall_s=wall,
            contention_events=0,
        )


class ScriptedSessions:
    def __init__(self, crash_on: str | None = None) -> None:
        self.crash_on = crash_on
        self.runners: list[ScriptedRunner] = []

    @asynccontextmanager
    async def __call__(self, spec: ConfigSpec) -> AsyncIterator[CellRunner]:
        runner = ScriptedRunner(spec, self.crash_on)
        self.runners.append(runner)
        yield runner


async def scripted_adversary(spec: AdversarySpec) -> list[AdversaryResult]:
    return [
        AdversaryResult(
            key=f"{adversary_key(spec)}#0",
            id=spec.id,
            mode=spec.mode or "mixed",
            honest_real=spec.honest_real,
            status="caught",
            actual="SPOTCHECK_FAIL",
            expected="SPOTCHECK_FAIL",
            exposure=8,
            exposure_real=6,
            canaries=2,
            first_bad=4,
            detect_delay=4,
            strikes=3,
            harmed=0,
            harmed_total=8,
            spec_wall_s=600.0,
            finished_at=datetime.now(UTC),
        )
    ]


async def scripted_pii(agent: str, case: BenchCase) -> PiiCaseResult:
    passed = "precise" in agent
    return PiiCaseResult(
        key=f"pii/{agent}/{case.case_id}",
        agent_id=agent,
        case_id=case.case_id,
        state="COMPLETED",
        passed=passed,
        f1=1.0 if passed else 0.5,
        tp=1,
        N=2,
        detail="scripted",
        wall_s=1.0,
        finished_at=datetime.now(UTC),
        output_sha256=f"{passed}:{case.case_id}",  # fast and precise differ on every case
    )
