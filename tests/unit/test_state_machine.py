import itertools
from datetime import UTC, datetime

import pytest

from bourse.contracts.approval import ApprovalDecision
from bourse.contracts.registry import AgentRecord, StateTransition
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import TrustState
from bourse.registry.repo_memory import InMemoryRepository
from bourse.registry.state import ALLOWED, IllegalTransition, UnknownAgent, transition

NOW = datetime(2026, 10, 30, tzinfo=UTC)
APPROVED = ApprovalDecision(approved=True, by="operator", at=NOW, reason="reviewed")
DENIED = ApprovalDecision(approved=False, by="operator", at=NOW, reason="timeout")

LEGAL = {
    (TrustState.PENDING, TrustState.ACTIVE),
    (TrustState.PENDING, TrustState.REVOKED),
    (TrustState.ACTIVE, TrustState.QUARANTINED),
    (TrustState.ACTIVE, TrustState.REVOKED),
    (TrustState.QUARANTINED, TrustState.ACTIVE),
    (TrustState.QUARANTINED, TrustState.REVOKED),
}
ALL_PAIRS = list(itertools.product(TrustState, TrustState))


def _agent(state: TrustState) -> AgentRecord:
    return AgentRecord(
        agent_id="a@vendor-a.local",
        vendor_domain="vendor-a.local",
        name="a",
        card_url=None,
        state=state,
        current_version="1.0.0",
        current_digest="0" * 64,
        skills=(SkillId.STATS_COMPUTE,),
        tags=(),
        created_at=NOW,
        updated_at=NOW,
    )


async def _repo_with(state: TrustState) -> InMemoryRepository:
    repo = InMemoryRepository()
    await repo.save_transition(
        _agent(state),
        # Seed row so the agent exists; tests count audit rows after this one.
        StateTransition(
            agent_id="a@vendor-a.local",
            from_state=state,
            to_state=state,
            actor="seed",
            reason="seed",
            at=NOW,
        ),
    )
    return repo


def test_table_matches_spec() -> None:
    assert {(s, d) for s, ds in ALLOWED.items() for d in ds} == LEGAL


@pytest.mark.parametrize(("src", "dst"), sorted(LEGAL))
async def test_legal_transitions_pass_and_audit(src: TrustState, dst: TrustState) -> None:
    repo = await _repo_with(src)
    approval = APPROVED if (src, dst) == (TrustState.QUARANTINED, TrustState.ACTIVE) else None
    row = await transition(
        repo, "a@vendor-a.local", dst, actor="admin", reason="test", approval=approval, now=NOW
    )
    assert (row.from_state, row.to_state) == (src, dst)
    agent = await repo.get_agent("a@vendor-a.local")
    assert agent is not None and agent.state is dst
    audit = await repo.list_transitions("a@vendor-a.local")
    assert audit[-1] == row and len(audit) == 2


@pytest.mark.parametrize(("src", "dst"), [p for p in ALL_PAIRS if p not in LEGAL])
async def test_illegal_transitions_raise(src: TrustState, dst: TrustState) -> None:
    repo = await _repo_with(src)
    with pytest.raises(IllegalTransition):
        await transition(
            repo, "a@vendor-a.local", dst, actor="admin", reason="x", approval=APPROVED
        )
    agent = await repo.get_agent("a@vendor-a.local")
    assert agent is not None and agent.state is src
    assert len(await repo.list_transitions("a@vendor-a.local")) == 1


@pytest.mark.parametrize("approval", [None, DENIED], ids=["no-decision", "denied"])
async def test_reinstate_requires_approved_decision(approval: ApprovalDecision | None) -> None:
    repo = await _repo_with(TrustState.QUARANTINED)
    with pytest.raises(IllegalTransition, match="approved"):
        await transition(
            repo,
            "a@vendor-a.local",
            TrustState.ACTIVE,
            actor="admin",
            reason="x",
            approval=approval,
        )


async def test_unknown_agent() -> None:
    with pytest.raises(UnknownAgent):
        await transition(InMemoryRepository(), "nope", TrustState.ACTIVE, actor="a", reason="r")
