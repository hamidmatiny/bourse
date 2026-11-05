"""Trust engine thresholds, window, HARD immediate, race, in-flight cancel."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from bourse.contracts.trust import Severity, TrustState, Violation
from bourse.registry.state import IllegalTransition, transition
from bourse.trust.engine import TrustEngine

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _clock(at: list[datetime]) -> Callable[[], datetime]:
    def clock() -> datetime:
        return at[0]

    return clock


def _v(agent_id: str, kind: str, severity: Severity, at: datetime) -> Violation:
    return Violation(kind=kind, severity=severity, agent_id=agent_id, evidence="e", at=at)


async def test_hard_attributed_quarantines_immediately() -> None:
    from tests.broker_harness import BrokerBed, honest
    from tests.unit.broker.test_exam_canary import pii_agent

    agent = pii_agent(honest)
    bed = await BrokerBed([agent]).setup()
    try:
        engine = TrustEngine(bed.registry_service.repo, k=3, window_s=86400, clock=lambda: NOW)
        state = await engine.ingest(
            _v(agent.agent_id, "EGRESS_ATTEMPT", Severity.HARD, NOW), attributed=True
        )
        assert state is TrustState.QUARANTINED
        live = await bed.registry_service.repo.get_agent(agent.agent_id)
        assert live is not None and live.state is TrustState.QUARANTINED
    finally:
        await bed.aclose()


async def test_soft_strikes_then_quarantine_window() -> None:
    from tests.broker_harness import BrokerBed, honest
    from tests.unit.broker.test_exam_canary import pii_agent

    agent = pii_agent(honest)
    bed = await BrokerBed([agent]).setup()
    at = [NOW]
    engine = TrustEngine(bed.registry_service.repo, k=3, window_s=86400, clock=_clock(at))
    try:
        s1 = await engine.ingest(
            _v(agent.agent_id, "SESSION_SMUGGLING", Severity.SOFT, NOW), attributed=True
        )
        at[0] = NOW + timedelta(hours=1)
        s2 = await engine.ingest(
            _v(agent.agent_id, "USAGE_MISREPORT", Severity.SOFT, at[0]), attributed=True
        )
        assert s1 is TrustState.ACTIVE and s2 is TrustState.ACTIVE
        at[0] = NOW + timedelta(hours=2)
        s3 = await engine.ingest(
            _v(agent.agent_id, "SPOTCHECK_FAIL", Severity.SOFT, at[0]), attributed=True
        )
        assert s3 is TrustState.QUARANTINED
    finally:
        await bed.aclose()


async def test_soft_outside_window_does_not_count() -> None:
    from tests.broker_harness import BrokerBed, honest
    from tests.unit.broker.test_exam_canary import pii_agent

    agent = pii_agent(honest)
    bed = await BrokerBed([agent]).setup()
    at = [NOW]
    engine = TrustEngine(bed.registry_service.repo, k=3, window_s=3600, clock=_clock(at))
    try:
        await engine.ingest(_v(agent.agent_id, "A", Severity.SOFT, NOW), attributed=True)
        await engine.ingest(
            _v(agent.agent_id, "B", Severity.SOFT, NOW + timedelta(minutes=1)), attributed=True
        )
        at[0] = NOW + timedelta(hours=2)
        state = await engine.ingest(_v(agent.agent_id, "C", Severity.SOFT, at[0]), attributed=True)
        assert state is TrustState.ACTIVE
    finally:
        await bed.aclose()


async def test_unattributed_hard_does_not_quarantine() -> None:
    from tests.broker_harness import BrokerBed, honest
    from tests.unit.broker.test_exam_canary import pii_agent

    agent = pii_agent(honest)
    bed = await BrokerBed([agent]).setup()
    engine = TrustEngine(bed.registry_service.repo, clock=lambda: NOW)
    try:
        state = await engine.ingest(
            _v(agent.agent_id, "TAMPERED", Severity.HARD, NOW), attributed=False
        )
        assert state is TrustState.ACTIVE
    finally:
        await bed.aclose()


async def test_quarantine_between_choose_and_delegate_sends_no_message() -> None:
    from bourse.broker.delegate import Delegator
    from bourse.contracts.skills import SkillId
    from bourse.contracts.tasks import Subtask
    from tests.broker_harness import BrokerBed, honest
    from tests.unit.broker.test_exam_canary import pii_agent

    agent = pii_agent(honest)
    bed = await BrokerBed([agent]).setup()
    try:
        engine = TrustEngine(bed.registry_service.repo, clock=lambda: NOW)
        await engine.ingest(
            _v(agent.agent_id, "EGRESS_ATTEMPT", Severity.HARD, NOW), attributed=True
        )
        from bourse.broker.delegate import RegistryClient
        from tests.broker_harness import ADMIN, REGISTRY

        delegator = Delegator(
            RegistryClient(bed.http, REGISTRY, ADMIN),
            bed.http,
            bed.runs,
            bed.breaker,
            deadline_s=5.0,
        )
        record, _output, _d, _s = await delegator.attempt(
            "run-1",
            Subtask(id="pii", skill=SkillId.PII_DETECT, input_ref="dataset"),
            (await bed.registry_service.repo.get_agent(agent.agent_id)),  # type: ignore[arg-type]
            1,
            __import__("bourse.contracts.skills_io", fromlist=["PiiIn"]).PiiIn(
                dataset=__import__(
                    "bourse.contracts.skills_io", fromlist=["DatasetRef"]
                ).DatasetRef.from_csv("a\n1\n")
            ),
        )
        from bourse.contracts.broker import DelegationState

        assert record.state is DelegationState.CANCELED_QUARANTINE
        assert bed.executors[agent.name].requests == []
    finally:
        await bed.aclose()


async def test_inflight_cancel_on_quarantine() -> None:
    from typing import cast

    import httpx
    from a2a.types import AgentCard

    from bourse.broker.inflight import InFlight, InFlightHandle

    canceled: list[str] = []

    async def fake_cancel(card: AgentCard, tid: str, http: httpx.AsyncClient) -> None:
        canceled.append(tid)

    inflight = InFlight(cancel=fake_cancel)
    card = cast(AgentCard, object())
    http = cast(httpx.AsyncClient, object())
    inflight.add(InFlightHandle("a@v.local", card, "t-1", http))
    inflight.add(InFlightHandle("a@v.local", card, "t-2", http))
    n = await inflight.cancel_agent("a@v.local")
    assert n == 2 and canceled == ["t-1", "t-2"]
    assert await inflight.cancel_agent("a@v.local") == 0


async def test_cancel_after_completed_is_best_effort() -> None:
    """Quarantine re-check after the agent finished: cancel must not raise."""
    from typing import cast

    import httpx
    from a2a.types import AgentCard, TaskNotCancelableError, TaskNotFoundError

    from bourse.broker.inflight import InFlight, InFlightHandle

    async def already_done(card: AgentCard, tid: str, http: httpx.AsyncClient) -> None:
        raise TaskNotCancelableError(message="Task cannot be canceled")

    async def missing(card: AgentCard, tid: str, http: httpx.AsyncClient) -> None:
        raise TaskNotFoundError(message="Task not found")

    card = cast(AgentCard, object())
    http = cast(httpx.AsyncClient, object())
    for cancel in (already_done, missing):
        inflight = InFlight(cancel=cancel)
        handle = InFlightHandle("a@v.local", card, "t-done", http)
        inflight.add(handle)
        await inflight.cancel_handle(handle)  # must not raise
        assert await inflight.cancel_agent("a@v.local") == 0


async def test_quarantine_recheck_after_completed_continues() -> None:
    """Live re-check finds QUARANTINED after COMPLETED; cancel fails; record CANCELED_QUARANTINE."""
    import httpx
    from a2a.types import AgentCard, TaskNotCancelableError

    from bourse.broker.delegate import Delegator, RegistryClient
    from bourse.broker.inflight import InFlight
    from bourse.contracts.broker import DelegationState
    from bourse.contracts.skills import SkillId
    from bourse.contracts.skills_io import DatasetRef, PiiIn
    from bourse.contracts.tasks import Subtask
    from bourse.contracts.trust import TrustState
    from bourse.registry.state import transition
    from tests.broker_harness import ADMIN, REGISTRY, BrokerBed, honest
    from tests.unit.broker.test_exam_canary import pii_agent

    agent = pii_agent(honest)
    bed = await BrokerBed([agent]).setup()
    try:

        async def boom_cancel(card: AgentCard, tid: str, http: httpx.AsyncClient) -> None:
            raise TaskNotCancelableError(message="Task cannot be canceled")

        inflight = InFlight(cancel=boom_cancel)
        delegator = Delegator(
            RegistryClient(bed.http, REGISTRY, ADMIN),
            bed.http,
            bed.runs,
            bed.breaker,
            deadline_s=5.0,
            inflight=inflight,
        )

        class Done:
            state = "TASK_STATE_COMPLETED"
            output = {"findings": []}
            error = ""
            error_kind = None
            data_parts: list[object] = []
            text_parts: list[object] = []
            extra_parts: list[object] = []
            artifact_metadata: dict[str, object] = {}

        async def fake_send(*args: object, **kwargs: object) -> Done:
            on_task = kwargs.get("on_task")
            if callable(on_task):
                on_task("a2a-already-done")
            # Quarantine after the task completed (re-check path).
            await transition(
                bed.registry_service.repo,
                agent.agent_id,
                TrustState.QUARANTINED,
                actor="test",
                reason="recheck after complete",
            )
            return Done()

        active_snap = await bed.registry_service.repo.get_agent(agent.agent_id)
        assert active_snap is not None and active_snap.state is TrustState.ACTIVE

        import bourse.broker.delegate as delegate_mod

        original = delegate_mod.send_task
        delegate_mod.send_task = fake_send  # type: ignore[assignment]
        try:
            record, _o, _d, _s = await delegator.attempt(
                "run-q",
                Subtask(id="pii", skill=SkillId.PII_DETECT, input_ref="dataset"),
                active_snap,
                1,
                PiiIn(dataset=DatasetRef.from_csv("a\n1\n")),
            )
        finally:
            delegate_mod.send_task = original  # type: ignore[assignment]
        assert record.state is DelegationState.CANCELED_QUARANTINE
    finally:
        await bed.aclose()


async def test_reinstate_requires_approved_decision() -> None:
    from bourse.contracts.approval import ApprovalDecision
    from tests.broker_harness import BrokerBed, honest
    from tests.unit.broker.test_exam_canary import pii_agent

    agent = pii_agent(honest)
    bed = await BrokerBed([agent]).setup()
    engine = TrustEngine(bed.registry_service.repo, clock=lambda: NOW)
    try:
        await engine.ingest(
            _v(agent.agent_id, "EGRESS_ATTEMPT", Severity.HARD, NOW), attributed=True
        )
        try:
            await transition(
                bed.registry_service.repo,
                agent.agent_id,
                TrustState.ACTIVE,
                actor="admin",
                reason="nope",
            )
            raise AssertionError("expected IllegalTransition")
        except IllegalTransition:
            pass
        await transition(
            bed.registry_service.repo,
            agent.agent_id,
            TrustState.ACTIVE,
            actor="admin",
            reason="ok",
            approval=ApprovalDecision(approved=True, by="op", at=NOW, reason="ok"),
        )
        live = await bed.registry_service.repo.get_agent(agent.agent_id)
        assert live is not None and live.state is TrustState.ACTIVE
    finally:
        await bed.aclose()
