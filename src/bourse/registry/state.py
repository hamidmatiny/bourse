"""Trust state machine (ADR-0013). Only listed transitions are legal; each writes an audit row."""

from datetime import UTC, datetime

from bourse.contracts.approval import ApprovalDecision
from bourse.contracts.registry import StateTransition
from bourse.contracts.trust import TrustState
from bourse.registry.repo import Repository

ALLOWED: dict[TrustState, frozenset[TrustState]] = {
    TrustState.PENDING: frozenset({TrustState.ACTIVE, TrustState.REVOKED}),
    TrustState.ACTIVE: frozenset({TrustState.QUARANTINED, TrustState.REVOKED}),
    TrustState.QUARANTINED: frozenset({TrustState.ACTIVE, TrustState.REVOKED}),
    TrustState.REVOKED: frozenset(),
}


class IllegalTransition(ValueError):
    pass


class UnknownAgent(LookupError):
    pass


def check_transition(
    src: TrustState, dst: TrustState, approval: ApprovalDecision | None = None
) -> None:
    if dst not in ALLOWED[src]:
        raise IllegalTransition(f"{src.value} -> {dst.value} is not allowed")
    reinstate = src is TrustState.QUARANTINED and dst is TrustState.ACTIVE
    if reinstate and (approval is None or not approval.approved):
        raise IllegalTransition("QUARANTINED -> ACTIVE requires an approved ApprovalDecision")


async def transition(
    repo: Repository,
    agent_id: str,
    to_state: TrustState,
    *,
    actor: str,
    reason: str,
    approval: ApprovalDecision | None = None,
    now: datetime | None = None,
) -> StateTransition:
    agent = await repo.get_agent(agent_id)
    if agent is None:
        raise UnknownAgent(agent_id)
    check_transition(agent.state, to_state, approval)
    at = now or datetime.now(UTC)
    record = StateTransition(
        agent_id=agent_id,
        from_state=agent.state,
        to_state=to_state,
        actor=actor,
        reason=reason,
        approval=approval,
        at=at,
    )
    await repo.save_transition(
        agent.model_copy(update={"state": to_state, "updated_at": at}), record
    )
    return record
