"""In-memory Repository for unit tests and `bourse attacks`."""

import re
from datetime import datetime

from bourse.contracts.registry import AgentRecord, CardRevision, StateTransition, Vendor
from bourse.contracts.skills import SkillId
from bourse.contracts.trust import TrustState, Violation
from bourse.registry.repo import RevisionExists, VendorExists
from bourse.trust.reliability_epoch import reliability_epoch

_TOKEN = re.compile(r"[a-z0-9]+")


def search_terms(agent: AgentRecord) -> set[str]:
    return set(_TOKEN.findall(" ".join((agent.name, *agent.tags)).lower()))


class InMemoryRepository:
    def __init__(self) -> None:
        self._vendors: dict[str, Vendor] = {}
        self._agents: dict[str, AgentRecord] = {}
        self._revisions: dict[tuple[str, str], CardRevision] = {}
        self._violations: list[Violation] = []
        self._transitions: list[StateTransition] = []
        self._aggregates: dict[tuple[str, datetime, str], int] = {}

    async def add_vendor(self, vendor: Vendor) -> None:
        if vendor.domain in self._vendors:
            raise VendorExists(vendor.domain)
        self._vendors[vendor.domain] = vendor

    async def get_vendor(self, domain: str) -> Vendor | None:
        return self._vendors.get(domain)

    async def get_agent(self, agent_id: str) -> AgentRecord | None:
        return self._agents.get(agent_id)

    async def save_registration(self, agent: AgentRecord, revision: CardRevision) -> None:
        key = (revision.agent_id, revision.version)
        if key in self._revisions:
            raise RevisionExists(f"{key[0]} {key[1]}")
        self._revisions[key] = revision
        self._agents[agent.agent_id] = agent

    async def save_transition(self, agent: AgentRecord, transition: StateTransition) -> None:
        self._agents[agent.agent_id] = agent
        self._transitions.append(transition)

    async def add_violation(self, violation: Violation) -> None:
        if violation.rel_epoch is None:
            agent = self._agents.get(violation.agent_id)
            epoch = reliability_epoch(agent.tags) if agent is not None else 0
            violation = violation.model_copy(update={"rel_epoch": epoch})
        self._violations.append(violation)

    async def bump_aggregate(self, vendor_domain: str, hour: datetime, kind: str) -> int:
        key = (vendor_domain, hour, kind)
        self._aggregates[key] = self._aggregates.get(key, 0) + 1
        return self._aggregates[key]

    async def get_aggregate(self, vendor_domain: str, hour: datetime) -> int:
        return sum(
            n for (d, h, _k), n in self._aggregates.items() if d == vendor_domain and h == hour
        )

    async def list_violations(self, agent_id: str) -> list[Violation]:
        return [v for v in self._violations if v.agent_id == agent_id]

    async def list_revisions(self, agent_id: str) -> list[CardRevision]:
        revs = [r for (a, _), r in self._revisions.items() if a == agent_id]
        return sorted(revs, key=lambda r: r.received_at)

    async def list_transitions(self, agent_id: str) -> list[StateTransition]:
        return [t for t in self._transitions if t.agent_id == agent_id]

    async def search(
        self,
        skill: SkillId | None = None,
        state: TrustState | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> list[AgentRecord]:
        wanted = set(_TOKEN.findall(q.lower())) if q else set()
        out = [
            a
            for a in sorted(self._agents.values(), key=lambda a: a.agent_id)
            if (skill is None or skill in a.skills)
            and (state is None or a.state == state)
            and wanted <= search_terms(a)
        ]
        return out[:limit]
