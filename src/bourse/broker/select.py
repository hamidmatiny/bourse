"""Agent selection (ADR-0025). Deterministic: same candidates + config -> same decision.

utility = reliability_lb - lambda * cost - mu * latency; ranked by utility descending, ties
broken by agent_id ascending. Phase 3 PriorPolicy uses priors only; Phase 4 swaps in measured
reliability, cost and latency behind the same SelectionPolicy protocol.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from bourse.contracts.broker import CandidateScore, SelectionDecision, UtilityTerms
from bourse.contracts.registry import AgentRecord
from bourse.contracts.tasks import Subtask


class SelectionPolicy(Protocol):
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
