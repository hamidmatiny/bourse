"""Agent selection (ADR-0025, ADR-0031). Deterministic: same inputs -> same decision.

utility = reliability_lb - lambda * cost - mu * latency; ranked by utility descending, ties
broken by agent_id ascending. Phase 3 PriorPolicy uses priors only; Phase 4 swaps in measured
reliability, cost and latency behind the same SelectionPolicy protocol.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from bourse.broker.reliability import AgentStats, stats_by_agent
from bourse.contracts.broker import CandidateScore, SelectionDecision, UtilityTerms
from bourse.contracts.registry import AgentRecord
from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import Subtask
from bourse.ledger.recorder import agent_major
from bourse.ledger.repo import LedgerRepository


class SelectionPolicy(Protocol):
    async def refresh(self, skill: SkillId, candidates: Sequence[AgentRecord]) -> None:
        """Load whatever the policy needs for `skill` before choose() (no-op for priors)."""
        ...

    def choose(self, subtask: Subtask, candidates: Sequence[AgentRecord]) -> SelectionDecision: ...


@dataclass(frozen=True)
class PriorConfig:
    reliability_prior: float = 0.5
    lambda_cost: float = 1.0
    mu_latency: float = 0.005
    # Shadow prices (USD per 1M tokens; not billed) and latency priors (s) per model.
    shadow_prices: Mapping[str, float] = field(default_factory=dict)
    latency_priors: Mapping[str, float] = field(default_factory=dict)
    # The Agent Card does not name a model; the operator pins agent_id -> model here.
    agent_models: Mapping[str, str] = field(default_factory=dict)


class PriorPolicy:
    """Unknown agents or models get the highest configured price and latency (conservative)."""

    def __init__(self, config: PriorConfig) -> None:
        self.config = config

    async def refresh(self, skill: SkillId, candidates: Sequence[AgentRecord]) -> None:
        return None

    def terms(self, agent_id: str) -> UtilityTerms:
        cfg = self.config
        model = cfg.agent_models.get(agent_id)
        worst_cost = max(cfg.shadow_prices.values(), default=0.0)
        worst_latency = max(cfg.latency_priors.values(), default=0.0)
        cost = cfg.shadow_prices.get(model, worst_cost) if model else worst_cost
        latency = cfg.latency_priors.get(model, worst_latency) if model else worst_latency
        rel = cfg.reliability_prior
        utility = rel - cfg.lambda_cost * cost - cfg.mu_latency * latency
        return UtilityTerms(reliability_lb=rel, cost=cost, latency=latency, utility=utility)

    def choose(self, subtask: Subtask, candidates: Sequence[AgentRecord]) -> SelectionDecision:
        scored = [
            CandidateScore(agent_id=a.agent_id, terms=self.terms(a.agent_id))
            for a in candidates
            if subtask.skill in a.skills
        ]
        scored.sort(key=lambda c: (-c.terms.utility, c.agent_id))
        ranked = [c.agent_id for c in scored]
        return SelectionDecision(
            subtask_id=subtask.id,
            skill=subtask.skill,
            candidates=tuple(scored),
            chosen=ranked[0] if ranked else None,
            fallbacks=tuple(ranked[1:]),
            reason=(
                "max utility (priors); ties by agent_id"
                if ranked
                else "no ACTIVE agent offers this skill"
            ),
        )


@dataclass(frozen=True)
class MeasuredConfig:
    lambda_cost: float = 0.2
    mu_latency: float = 0.2
    ewma_alpha: float = 0.3
    min_trials: int = 5
    explore_every: int = 4
    cold_tokens: int = 2000  # tokens assumed per delegation for cold-start cost


EXPLORATION = "exploration"
MEASURED = "max utility (measured; ties by agent_id)"


class MeasuredPolicy:
    """ADR-0031. utility = wilson_lb - lambda * cost_norm - mu * latency_norm, where cost and
    latency are EWMAs of the agent's ledger rows (cold start: Phase 3 priors converted to USD per
    delegation and ms) normalized by the maximum among the candidates.

    Exploration quota: when any candidate has fewer than min_trials attempts on this skill, every
    explore_every-th selection decision for the skill (counted from persisted
    selection_decisions) goes to the least-tried candidate (ties by agent_id).
    """

    def __init__(self, config: MeasuredConfig, priors: PriorConfig, ledger: LedgerRepository):
        self.config = config
        self.priors = priors
        self.ledger = ledger
        self._stats: dict[SkillId, dict[str, AgentStats]] = {}
        self._decisions: dict[SkillId, int] = {}

    async def refresh(self, skill: SkillId, candidates: Sequence[AgentRecord]) -> None:
        majors = {a.agent_id: agent_major(a.current_version) for a in candidates}
        rows = await self.ledger.list_rows(skill=skill)
        self._stats[skill] = stats_by_agent(rows, skill, majors, self.config.ewma_alpha)
        self._decisions[skill] = len(await self.ledger.list_selections(skill=skill))

    def _cold(self, agent_id: str) -> tuple[float, float]:
        prior = PriorPolicy(self.priors).terms(agent_id)
        return prior.cost * self.config.cold_tokens / 1e6, prior.latency * 1000

    def choose(self, subtask: Subtask, candidates: Sequence[AgentRecord]) -> SelectionDecision:
        cfg = self.config
        stats = self._stats.get(subtask.skill, {})
        cands = sorted(
            (a for a in candidates if subtask.skill in a.skills), key=lambda a: a.agent_id
        )
        raw: list[tuple[str, AgentStats, float, float, bool]] = []
        for a in cands:
            st = stats.get(a.agent_id) or AgentStats(0, 0, 0.0, None, None)
            cold_cost, cold_latency = self._cold(a.agent_id)
            measured = st.trials > 0 and st.ewma_usd is not None and st.ewma_wall_ms is not None
            cost = st.ewma_usd if measured and st.ewma_usd is not None else cold_cost
            latency = st.ewma_wall_ms if measured and st.ewma_wall_ms is not None else cold_latency
            raw.append((a.agent_id, st, cost, latency, measured))
        max_cost = max((r[2] for r in raw), default=0.0)
        max_latency = max((r[3] for r in raw), default=0.0)
        scored: list[CandidateScore] = []
        for agent_id, st, cost, latency, measured in raw:
            cost_norm = cost / max_cost if max_cost > 0 else 0.0
            latency_norm = latency / max_latency if max_latency > 0 else 0.0
            utility = st.wilson_lb - cfg.lambda_cost * cost_norm - cfg.mu_latency * latency_norm
            terms = UtilityTerms(
                reliability_lb=st.wilson_lb,
                cost=cost,
                latency=latency,
                utility=utility,
                s=st.s,
                f=st.f,
                cost_norm=cost_norm,
                latency_norm=latency_norm,
                measured=measured,
            )
            scored.append(CandidateScore(agent_id=agent_id, terms=terms))
        scored.sort(key=lambda c: (-c.terms.utility, c.agent_id))
        ranked = [c.agent_id for c in scored]
        if not ranked:
            return SelectionDecision(
                subtask_id=subtask.id,
                skill=subtask.skill,
                candidates=(),
                chosen=None,
                fallbacks=(),
                reason="no ACTIVE agent offers this skill",
            )
        reason = MEASURED
        chosen = ranked[0]
        index = self._decisions.get(subtask.skill, 0) + 1
        trials = {c.agent_id: c.terms.s + c.terms.f for c in scored}
        under = any(t < cfg.min_trials for t in trials.values())
        if len(ranked) > 1 and cfg.explore_every > 0 and index % cfg.explore_every == 0 and under:
            chosen = min(ranked, key=lambda a: (trials[a], a))
            reason = EXPLORATION
        return SelectionDecision(
            subtask_id=subtask.id,
            skill=subtask.skill,
            candidates=tuple(scored),
            chosen=chosen,
            fallbacks=tuple(a for a in ranked if a != chosen),
            reason=reason,
        )
