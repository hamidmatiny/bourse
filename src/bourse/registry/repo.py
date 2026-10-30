"""Repository protocol (ADR-0014). Writes that must be atomic are single methods."""

from typing import Protocol

from bourse.contracts.registry import AgentRecord, CardRevision, StateTransition, Vendor
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import TrustState, Violation


class VendorExists(Exception):
    pass


class RevisionExists(Exception):
    """(agent_id, version) already stored; revisions are immutable."""


class Repository(Protocol):
    async def add_vendor(self, vendor: Vendor) -> None: ...

    async def get_vendor(self, domain: str) -> Vendor | None: ...

    async def get_agent(self, agent_id: str) -> AgentRecord | None: ...

    async def save_registration(self, agent: AgentRecord, revision: CardRevision) -> None:
        """Insert-or-update the agent and insert the revision in one transaction."""
        ...

    async def save_transition(self, agent: AgentRecord, transition: StateTransition) -> None:
        """Update the agent's state and write the audit row in one transaction."""
        ...

    async def add_violation(self, violation: Violation) -> None: ...

    async def list_violations(self, agent_id: str) -> list[Violation]: ...

    async def list_revisions(self, agent_id: str) -> list[CardRevision]: ...

    async def list_transitions(self, agent_id: str) -> list[StateTransition]: ...

    async def search(
        self,
        skill: SkillId | None = None,
        state: TrustState | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> list[AgentRecord]:
        """Filter by skill and state; `q` matches agent name and skill tags only."""
        ...
