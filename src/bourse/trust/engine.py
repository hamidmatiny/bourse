"""Trust engine (ADR-0039). Deterministic; no LLM.

HARD attributed violations move an ACTIVE agent to QUARANTINED immediately. SOFT attributed
violations are strikes; k (default 3) within window W (default 24h) also quarantines.
Strikes are counted per (agent, rel_epoch): reinstatement bumps rel_epoch, so violations from
before reinstatement no longer count, the same way reliability restarts (ADR-0048).
QUARANTINED -> ACTIVE only via an approved ApprovalDecision (DenyApprover keeps it quarantined).
HIGH_REP_VIOLATION notify (ADR-0048) when HARD + wilson_lb >= 0.8.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from bourse.approval.base import Approver
from bourse.approval.triggers import is_high_rep_hard, notify_high_rep_violation
from bourse.contracts.trust import Severity, TrustState, Violation
from bourse.registry.repo import Repository
from bourse.registry.state import IllegalTransition, transition
from bourse.trust.reliability_epoch import reliability_epoch

OnQuarantine = Callable[[str], Awaitable[None] | None]
WilsonLookup = Callable[[str], Awaitable[float] | float]


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
        approver: Approver | None = None,
        wilson_lb_for: WilsonLookup | None = None,
    ) -> None:
        if k < 1:
            raise ValueError("k must be >= 1")
        self.repo = repo
        self.k = k
        self.window_s = window_s
        self.clock = clock
        self.on_quarantine = on_quarantine
        self.actor = actor
        self.approver = approver
        self.wilson_lb_for = wilson_lb_for

    async def ingest(self, violation: Violation, *, attributed: bool) -> TrustState | None:
        """Store the row, then maybe quarantine. Returns the agent's state after ingest."""
        await self.repo.add_violation(violation)
        agent = await self.repo.get_agent(violation.agent_id)
        if agent is None:
            return None
        if not attributed or agent.state is not TrustState.ACTIVE:
            return agent.state
        hard = violation.severity is Severity.HARD
        if hard and self.approver is not None:
            await self._maybe_high_rep(agent.agent_id, agent.vendor_domain, violation)
        epoch = reliability_epoch(agent.tags)
        soft_strike = await self._strikes(violation.agent_id, epoch) >= self.k
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

    async def _maybe_high_rep(self, agent_id: str, vendor: str, violation: Violation) -> None:
        assert self.approver is not None
        wilson = 0.0
        if self.wilson_lb_for is not None:
            raw = self.wilson_lb_for(agent_id)
            wilson = float(await raw) if isinstance(raw, Awaitable) else float(raw)
        if not is_high_rep_hard(violation, wilson):
            return
        live = await self.repo.get_agent(agent_id)
        strikes = await self._strikes(agent_id, reliability_epoch(live.tags) if live else 0)
        await notify_high_rep_violation(
            self.approver,
            agent_id=agent_id,
            vendor=vendor,
            violation=violation,
            wilson_lb=wilson,
            strikes=strikes,
        )

    async def _strikes(self, agent_id: str, epoch: int) -> int:
        """SOFT violations in window W stored under the agent's current rel_epoch."""
        start = self.clock() - timedelta(seconds=self.window_s)
        return sum(
            1
            for v in await self.repo.list_violations(agent_id)
            if v.severity is Severity.SOFT and v.at >= start and (v.rel_epoch or 0) == epoch
        )
