"""Baseline building blocks: B0 generalist, B1 random policy, broker trust controls,
the ADR-0009 pool executor, and per-adversary honest phase (ADR-0051)."""

import time
from typing import Any

import anyio
import pytest

from bourse.adversaries.behaviors import EXAM_TASKS, HONEST_REAL, AdversaryExecutor
from bourse.bench.generator import generate_case
from bourse.bench.live import NO_TRUST, controls_for, usage_cost
from bourse.bench.solo import run_solo
from bourse.broker.select import PriorConfig, PriorPolicy, RandomPolicy
from bourse.contracts.bench import ConfigSpec
from bourse.contracts.broker import BrokerControls, SubtaskStatus
from bourse.contracts.metering import LlmUsageRecord
from bourse.contracts.registry import AgentRecord
from bourse.contracts.skills import SkillId
from bourse.contracts.skills_io import DatasetRef
from bourse.contracts.tasks import Subtask
from bourse.grading.graders import grade
from bourse.runtime.executor import PoolExecutor, WorkKind
from tests.bench_fakes import TruthChat


async def test_b0_generalist_runs_template_and_grades_from_truth() -> None:
    case = generate_case("dev", "b0", 1)
    chat = TruthChat(case.truth)
    subs = await run_solo(
        lambda _tid: chat, "bench-b0-x", DatasetRef.from_csv(case.csv_text),
        max_tokens=512, deadline_s=5.0,
    )  # fmt: skip
    assert [s.status for s in subs] == [SubtaskStatus.COMPLETED] * 5
    for s in subs:
        assert s.output is not None and grade(s.skill, s.output, case.truth).passed, s.skill
    assert chat.calls[-1] == "report.compose"  # report last, after its dependencies


async def test_b0_failed_dependency_skips_dependants() -> None:
    case = generate_case("dev", "b0", 2)
    chat = TruthChat(case.truth, fail_skills=[SkillId.SCHEMA_PROFILE])
    subs = await run_solo(
        lambda _tid: chat, "bench-b0-y", DatasetRef.from_csv(case.csv_text),
        max_tokens=512, deadline_s=5.0,
    )  # fmt: skip
    st = {s.subtask_id: s.status for s in subs}
    assert st["profile"] is SubtaskStatus.FAILED
    assert st["stats"] is SubtaskStatus.SKIPPED and st["report"] is SubtaskStatus.SKIPPED
    assert st["pii"] is SubtaskStatus.COMPLETED


def _agent(aid: str) -> AgentRecord:
    from datetime import UTC, datetime

    from bourse.contracts.trust import TrustState

    return AgentRecord.model_validate(
        {
            "agent_id": aid,
            "vendor_domain": "v.local",
            "name": aid.split("@")[0],
            "state": TrustState.ACTIVE,
            "skills": [SkillId.PII_DETECT],
            "current_version": "1.0.0",
            "current_digest": "0" * 64,
            "card_url": "http://x/.well-known/agent-card.json",
            "tags": [],
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
    )


def test_random_policy_seeded_and_covers_candidates() -> None:
    agents = [_agent(f"a{i}@v.local") for i in range(3)]
    sub = Subtask(id="pii", skill=SkillId.PII_DETECT, input_ref="dataset")
    seq1 = [RandomPolicy("s").choose(sub, agents).chosen for _ in range(1)]
    p1, p2 = RandomPolicy("seed-x"), RandomPolicy("seed-x")
    a = [p1.choose(sub, agents).chosen for _ in range(30)]
    b = [p2.choose(sub, agents).chosen for _ in range(30)]
    assert a == b and seq1
    assert set(a) == {ag.agent_id for ag in agents}
    d = RandomPolicy("z").choose(sub, agents)
    assert d.chosen not in d.fallbacks and len(d.fallbacks) == 2


def test_b2_prior_ties_are_a_seeded_draw_per_task_not_name_order() -> None:
    """ADR-0051 fix round 2: with one model every prior ties; agent_id order would always pick
    `poison-quiet@…` / `sleeper-*@…` over `vendor-*@…`. B2 draws per (case, repeat, subtask)."""
    agents = [
        _agent(a) for a in ("poison-quiet@x.local", "sleeper-loud@x.local", "vendor-c@c.local")
    ]
    sub = Subtask(id="pii", skill=SkillId.PII_DETECT, input_ref="dataset")
    assert PriorPolicy(PriorConfig()).choose(sub, agents).chosen == "poison-quiet@x.local"
    picks = [
        PriorPolicy(PriorConfig(), tie_seed=f"case-{i}:1").choose(sub, agents).chosen
        for i in range(300)
    ]
    counts = {a.agent_id: picks.count(a.agent_id) for a in agents}
    assert all(70 <= n <= 130 for n in counts.values()), counts  # ~100 each, not all-or-none
    again = PriorPolicy(PriorConfig(), tie_seed="case-7:1").choose(sub, agents)
    assert again.chosen == picks[7] and "seeded hash per task" in again.reason
    assert len(again.fallbacks) == 2 and again.chosen not in again.fallbacks
    other = Subtask(id="pii2", skill=SkillId.PII_DETECT, input_ref="dataset")
    per_subtask = [
        PriorPolicy(PriorConfig(), tie_seed=f"case-{i}:1").choose(other, agents).chosen
        for i in range(300)
    ]
    assert per_subtask != picks  # the subtask id is part of the draw


def test_b2_seeded_tie_break_never_overrides_utility() -> None:
    agents = [_agent("a-cheap@v.local"), _agent("b-dear@v.local")]
    cfg = PriorConfig(
        shadow_prices={"m-cheap": 0.1, "m-dear": 5.0},
        agent_models={"a-cheap@v.local": "m-cheap", "b-dear@v.local": "m-dear"},
    )
    sub = Subtask(id="pii", skill=SkillId.PII_DETECT, input_ref="dataset")
    assert {PriorPolicy(cfg, tie_seed=f"s{i}").choose(sub, agents).chosen for i in range(50)} == {
        "a-cheap@v.local"
    }


def test_baseline_controls() -> None:
    assert controls_for(ConfigSpec(id="b1", baseline="B1")) == NO_TRUST
    assert controls_for(ConfigSpec(id="b2", baseline="B2")) == NO_TRUST
    assert NO_TRUST.task_budgets  # the compose meter requires per-task budgets
    assert controls_for(ConfigSpec(id="b3", baseline="B3")) == BrokerControls()
    assert not controls_for(ConfigSpec(id="x", baseline="B3", ablation="no-spotcheck")).spot_checks
    assert not controls_for(ConfigSpec(id="y", baseline="B3", ablation="no-canary")).canaries


def test_usage_cost_prefers_ollama_duration_and_counts_contention() -> None:
    from datetime import UTC, datetime

    def row(**kw: Any) -> LlmUsageRecord:
        base: dict[str, Any] = {
            "agent_id": "a",
            "task_id": "r:pii:1",
            "provider": "ollama",
            "model": "m",
            "tokens_in": 10,
            "tokens_out": 2,
            "wall_ms": 900.0,
            "status": 200,
            "error_kind": None,
            "at": datetime.now(UTC),
        }
        return LlmUsageRecord.model_validate(base | kw)

    rows = [row(total_duration_ms=500.0), row(), row(status=429, error_kind="contention")]
    assert usage_cost(rows) == (30, 6, 500.0 + 900.0 + 900.0, 1)


async def test_pool_executor_bounds_llm_and_keeps_order() -> None:
    ex = PoolExecutor(llm_slots=2, cpu_workers=1)
    live = 0
    peak = 0

    async def work(i: int) -> int:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await anyio.sleep(0.02)
        live -= 1
        return i * i

    started = time.monotonic()
    out: list[int] = await ex.map(WorkKind.LLM, work, list(range(6)))
    assert out == [i * i for i in range(6)] and peak == 2
    assert time.monotonic() - started >= 0.05  # 6 tasks / 2 slots x 20 ms
    assert await ex.map(WorkKind.CPU, abs, [-3, 4, -5]) == [3, 4, 5]
    assert await ex.submit_cpu(len, "abcd") == 4
    with pytest.raises(TypeError):
        await ex.map(WorkKind.LLM, abs, [1])
    await ex.aclose()


def test_honest_real_sets_sleeper_prefix_per_adversary() -> None:
    ex = AdversaryExecutor.__new__(AdversaryExecutor)
    ex._completed = 0
    ex._prefix_count = 0
    ex._behavior_log = []
    ex.honest_real = HONEST_REAL
    assert ex.prefix_before_deviate == EXAM_TASKS + HONEST_REAL
    ex.reset_behavior(honest_real=7)
    assert ex.prefix_before_deviate == EXAM_TASKS + 7
    ex._prefix_count = EXAM_TASKS + 6
    assert not ex._awake()
    ex._prefix_count = EXAM_TASKS + 7
    assert ex._awake()
    ex.reset_behavior()  # None keeps the configured value
    assert ex.honest_real == 7 and ex._prefix_count == 0
    with pytest.raises(ValueError):
        ex.reset_behavior(honest_real=-1)
