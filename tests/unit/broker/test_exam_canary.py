"""Admission exam (pass, fail, cooldown, registry hook) and continuous canaries."""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from bourse.agents.common.executor import ParsedRequest
from bourse.broker.canary import CANARY_FAIL
from bourse.broker.reliability import stats_by_agent
from bourse.contracts.registry import RegisterRequest
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import TrustState
from bourse.trust.exam import EXAM_FAILED, ExamConfig, ExamRunner, ExamScheduler
from bourse.trust.honeytokens import InMemoryHoneytokenRepository
from tests.broker_harness import (
    BrokerBed,
    FakeAgent,
    Outcome,
    honest,
    intake,
    one_agent_per_skill,
)
from tests.registry_helpers import make_party

TASK_ID = re.compile(r"^run-\d{8}T\d{6}Z-[0-9a-f]{8}:[a-z]+:1$")
SECRET = b"exam-test-secret"


@pytest.fixture
async def beds() -> AsyncIterator[list[BrokerBed]]:
    out: list[BrokerBed] = []
    yield out
    for bed in out:
        await bed.aclose()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def pii_agent(behaviour: object) -> FakeAgent:
    return FakeAgent("x-pii", make_party("vendor-x.local"), (SkillId.PII_DETECT,), behaviour)  # type: ignore[arg-type]


async def wrong(req: ParsedRequest) -> Outcome:
    return Outcome(output={"findings": []})


def runner(bed: BrokerBed, clock: Clock, tokens: InMemoryHoneytokenRepository) -> ExamRunner:
    return ExamRunner(
        bed.registry_service.repo,
        bed.http,
        tokens,
        SECRET,
        ExamConfig(k=5, pass_n=4, cooldown_s=600, deadline_s=10),
        clock=clock,
    )


async def test_exam_pass_activates_with_audit_reason(beds: list[BrokerBed]) -> None:
    agent = pii_agent(honest)
    bed = await BrokerBed([agent], activate=False).setup()
    beds.append(bed)
    tokens = InMemoryHoneytokenRepository()
    result = await runner(bed, Clock(), tokens).run(agent.agent_id)
    assert result is not None and result.passed and (result.n, result.total) == (5, 5)
    record = await bed.registry_service.repo.get_agent(agent.agent_id)
    assert record is not None and record.state is TrustState.ACTIVE
    last = (await bed.registry_service.repo.list_transitions(agent.agent_id))[-1]
    assert (last.actor, last.reason) == ("exam", "admission exam passed 5/5")
    requests = bed.executors["x-pii"].requests
    assert len(requests) == 5 and all(TASK_ID.match(r.task_id) for r in requests)
    assert len({r.task_id for r in requests}) == 5
    assert len(tokens.tokens) == 5  # one honeytoken per exam task
    for token, req in zip(tokens.tokens, requests, strict=True):
        assert token.email in req.payload.dataset.csv_text


async def test_exam_fail_stays_pending_and_cooldown(beds: list[BrokerBed]) -> None:
    agent = pii_agent(wrong)
    bed = await BrokerBed([agent], activate=False).setup()
    beds.append(bed)
    clock = Clock()
    exam = runner(bed, clock, InMemoryHoneytokenRepository())
    result = await exam.run(agent.agent_id)
    assert result is not None and not result.passed and result.n == 0
    repo = bed.registry_service.repo
    record = await repo.get_agent(agent.agent_id)
    assert record is not None and record.state is TrustState.PENDING
    assert [v.kind for v in await repo.list_violations(agent.agent_id)] == [EXAM_FAILED]
    assert await exam.run(agent.agent_id) is None  # cooldown: nothing sent
    assert len(bed.executors["x-pii"].requests) == 5
    clock.now += timedelta(seconds=601)
    assert await exam.run(agent.agent_id) is not None
    assert len(bed.executors["x-pii"].requests) == 10


async def test_exam_needs_4_of_5_per_skill(beds: list[BrokerBed]) -> None:
    calls = 0

    async def flaky(req: ParsedRequest) -> Outcome:
        nonlocal calls
        calls += 1
        return await (wrong(req) if calls in (2, 4) else honest(req))

    agent = pii_agent(flaky)
    bed = await BrokerBed([agent], activate=False).setup()
    beds.append(bed)
    result = await runner(bed, Clock(), InMemoryHoneytokenRepository()).run(agent.agent_id)
    assert result is not None and (result.passed, result.n) == (False, 3)


async def test_registration_starts_exam_without_manual_activation(beds: list[BrokerBed]) -> None:
    agent = pii_agent(honest)
    bed = await BrokerBed([agent], activate=False, register_agents=False).setup()
    beds.append(bed)
    # Register through the service with the exam hook, as `bourse registry serve` wires it.
    scheduler = ExamScheduler(runner(bed, Clock(), InMemoryHoneytokenRepository()))
    bed.registry_service.on_accepted = scheduler.schedule
    outcome = await bed.registry_service.register(
        RegisterRequest(vendor_domain=agent.party.domain, card_url=agent.card_url)
    )
    assert outcome.accepted
    await scheduler.wait()
    record = await bed.registry_service.repo.get_agent(agent.agent_id)
    assert record is not None and record.state is TrustState.ACTIVE


async def canary_bed(beds: list[BrokerBed], behaviour: object, runs: int) -> BrokerBed:
    agents = one_agent_per_skill()
    for a in agents:
        a.behaviour = behaviour  # type: ignore[assignment]
    bed = await BrokerBed(agents, canary_every=2).setup()
    beds.append(bed)
    for _ in range(runs):
        await bed.broker().start(intake())  # default run ids: run-<utc>-<hex8>, like canaries
    return bed


async def test_canary_schedule_is_deterministic(beds: list[BrokerBed]) -> None:
    sequences = []
    for _ in range(2):
        bed = await canary_bed(beds, honest, runs=4)
        sequences.append([(r.agent_id, r.skill.value, r.kind) for r in bed.ledger.rows])
        canaries = [r for r in bed.ledger.rows if r.kind == "canary"]
        assert len(canaries) == 10  # 4 real per (agent, skill) / every 2 = 2, x 5 skills
        assert all(r.spot_check_passed for r in canaries)
        for name, executor in bed.executors.items():
            assert len(executor.requests) == 6, name  # 4 real + 2 canaries, same format
            assert all(TASK_ID.match(r.task_id) for r in executor.requests)
    assert sequences[0] == sequences[1]


async def test_canaries_count_in_reliability(beds: list[BrokerBed]) -> None:
    bed = await canary_bed(beds, honest, runs=2)
    agent = "a-pii@vendor-x.local"
    stats = stats_by_agent(bed.ledger.rows, SkillId.PII_DETECT, {agent: 1}, 0.3)[agent]
    assert (stats.s, stats.f) == (3, 0)  # 2 real + 1 canary


async def test_two_consecutive_canary_failures_record_violation(beds: list[BrokerBed]) -> None:
    # `complete` returns the people fixture's answers for any input: right on real tasks,
    # wrong on canary datasets.
    from tests.broker_harness import complete

    bed = await canary_bed(beds, complete, runs=4)
    repo = bed.registry_service.repo
    for agent in bed.agents:
        kinds = [v.kind for v in await repo.list_violations(agent.agent_id)]
        assert kinds.count(CANARY_FAIL) == 1, agent.agent_id
    canaries = [r for r in bed.ledger.rows if r.kind == "canary"]
    assert len(canaries) == 10 and not any(r.spot_check_passed for r in canaries)
