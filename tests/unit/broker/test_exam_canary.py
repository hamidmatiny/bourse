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
from bourse.trust.exam import (
    EXAM_FAILED,
    EXAM_INCONCLUSIVE,
    INFRA,
    ExamConfig,
    ExamRunner,
    ExamScheduler,
    is_exam_infra_failure,
)
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


async def contention_fail(req: ParsedRequest) -> Outcome:
    return Outcome(
        kind="fail",
        error_kind="llm_unavailable",
        detail="llm_unavailable: AllProvidersFailed",
    )


def runner(
    bed: BrokerBed,
    clock: Clock,
    tokens: InMemoryHoneytokenRepository,
    config: ExamConfig | None = None,
) -> ExamRunner:
    return ExamRunner(
        bed.registry_service.repo,
        bed.http,
        tokens,
        SECRET,
        config or ExamConfig(k=5, pass_n=4, cooldown_s=600, deadline_s=10, infra_backoff_s=0.0),
        clock=clock,
    )


def test_is_exam_infra_failure_contention_and_llm_unavailable() -> None:
    assert is_exam_infra_failure(error_kind="llm_unavailable", error="AllProvidersFailed")
    assert is_exam_infra_failure(detail="contention: no upstream slot")
    assert is_exam_infra_failure(usage_contention=True)
    assert is_exam_infra_failure(
        error_kind="deadline",
        error="deadline: DeadlineExceeded",
        task_deadline_s=100.0,
        queue_wait_ms=60_000.0,
        contention_queue_fraction=0.5,
    )
    assert not is_exam_infra_failure(
        error_kind="deadline",
        error="deadline: DeadlineExceeded",
        task_deadline_s=100.0,
        queue_wait_ms=1_000.0,
        contention_queue_fraction=0.5,
    )
    assert not is_exam_infra_failure(error_kind="internal", detail="scripted failure")


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


async def test_exam_infra_contention_retries_then_inconclusive(beds: list[BrokerBed]) -> None:
    """Contention/llm_unavailable is INFRA: retry then EXAM_INCONCLUSIVE, never EXAM_FAILED."""
    agent = pii_agent(contention_fail)
    bed = await BrokerBed([agent], activate=False).setup()
    beds.append(bed)
    cfg = ExamConfig(
        k=1,
        pass_n=1,
        cooldown_s=600,
        deadline_s=10,
        infra_max_retries=2,
        infra_backoff_s=0.0,
    )
    result = await runner(bed, Clock(), InMemoryHoneytokenRepository(), cfg).run(agent.agent_id)
    assert result is not None
    assert not result.passed and result.inconclusive
    assert all(t.outcome == INFRA for t in result.tasks)
    assert result.tasks[0].infra_retries == 2
    # 1 slot x (1 + 2 retries) = 3 attempts
    assert len(bed.executors["x-pii"].requests) == 3
    kinds = [v.kind for v in await bed.registry_service.repo.list_violations(agent.agent_id)]
    assert EXAM_INCONCLUSIVE in kinds
    assert EXAM_FAILED not in kinds
    record = await bed.registry_service.repo.get_agent(agent.agent_id)
    assert record is not None and record.state is TrustState.PENDING


async def test_exam_grader_failure_still_exam_failed(beds: list[BrokerBed]) -> None:
    """Grader failures remain agent-attributable EXAM_FAILED (no INFRA path)."""
    agent = pii_agent(wrong)
    bed = await BrokerBed([agent], activate=False).setup()
    beds.append(bed)
    cfg = ExamConfig(k=2, pass_n=2, cooldown_s=600, deadline_s=10, infra_backoff_s=0.0)
    result = await runner(bed, Clock(), InMemoryHoneytokenRepository(), cfg).run(agent.agent_id)
    assert result is not None and not result.passed and not result.inconclusive
    assert all(t.outcome == "grader-failed" for t in result.tasks)
    assert len(bed.executors["x-pii"].requests) == 2  # no INFRA retries
    kinds = [v.kind for v in await bed.registry_service.repo.list_violations(agent.agent_id)]
    assert kinds == [EXAM_FAILED]


async def test_exam_scheduler_never_exceeds_concurrency(beds: list[BrokerBed]) -> None:
    import anyio

    async def slow(req: ParsedRequest) -> Outcome:
        await anyio.sleep(0.2)
        return await honest(req)

    agents = [
        FakeAgent(
            f"pii-{i}",
            make_party(f"vendor-{i}.local"),
            (SkillId.PII_DETECT,),
            slow,
        )
        for i in range(4)
    ]
    bed = await BrokerBed(agents, activate=False).setup()
    beds.append(bed)
    cfg = ExamConfig(k=1, pass_n=1, cooldown_s=600, deadline_s=30, infra_backoff_s=0.0)
    exam = runner(bed, Clock(), InMemoryHoneytokenRepository(), cfg)
    lines: list[str] = []
    scheduler = ExamScheduler(exam, max_concurrent=2, reschedule_delay_s=3600.0, echo=lines.append)
    for a in agents:
        assert scheduler.schedule(a.agent_id)
    await scheduler.wait()
    assert scheduler.peak_concurrent <= 2
    assert scheduler.peak_concurrent == 2
    assert any("exam queue:" in ln and "limit=2" in ln for ln in lines)
    for a in agents:
        rec = await bed.registry_service.repo.get_agent(a.agent_id)
        assert rec is not None and rec.state is TrustState.ACTIVE


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
    """CANARY_FAIL requires COMPLETED + grader-fail (not TIMEOUT/spot-fail).

    Drop PERSON_NAME only: spot check (EMAIL/CARD/SIN) still passes; grade F1 fails.
    """
    from tests.broker_harness import honest

    async def grade_fail_spot_ok(req: ParsedRequest) -> Outcome:
        out = await honest(req)
        if req.skill is SkillId.PII_DETECT and out.output is not None:
            findings = [f for f in out.output["findings"] if f["kind"] != "PERSON_NAME"]
            return Outcome(output={"findings": findings})
        # Non-PII: honest on real tasks; on canaries honest() uses the delegated csv so
        # both spot and grade pass — only the PII agent is expected to strike.
        return out

    bed = await canary_bed(beds, grade_fail_spot_ok, runs=4)
    repo = bed.registry_service.repo
    pii = next(a for a in bed.agents if a.name == "a-pii")
    kinds = [v.kind for v in await repo.list_violations(pii.agent_id)]
    assert kinds.count(CANARY_FAIL) == 1, (pii.agent_id, kinds)
    pii_canaries = [r for r in bed.ledger.rows if r.kind == "canary" and r.agent_id == pii.agent_id]
    assert len(pii_canaries) >= 2
    assert all(r.state.value == "COMPLETED" for r in pii_canaries)
    assert all(r.spot_check_passed is False for r in pii_canaries)


async def test_infra_canary_errors_do_not_emit_canary_fail(beds: list[BrokerBed]) -> None:
    """Spot-check failures on canaries are SPOTCHECK_FAIL / reliability f, never CANARY_FAIL."""
    from tests.broker_harness import complete

    bed = await canary_bed(beds, complete, runs=4)
    repo = bed.registry_service.repo
    for agent in bed.agents:
        kinds = [v.kind for v in await repo.list_violations(agent.agent_id)]
        assert CANARY_FAIL not in kinds, (agent.agent_id, kinds)
    canaries = [r for r in bed.ledger.rows if r.kind == "canary"]
    assert len(canaries) == 10
    assert not any(r.state.value == "COMPLETED" and r.spot_check_passed is False for r in canaries)


async def test_canary_counter_persistent_across_runs_m10() -> None:
    """25 task delegations over 5 runs, M=10 -> exactly 2 canaries (ledger-wide)."""
    from datetime import UTC, datetime

    from bourse.broker.canary import CanaryRunner
    from bourse.contracts.broker import DelegationState, LedgerRow
    from bourse.ledger.repo import InMemoryLedgerRepository

    ledger = InMemoryLedgerRepository()
    canary = CanaryRunner(
        ledger=ledger,
        recorder=None,  # type: ignore[arg-type]
        delegator=None,  # type: ignore[arg-type]
        registry=None,  # type: ignore[arg-type]
        tokens=None,  # type: ignore[arg-type]
        secret=b"x",
        every=10,
    )
    agent_id = "a@vendor-a.local"
    skill = SkillId.PII_DETECT
    now = datetime(2026, 11, 1, tzinfo=UTC)
    n_canaries = 0
    for i in range(25):
        run_id = f"run-{i // 5}"
        await ledger.add_row(
            LedgerRow(
                run_id=run_id,
                subtask_id="pii",
                attempt=1,
                agent_id=agent_id,
                skill=skill,
                revision_digest="0" * 64,
                agent_major=1,
                state=DelegationState.COMPLETED,
                tokens_in=0,
                tokens_out=0,
                tool_calls=0,
                shadow_usd=0.0,
                price_table_version="t",
                wall_ms=1.0,
                spot_check_passed=True,
                at=now,
                kind="task",
            )
        )
        if await canary.due(agent_id, skill):
            n_canaries += 1
            await ledger.add_row(
                LedgerRow(
                    run_id=f"canary-{n_canaries}",
                    subtask_id="pii",
                    attempt=1,
                    agent_id=agent_id,
                    skill=skill,
                    revision_digest="0" * 64,
                    agent_major=1,
                    state=DelegationState.COMPLETED,
                    tokens_in=0,
                    tokens_out=0,
                    tool_calls=0,
                    shadow_usd=0.0,
                    price_table_version="t",
                    wall_ms=1.0,
                    spot_check_passed=True,
                    at=now,
                    kind="canary",
                )
            )
    assert n_canaries == 2


async def test_delegator_logs_task_and_canary_deliveries_in_send_order(
    beds: list[BrokerBed],
) -> None:
    """Delivery log (phase5 exposure, ADR-0042): every A2A send, canaries included."""
    agents = one_agent_per_skill()
    bed = await BrokerBed(agents, canary_every=2).setup()
    beds.append(bed)
    broker = bed.broker()
    for _ in range(4):
        await broker.start(intake())
    log = broker.delegator.deliveries
    ids = {a.name: a.agent_id for a in agents}
    for name, executor in bed.executors.items():
        sent = [d for d in log if d.agent_id == ids[name]]
        assert [d.task_id for d in sent] == [r.task_id for r in executor.requests], name
        assert sum(d.kind == "canary" for d in sent) == 2, name
        assert sum(d.kind == "task" for d in sent) == 4, name
