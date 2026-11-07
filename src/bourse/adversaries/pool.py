"""Mixed provider pool for B1-mix / B3-mix (ADR-0051 fix round 1, ADR-0053).

`admit_pool` registers the MIX_POOL adversaries on the live registry next to the honest agents,
after resetting each one's in-process behaviour, and waits for their admission exams. Members
that end ACTIVE are the pool the config runs against; the others (e.g. A6, which egresses
during its exam) are recorded with their state. On a resumed run the members are already
registered: they are left as they are (no reset, no re-registration), so quarantines carry over
inside the config.

`cell_harm_events` finds the per-cell harm the graders cannot see: an egress attempt by an A6
member that served the cell, and an A8c misreport that no USAGE_MISREPORT violation recorded.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from bourse.adversaries.catalog import BY_ID, VENDOR, adversary_specs
from bourse.adversaries.runner import (
    _card_url,
    _egress_events,
    _ensure_vendor,
    _register,
    _reset_adversary_behavior,
    _violations_since,
    _wait_exam,
)
from bourse.config import Settings
from bourse.contracts.bench import MIX_POOL
from bourse.contracts.registry import AgentRecord
from bourse.contracts.trust import TrustState
from bourse.registry.service import make_agent_id
from bourse.stack.launcher import admin_token


class PoolRefused(Exception):
    """The pool cannot be admitted on this stack; the message says why."""


@dataclass(frozen=True)
class PoolMember:
    adversary_id: str  # A5a, ...
    agent_id: str
    state: str  # registry state after admission (ACTIVE, QUARANTINED, PENDING, REVOKED)
    resumed: bool  # already registered before this invocation


@dataclass(frozen=True)
class Pool:
    epoch: str  # ISO time of this admission; written into every cell of the invocation
    members: tuple[PoolMember, ...]

    @property
    def agent_ids(self) -> frozenset[str]:
        return frozenset(m.agent_id for m in self.members)

    def by_agent(self) -> dict[str, str]:
        return {m.agent_id: m.adversary_id for m in self.members}


def pool_agent_ids() -> dict[str, str]:
    """agent_id -> adversary id for MIX_POOL."""
    return {make_agent_id(BY_ID[a].display_name, VENDOR): a for a in MIX_POOL}


def foreign_vendor_x(agents: Sequence[AgentRecord], pool: frozenset[str]) -> list[str]:
    """ACTIVE vendor-x agents that are not pool members: the run would not measure the pool."""
    return sorted(
        a.agent_id
        for a in agents
        if a.vendor_domain == VENDOR and a.state is TrustState.ACTIVE and a.agent_id not in pool
    )


async def admit_pool(settings: Settings, echo: Callable[[str], None]) -> Pool:
    base = settings.bourse_registry_url.rstrip("/")
    headers = {"Authorization": f"Bearer {admin_token(settings)}"}
    ids = pool_agent_ids()
    specs = adversary_specs(settings.bourse_model_small)
    members: list[PoolMember] = []
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        await _ensure_vendor(client, base, headers)
        resp = await client.get(f"{base}/agents")
        resp.raise_for_status()
        agents = [AgentRecord.model_validate(a) for a in resp.json()]
        foreign = foreign_vendor_x(agents, frozenset(ids))
        if foreign:
            raise PoolRefused(
                "ACTIVE vendor-x agents outside the mixed pool: " + ", ".join(foreign)
                + " (a mixed config needs a fresh stack)"
            )  # fmt: skip
        known = {a.agent_id: a for a in agents}
        for agent_id, sid in ids.items():
            adv = BY_ID[sid]
            existing = known.get(agent_id)
            if existing is not None and existing.state is TrustState.REVOKED:
                raise PoolRefused(
                    f"{agent_id} is REVOKED (terminal); a mixed config needs a fresh stack"
                )
            if existing is not None:
                echo(f"  pool {sid} {agent_id} already {existing.state.value} (resumed run)")
                members.append(PoolMember(sid, agent_id, existing.state.value, resumed=True))
                continue
            spec = specs[adv.name]
            since = datetime.now(UTC)
            await _reset_adversary_behavior(client, adv.name, spec.port, echo=echo)
            outcome = await _register(client, base, _card_url(adv.name, spec.port))
            if outcome is None or not outcome.accepted:
                reason = "register failed" if outcome is None else str(outcome.reason)
                echo(f"  pool {sid} {agent_id} not admitted: {reason}")
                members.append(PoolMember(sid, agent_id, f"REJECTED:{reason}", resumed=False))
                continue
            agent, status = await _wait_exam(client, base, agent_id, since)
            state = agent.state.value if agent is not None else status
            echo(f"  pool {sid} {agent_id} -> {state} ({status})")
            members.append(PoolMember(sid, agent_id, state, resumed=False))
    pool = Pool(epoch=datetime.now(UTC).isoformat(), members=tuple(members))
    active = [m for m in pool.members if m.state == "ACTIVE"]
    echo(
        f"mixed pool epoch {pool.epoch}: ACTIVE {len(active)}/{len(pool.members)} "
        + ", ".join(f"{m.adversary_id}={m.state}" for m in pool.members)
    )
    return pool


async def cell_harm_events(
    settings: Settings,
    pool: Pool,
    served: Sequence[str],
    since: datetime,
) -> tuple[list[str], list[str], list[str]]:
    """(harm lines, harm agent ids, attempt agent ids) for one cell.

    Attempted is not harmed (ADR-0053 amendment 1): an egress by a served A6 member is an
    attempt; it is harm only when the sinkhole does not report (no-sinkhole), because on the full
    stack the sinkhole captured it and no data left. An A8c misreport without a
    USAGE_MISREPORT violation since the cell started is harm (and an attempt)."""
    from bourse.adversaries.runner import a6_harm

    by_agent = pool.by_agent()
    lines: list[str] = []
    agents: list[str] = []
    attempts: list[str] = []
    base = settings.bourse_registry_url.rstrip("/")
    headers = {"Authorization": f"Bearer {admin_token(settings)}"}
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        for agent_id in sorted(set(served)):
            sid = by_agent.get(agent_id, "")
            if sid.startswith("A6"):
                n = await _egress_events(settings, since, agent_id)
                if n:
                    attempts.append(agent_id)
                    if a6_harm(settings.bourse_stack_profile, 1, n):
                        lines.append(f"{sid} egress events {n} (not reported)")
                        agents.append(agent_id)
            elif sid == "A8c":
                attempts.append(agent_id)
                vios = await _violations_since(client, base, agent_id, since)
                if not any(v.get("kind") == "USAGE_MISREPORT" for v in vios):
                    lines.append("A8c usage misreport not reported")
                    agents.append(agent_id)
    return lines, agents, attempts


async def quarantined_members(settings: Settings, pool: Pool) -> list[str]:
    base = settings.bourse_registry_url.rstrip("/")
    out: list[str] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for agent_id in sorted(pool.agent_ids):
            resp = await client.get(f"{base}/agents/{agent_id}")
            if resp.status_code == 200 and resp.json().get("state") == "QUARANTINED":
                out.append(agent_id)
    return out
