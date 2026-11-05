"""Trigger wiring: HIGH_REP, PAID_CALL, ABORT_NOTICE (ADR-0048)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from bourse.approval.triggers import is_high_rep_hard, paid_call_needs_approval
from bourse.contracts.approval import ApprovalDecision, ApprovalRequest
from bourse.contracts.metering import BudgetSpec, Usage
from bourse.contracts.trust import Severity, TrustState, Violation
from bourse.llm.base import ChatMessage, ChatResult
from bourse.llm.paid import PaidDisabled
from bourse.llm.router import Router
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.budget import BudgetLedger
from bourse.trust.engine import TrustEngine

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class RecordingApprover:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.requests: list[ApprovalRequest] = []

    async def request(self, req: ApprovalRequest, timeout_s: float) -> ApprovalDecision:
        self.requests.append(req)
        return ApprovalDecision(
            approved=self.approved,
            by="rec",
            at=NOW,
            reason="ok" if self.approved else "no",
        )


class FakePaidProvider:
    name = "paid-fake"
    paid = True

    def price(self, tokens_in: int, tokens_out: int) -> Decimal:
        return Decimal("6.00")  # > 50% of usd_cap=10

    async def chat(
        self, messages: Any, tools: Any, max_tokens: int, deadline_s: float
    ) -> ChatResult:
        return ChatResult(
            text="ok",
            usage=Usage(tokens_in=1, tokens_out=1, wall_ms=1.0, model="m", provider="paid-fake"),
            usd=Decimal("0.01"),
        )


def test_paid_threshold() -> None:
    assert paid_call_needs_approval(
        projected_usd=Decimal("5.01"), usd_cap=Decimal("10"), allow_paid=True
    )
    assert not paid_call_needs_approval(
        projected_usd=Decimal("5"), usd_cap=Decimal("10"), allow_paid=True
    )
    assert not paid_call_needs_approval(
        projected_usd=Decimal("9"), usd_cap=Decimal("10"), allow_paid=False
    )


def test_high_rep_predicate() -> None:
    v = Violation(kind="EGRESS_ATTEMPT", severity=Severity.HARD, agent_id="a", evidence="e", at=NOW)
    assert is_high_rep_hard(v, 0.8)
    assert not is_high_rep_hard(v, 0.79)
    soft = v.model_copy(update={"severity": Severity.SOFT})
    assert not is_high_rep_hard(soft, 0.99)


async def test_high_rep_notify_on_hard() -> None:
    from tests.broker_harness import BrokerBed, honest
    from tests.unit.broker.test_exam_canary import pii_agent

    agent = pii_agent(honest)
    bed = await BrokerBed([agent]).setup()
    approver = RecordingApprover()
    try:
        engine = TrustEngine(
            bed.registry_service.repo,
            clock=lambda: NOW,
            approver=approver,
            wilson_lb_for=lambda _aid: 0.91,
        )
        state = await engine.ingest(
            Violation(
                kind="EGRESS_ATTEMPT",
                severity=Severity.HARD,
                agent_id=agent.agent_id,
                evidence="e",
                at=NOW,
            ),
            attributed=True,
        )
        assert state is TrustState.QUARANTINED
        assert len(approver.requests) == 1
        assert approver.requests[0].trigger.value == "HIGH_REP_VIOLATION"
    finally:
        await bed.aclose()


async def test_paid_call_denied_by_approver() -> None:
    approver = RecordingApprover(approved=False)
    spec = BudgetSpec(
        max_tokens_per_call=100,
        max_tokens_run=1000,
        wall_budget_s=60.0,
        usd_cap=Decimal("10"),
        allow_paid=True,
    )
    router = Router(
        [FakePaidProvider()],
        BudgetLedger(spec),
        CircuitBreaker(),
        approver=approver,
        paid_timeout_s=1.0,
    )
    try:
        await router.chat([ChatMessage(role="user", content="hi")], [], 50, 5.0)
        raise AssertionError("expected PaidDisabled")
    except PaidDisabled:
        pass
    assert len(approver.requests) == 1
    assert approver.requests[0].trigger.value == "PAID_CALL"
