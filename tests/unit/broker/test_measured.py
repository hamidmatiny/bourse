"""MeasuredPolicy, exploration quota, spot-check fallback, ledger + selection persistence."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from bourse.agents.common.executor import ParsedRequest
from bourse.broker.select import EXPLORATION, MeasuredConfig, MeasuredPolicy, PriorConfig
from bourse.contracts.broker import DelegationState, RunStatus, SelectionRecord
from bourse.contracts.registry import AgentRecord
from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import Subtask
from bourse.ledger.repo import InMemoryLedgerRepository
from tests.broker_harness import TRUTH, BrokerBed, FakeAgent, Outcome, intake, one_agent_per_skill
from tests.ledger_helpers import history, row
from tests.registry_helpers import make_party

FAST = "fast@v.local"
PRECISE = "precise@v.local"
PRIORS = PriorConfig(
    shadow_prices={"small": 0.10, "large": 0.25},
    latency_priors={"small": 5.0, "large": 60.0},
    agent_models={FAST: "small", PRECISE: "large"},
)
SUB = Subtask(id="pii", skill=SkillId.PII_DETECT, input_ref="dataset")
NOW = datetime(2026, 10, 31, tzinfo=UTC)


def record(agent_id: str, version: str = "1.0.0") -> AgentRecord:
    return AgentRecord.model_validate(
        {
            "agent_id": agent_id,
            "vendor_domain": "v.local",
            "name": agent_id,
            "card_url": None,
            "state": "ACTIVE",
            "current_version": version,
            "current_digest": "0" * 64,
            "skills": ["pii.detect"],
            "tags": [],
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


CANDS = [record(PRECISE), record(FAST)]


async def choose(policy: MeasuredPolicy, cands: list[AgentRecord] = CANDS) -> Any:
    await policy.refresh(SkillId.PII_DETECT, cands)
    return policy.choose(SUB, cands)


async def test_precise_chosen_when_fast_reliability_is_low() -> None:
    ledger = InMemoryLedgerRepository()
    ledger.rows = history(FAST, 2, 8, usd=0.0004, wall_ms=3000) + history(
        PRECISE, 10, 0, usd=0.001, wall_ms=50000
    )
    decision = await choose(MeasuredPolicy(MeasuredConfig(explore_every=0), PRIORS, ledger))
    assert decision.chosen == PRECISE
    terms = {c.agent_id: c.terms for c in decision.candidates}
    assert (terms[FAST].s, terms[FAST].f, terms[PRECISE].s) == (2, 8, 10)
    assert terms[FAST].measured and terms[PRECISE].measured
    assert terms[PRECISE].cost_norm == 1.0 and terms[PRECISE].latency_norm == 1.0


@pytest.mark.parametrize(("lambda_cost", "expected"), [(0.0, PRECISE), (2.0, FAST)])
async def test_lambda_trades_reliability_for_cost(lambda_cost: float, expected: str) -> None:
    ledger = InMemoryLedgerRepository()
    ledger.rows = history(FAST, 8, 2, usd=0.0004, wall_ms=3000) + history(
        PRECISE, 10, 0, usd=0.001, wall_ms=3000
    )
    cfg = MeasuredConfig(lambda_cost=lambda_cost, mu_latency=0.0, explore_every=0)
    decision = await choose(MeasuredPolicy(cfg, PRIORS, ledger))
    assert decision.chosen == expected


async def test_cold_start_uses_priors_and_prefers_cheaper() -> None:
    decision = await choose(
        MeasuredPolicy(MeasuredConfig(explore_every=0), PRIORS, InMemoryLedgerRepository())
    )
    terms = {c.agent_id: c.terms for c in decision.candidates}
    assert not terms[FAST].measured
    assert terms[FAST].cost == pytest.approx(0.10 * 2000 / 1e6)
    assert terms[PRECISE].latency == 60_000
    assert decision.chosen == FAST


async def test_new_major_version_starts_cold() -> None:
    ledger = InMemoryLedgerRepository()
    ledger.rows = history(PRECISE, 10, 0)
    decision = await choose(
        MeasuredPolicy(MeasuredConfig(explore_every=0), PRIORS, ledger),
        [record(PRECISE, "2.0.0"), record(FAST)],
    )
    terms = {c.agent_id: c.terms for c in decision.candidates}
    assert (terms[PRECISE].s, terms[PRECISE].f, terms[PRECISE].measured) == (0, 0, False)


async def simulate(n: int) -> list[tuple[str | None, str]]:
    """n consecutive decisions; every chosen agent succeeds; returns (chosen, reason)."""
    ledger = InMemoryLedgerRepository()
    policy = MeasuredPolicy(MeasuredConfig(min_trials=5, explore_every=4), PRIORS, ledger)
    out: list[tuple[str | None, str]] = []
    for i in range(n):
        decision = await choose(policy)
        out.append((decision.chosen, decision.reason))
        await ledger.add_selection(
            SelectionRecord(
                run_id=f"run-{i}",
                subtask_id="pii",
                skill=SkillId.PII_DETECT,
                candidates=decision.candidates,
                chosen=decision.chosen,
                reason=decision.reason,
                at=NOW,
            )
        )
        assert decision.chosen is not None
        await ledger.add_row(row(decision.chosen, i=i, run_id=f"run-{i}"))
    return out


async def test_exploration_quota_is_deterministic() -> None:
    first = await simulate(12)
    assert first == await simulate(12)
    explored = [i + 1 for i, (_, reason) in enumerate(first) if reason == EXPLORATION]
    assert explored == [4, 8, 12]
    # Each exploration pick is the least-tried candidate at that moment: PRECISE here.
    assert {first[i - 1][0] for i in explored} == {PRECISE}


async def test_no_exploration_once_every_candidate_has_min_trials() -> None:
    ledger = InMemoryLedgerRepository()
    ledger.rows = history(FAST, 5, 0) + history(PRECISE, 5, 0)
    ledger.selections = [
        SelectionRecord(
            run_id=f"r{i}",
            subtask_id="pii",
            skill=SkillId.PII_DETECT,
            candidates=(),
            chosen=FAST,
            reason="x",
            at=NOW,
        )
        for i in range(3)
    ]  # the next decision is the 4th
    decision = await choose(
        MeasuredPolicy(MeasuredConfig(min_trials=5, explore_every=4), PRIORS, ledger)
    )
    assert decision.reason != EXPLORATION


# --- through the broker ------------------------------------------------------------------------


@pytest.fixture
async def beds() -> AsyncIterator[list[BrokerBed]]:
    out: list[BrokerBed] = []
    yield out
    for bed in out:
        await bed.aclose()


async def test_spotcheck_failure_falls_back_and_is_ledgered(beds: list[BrokerBed]) -> None:
    agents = one_agent_per_skill()
    agents.append(FakeAgent("b-pii", make_party("vendor-y.local"), (SkillId.PII_DETECT,)))

    async def drops_a_card(req: ParsedRequest) -> Outcome:
        findings = [f for f in TRUTH["pii"]["findings"] if f["kind"] != "CARD"]
        return Outcome(output={"findings": findings})

    agents[2].behaviour = drops_a_card  # a-pii, ranked first
    bed = await BrokerBed(agents).setup()
    beds.append(bed)
    report = await bed.broker().start(intake(), run_id="run-sc")
    assert report.status is RunStatus.SUCCEEDED
    pii = [
        (r.agent_id, r.state, r.spot_check_passed) for r in bed.ledger.rows if r.subtask_id == "pii"
    ]
    assert pii == [
        ("a-pii@vendor-x.local", DelegationState.FAILED_SPOTCHECK, False),
        ("b-pii@vendor-y.local", DelegationState.COMPLETED, True),
    ]
    assert len(bed.ledger.rows) == 6
    assert all(r.price_table_version for r in bed.ledger.rows)


async def test_selection_decisions_persisted_with_terms(beds: list[BrokerBed]) -> None:
    bed = await BrokerBed(one_agent_per_skill(), measured=MeasuredConfig()).setup()
    beds.append(bed)
    report = await bed.broker().start(intake(), run_id="run-sel")
    assert report.status is RunStatus.SUCCEEDED
    selections = await bed.ledger.list_selections(run_id="run-sel")
    assert sorted(s.skill.value for s in selections) == sorted(s.value for s in SkillId)
    for s in selections:
        assert s.chosen is not None and len(s.candidates) == 1
        assert s.candidates[0].terms.cost_norm is not None
    assert {r.subtask_id for r in bed.ledger.rows} == {"profile", "stats", "pii", "units", "report"}
