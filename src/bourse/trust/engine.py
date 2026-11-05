"""Trust engine (ADR-0039). Deterministic; no LLM.

HARD attributed violations move an ACTIVE agent to QUARANTINED immediately. SOFT attributed
violations are strikes; k (default 3) within window W (default 24h) also quarantines.
QUARANTINED -> ACTIVE only via an approved ApprovalDecision (DenyApprover keeps it quarantined).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from bourse.contracts.trust import Severity, TrustState, Violation
from bourse.registry.repo import Repository
from bourse.registry.state import IllegalTransition, transition

OnQuarantine = Callable[[str], Awaitable[None] | None]


class TrustEngine:
    def __init__(
        self,
        repo: Repository,
        *,
        k: int = 3,
        window_s: float = 86400.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        on_quarantine: OnQuarantine | None = None,
        actor: str = "trust",
    ) -> None:
        if k < 1:
            raise ValueError("k must be >= 1")
        self.repo = repo
        self.k = k
        self.window_s = window_s
        self.clock = clock
        self.on_quarantine = on_quarantine
        self.actor = actor

    async def ingest(self, violation: Violation, *, attributed: bool) -> TrustState | None:
        """Store the row, then maybe quarantine. Returns the agent's state after ingest."""
        await self.repo.add_violation(violation)
        agent = await self.repo.get_agent(violation.agent_id)
        if agent is None:
            return None
        if not attributed or agent.state is not TrustState.ACTIVE:
            return agent.state
        hard = violation.severity is Severity.HARD
        soft_strike = await self._strikes(violation.agent_id) >= self.k
        if hard or (violation.severity is Severity.SOFT and soft_strike):
            reason = (
                f"HARD violation {violation.kind}"
                if hard
                else f"{self.k} SOFT strikes within {int(self.window_s)}s ({violation.kind})"
            )
            try:
                await transition(
                    self.repo,
                    violation.agent_id,
                    TrustState.QUARANTINED,
                    actor=self.actor,
                    reason=reason,
                    now=self.clock(),
                )
            except IllegalTransition:
                live = await self.repo.get_agent(violation.agent_id)
                return None if live is None else live.state
            if self.on_quarantine is not None:
                result = self.on_quarantine(violation.agent_id)
                if isinstance(result, Awaitable):
                    await result
            live = await self.repo.get_agent(violation.agent_id)
            return None if live is None else live.state
        return agent.state

    async def _strikes(self, agent_id: str) -> int:
        start = self.clock() - timedelta(seconds=self.window_s)
        return sum(
            1
            for v in await self.repo.list_violations(agent_id)
            if v.severity is Severity.SOFT and v.at >= start
        )
