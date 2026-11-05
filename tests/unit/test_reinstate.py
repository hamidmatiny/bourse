"""Reinstatement flow unit tests (ADR-0048)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bourse.approval.deny import DenyApprover
from bourse.contracts.approval import ApprovalDecision
from bourse.contracts.exam import ExamReport
from bourse.contracts.registry import AgentRecord, StateTransition
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import TrustState
from bourse.registry.repo_memory import InMemoryRepository
from bourse.trust.reinstate import reinstate_agent
from bourse.trust.reliability_epoch import bump_rel_epoch, effective_agent_major, reliability_epoch

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class FakeExamRunner:
    def __init__(self, report: ExamReport | None) -> None:
        self.report = report
        self.calls: list[str] = []

    async def run_for_reinstate(self, agent_id: str) -> ExamReport | None:
        self.calls.append(agent_id)
        return self.report


class ScriptedApprover:
    def __init__(self, approved: bool, reason: str = "scripted") -> None:
        self.approved = approved
        self.reason = reason
        self.requests: list[Any] = []

    async def request(self, req: Any, timeout_s: float) -> ApprovalDecision:
        self.requests.append(req)
        return ApprovalDecision(approved=self.approved, by="U_TEST", at=NOW, reason=self.reason)


async def _seed(state: TrustState, agent_id: str = "a1") -> InMemoryRepository:
    repo = InMemoryRepository()
    agent = AgentRecord(
        agent_id=agent_id,
        vendor_domain="vendor-x.local",
        name="sleeper",
        card_url=None,
        state=state,
        current_version="1.0.0",
        current_digest="0" * 64,
        skills=(SkillId.PII_DETECT,),
        tags=(),
        created_at=NOW,
        updated_at=NOW,
    )
    await repo.save_transition(
        agent,
        StateTransition(
            agent_id=agent_id,
            from_state=state,
            to_state=state,
            actor="seed",
            reason="seed",
            at=NOW,
        ),
    )
    return repo


def _exam(passed: bool, agent_id: str = "a1") -> ExamReport:
    return ExamReport(
        agent_id=agent_id,
        model="m",
        passed=passed,
        per_skill={"pii.detect": 5 if passed else 0},
        n=5 if passed else 0,
        total=5,
        tasks=(),
    )


async def test_reinstate_pass() -> None:
    repo = await _seed(TrustState.QUARANTINED)
    result = await reinstate_agent(
        repo,
        FakeExamRunner(_exam(True)),
        ScriptedApprover(True),
        "a1",
        reason="ops ok",
        timeout_s=1.0,
        clock=lambda: NOW,
    )
    assert result.status == "active"
    live = await repo.get_agent("a1")
    assert live is not None
    assert live.state is TrustState.ACTIVE
    assert reliability_epoch(live.tags) == 1
    assert effective_agent_major(live.current_version, live.tags) == 10_001
    assert result.decision is not None and result.decision.approved is True
    assert result.exam is not None and result.exam.n == 5


async def test_reinstate_deny_stays_quarantined() -> None:
    repo = await _seed(TrustState.QUARANTINED)
    result = await reinstate_agent(
        repo,
        FakeExamRunner(_exam(True)),
        ScriptedApprover(False, reason="operator denied"),
        "a1",
        reason="try",
        timeout_s=1.0,
        clock=lambda: NOW,
    )
    assert result.status == "denied"
    live = await repo.get_agent("a1")
    assert live is not None and live.state is TrustState.QUARANTINED


async def test_reinstate_exam_fail() -> None:
    repo = await _seed(TrustState.QUARANTINED)
    result = await reinstate_agent(
        repo,
        FakeExamRunner(_exam(False)),
        ScriptedApprover(True),
        "a1",
        reason="try",
        timeout_s=1.0,
        clock=lambda: NOW,
    )
    assert result.status == "exam_failed"
    live = await repo.get_agent("a1")
    assert live is not None and live.state is TrustState.QUARANTINED


async def test_reinstate_revoked_never() -> None:
    repo = await _seed(TrustState.REVOKED)
    result = await reinstate_agent(
        repo,
        FakeExamRunner(_exam(True)),
        DenyApprover(),
        "a1",
        reason="nope",
        timeout_s=1.0,
        clock=lambda: NOW,
    )
    assert result.status == "revoked"
    live = await repo.get_agent("a1")
    assert live is not None and live.state is TrustState.REVOKED


def test_bump_rel_epoch() -> None:
    tags = bump_rel_epoch(())
    assert tags == ("rel_epoch:1",)
    assert bump_rel_epoch(tags) == ("rel_epoch:2",)


async def test_reinstate_twice_epoch_2_isolates_old_scores() -> None:
    """Two reinstatements → rel_epoch 2; old-epoch ledger s/f do not mix into new score."""
    from datetime import UTC, datetime

    from bourse.broker.reliability import scores
    from bourse.contracts.broker import DelegationState, LedgerRow
    from bourse.registry.state import transition

    repo = await _seed(TrustState.QUARANTINED)
    r1 = await reinstate_agent(
        repo,
        FakeExamRunner(_exam(True)),
        ScriptedApprover(True),
        "a1",
        reason="first",
        timeout_s=1.0,
        clock=lambda: NOW,
    )
    assert r1.status == "active"
    live = await repo.get_agent("a1")
    assert live is not None and reliability_epoch(live.tags) == 1
    major1 = effective_agent_major(live.current_version, live.tags)
    assert major1 == 10_001

    await transition(
        repo,
        "a1",
        TrustState.QUARANTINED,
        actor="test",
        reason="re-quarantine",
    )
    r2 = await reinstate_agent(
        repo,
        FakeExamRunner(_exam(True)),
        ScriptedApprover(True),
        "a1",
        reason="second",
        timeout_s=1.0,
        clock=lambda: NOW,
    )
    assert r2.status == "active"
    live2 = await repo.get_agent("a1")
    assert live2 is not None and reliability_epoch(live2.tags) == 2
    major2 = effective_agent_major(live2.current_version, live2.tags)
    assert major2 == 20_001

    now = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)

    def _row(major: int, *, ok: bool) -> LedgerRow:
        return LedgerRow(
            run_id="r",
            subtask_id="s",
            attempt=1,
            agent_id="a1",
            skill=SkillId.PII_DETECT,
            revision_digest="0" * 64,
            agent_major=major,
            state=DelegationState.COMPLETED if ok else DelegationState.FAILED,
            error_kind=None,
            tokens_in=1,
            tokens_out=1,
            tool_calls=0,
            shadow_usd=0.0,
            price_table_version="v",
            wall_ms=10.0,
            spot_check_passed=bool(ok),
            at=now,
            kind="task",
        )

    # Old epoch: 3 success; new epoch: 1 fail only — scores must stay separate.
    sc = scores(
        [
            _row(major1, ok=True),
            _row(major1, ok=True),
            _row(major1, ok=True),
            _row(major2, ok=False),
        ],
        alpha=0.3,
    )
    by_major = {s.agent_major: s for s in sc if s.agent_id == "a1"}
    assert by_major[major1].s == 3 and by_major[major1].f == 0
    assert by_major[major2].s == 0 and by_major[major2].f == 1


async def test_deny_approver_path_on_reinstate() -> None:
    """Default DenyApprover keeps QUARANTINED (ADR-0039 / 0048)."""
    repo = await _seed(TrustState.QUARANTINED)
    result = await reinstate_agent(
        repo,
        FakeExamRunner(_exam(True)),
        DenyApprover(),
        "a1",
        reason="ci",
        timeout_s=1.0,
        clock=lambda: NOW,
    )
    assert result.status == "denied"
    live = await repo.get_agent("a1")
    assert live is not None and live.state is TrustState.QUARANTINED


# --- strikes per (agent, rel_epoch) (ADR-0039) ----------------------------------------------


def _vio(kind: str, severity: Any, at: datetime, agent_id: str = "a1") -> Any:
    from bourse.contracts.trust import Violation

    return Violation(kind=kind, severity=severity, agent_id=agent_id, evidence="e", at=at)


async def _reinstate(repo: InMemoryRepository, at: datetime) -> None:
    result = await reinstate_agent(
        repo,
        FakeExamRunner(_exam(True)),
        ScriptedApprover(True),
        "a1",
        reason="strike epoch test",
        timeout_s=1.0,
        clock=lambda: at,
    )
    assert result.status == "active"


async def test_two_strikes_then_reinstate_then_one_strike_stays_active() -> None:
    from datetime import timedelta

    from bourse.contracts.trust import Severity
    from bourse.trust.engine import TrustEngine

    repo = await _seed(TrustState.ACTIVE)
    at = [NOW]
    engine = TrustEngine(repo, k=3, window_s=86400, clock=lambda: at[0])
    for i, kind in enumerate(("SPOTCHECK_FAIL", "SPOTCHECK_FAIL")):
        at[0] = NOW + timedelta(minutes=i)
        assert await engine.ingest(_vio(kind, Severity.SOFT, at[0]), attributed=True) is (
            TrustState.ACTIVE
        )
    # Quarantined by an unrelated HARD violation with 2 SOFT strikes on record.
    at[0] = NOW + timedelta(minutes=5)
    state = await engine.ingest(_vio("EGRESS_ATTEMPT", Severity.HARD, at[0]), attributed=True)
    assert state is TrustState.QUARANTINED
    at[0] = NOW + timedelta(minutes=10)
    await _reinstate(repo, at[0])
    # Epoch 1: one SOFT strike. Old-epoch count (2+1=3) would quarantine; epoch count is 1.
    at[0] = NOW + timedelta(minutes=11)
    state = await engine.ingest(_vio("SPOTCHECK_FAIL", Severity.SOFT, at[0]), attributed=True)
    assert state is TrustState.ACTIVE
    epochs = [v.rel_epoch for v in await repo.list_violations("a1")]
    assert epochs == [0, 0, 0, 1]


async def test_three_strikes_in_new_epoch_quarantines() -> None:
    from datetime import timedelta

    from bourse.contracts.trust import Severity
    from bourse.trust.engine import TrustEngine

    repo = await _seed(TrustState.ACTIVE)
    at = [NOW]
    engine = TrustEngine(repo, k=3, window_s=86400, clock=lambda: at[0])
    for i in range(3):
        at[0] = NOW + timedelta(minutes=i)
        state = await engine.ingest(_vio("SPOTCHECK_FAIL", Severity.SOFT, at[0]), attributed=True)
    assert state is TrustState.QUARANTINED
    at[0] = NOW + timedelta(minutes=10)
    await _reinstate(repo, at[0])
    states = []
    for i in range(3):
        at[0] = NOW + timedelta(minutes=20 + i)
        states.append(
            await engine.ingest(_vio("SPOTCHECK_FAIL", Severity.SOFT, at[0]), attributed=True)
        )
    assert states == [TrustState.ACTIVE, TrustState.ACTIVE, TrustState.QUARANTINED]
    live = await repo.get_agent("a1")
    assert live is not None and reliability_epoch(live.tags) == 1
    epoch1 = [v for v in await repo.list_violations("a1") if v.rel_epoch == 1]
    assert len(epoch1) == 3


async def test_hard_after_reinstate_quarantines_immediately() -> None:
    from datetime import timedelta

    from bourse.contracts.trust import Severity
    from bourse.trust.engine import TrustEngine

    repo = await _seed(TrustState.QUARANTINED)
    await _reinstate(repo, NOW)
    engine = TrustEngine(repo, k=3, window_s=86400, clock=lambda: NOW + timedelta(minutes=1))
    state = await engine.ingest(
        _vio("EGRESS_ATTEMPT", Severity.HARD, NOW + timedelta(minutes=1)), attributed=True
    )
    assert state is TrustState.QUARANTINED


async def test_explicit_rel_epoch_is_kept() -> None:
    from bourse.contracts.trust import Severity, Violation

    repo = await _seed(TrustState.ACTIVE)
    await repo.add_violation(
        Violation(
            kind="X", severity=Severity.SOFT, agent_id="a1", evidence="e", at=NOW, rel_epoch=4
        )
    )
    await repo.add_violation(_vio("Y", Severity.SOFT, NOW))
    assert [v.rel_epoch for v in await repo.list_violations("a1")] == [4, 0]
