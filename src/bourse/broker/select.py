"""Agent selection (ADR-0025, ADR-0031). Deterministic: same inputs -> same decision.

utility = reliability_lb - lambda * cost - mu * latency; ranked by utility descending, ties
broken by agent_id ascending. Phase 3 PriorPolicy uses priors only; Phase 4 swaps in measured
reliability, cost and latency behind the same SelectionPolicy protocol.
"""

import hashlib
import random
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from bourse.broker.reliability import AgentStats, stats_by_agent
from bourse.contracts.broker import CandidateScore, SelectionDecision, UtilityTerms
from bourse.contracts.registry import AgentRecord
from bourse.contracts.skills import SkillId
from bourse.contracts.tasks import Subtask
from bourse.ledger.repo import LedgerRepository
from bourse.trust.reliability_epoch import effective_agent_major


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
    """Unknown agents or models get the highest configured price and latency (conservative).

    Ties on utility go to agent_id order, or, with `tie_seed` (bench baseline B2, ADR-0051),
    to sha256(tie_seed:subtask_id:agent_id) order: a seeded draw per task that does not
    depend on how agents are named.
    """

    def __init__(self, config: PriorConfig, tie_seed: str | None = None) -> None:
        self.config = config
        self.tie_seed = tie_seed

    def _tie(self, subtask_id: str, agent_id: str) -> str:
        if self.tie_seed is None:
            return agent_id
        return tie_key(self.tie_seed, subtask_id, agent_id)

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
        scored.sort(key=lambda c: (-c.terms.utility, self._tie(subtask.id, c.agent_id)))
        ranked = [c.agent_id for c in scored]
        tie = "agent_id" if self.tie_seed is None else "seeded hash per task"
        return SelectionDecision(
            subtask_id=subtask.id,
            skill=subtask.skill,
            candidates=tuple(scored),
            chosen=ranked[0] if ranked else None,
            fallbacks=tuple(ranked[1:]),
            reason=(
                f"max utility (priors); ties by {tie}"
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
MEASURED = "max utility (measured; ties by seeded hash per decision)"


def tie_key(seed: str, *parts: object) -> str:
    """Tie-break key that does not depend on how an agent is named (ADR-0051 fix round 3)."""
    return hashlib.sha256(":".join([seed, *map(str, parts)]).encode()).hexdigest()


class MeasuredPolicy:
    """ADR-0031. utility = wilson_lb - lambda * cost_norm - mu * latency_norm, where cost and
    latency are EWMAs of the agent's ledger rows (cold start: Phase 3 priors converted to USD per
    delegation and ms) normalized by the maximum among the candidates.

    Exploration quota: when any candidate has fewer than min_trials attempts on this skill, every
    explore_every-th selection decision for the skill (counted from persisted
    selection_decisions) goes to the least-tried candidate.

    Exact utility ties and least-tried ties are broken by tie_key(tie_seed, decision index,
    subtask id, agent_id), never by agent_id order: a name that sorts first would otherwise win
    every tie (attacker-steerable). The default seed is a per-process secret, so the order
    cannot be precomputed by choosing a name; the bench passes a per-case seed so runs are
    reproducible (ADR-0051).
    """

    def __init__(
        self,
        config: MeasuredConfig,
        priors: PriorConfig,
        ledger: LedgerRepository,
        tie_seed: str | None = None,
    ):
        self.config = config
        self.priors = priors
        self.ledger = ledger
        self.tie_seed = tie_seed if tie_seed is not None else secrets.token_hex(16)
        self._stats: dict[SkillId, dict[str, AgentStats]] = {}
        self._decisions: dict[SkillId, int] = {}

    async def refresh(self, skill: SkillId, candidates: Sequence[AgentRecord]) -> None:
        majors = {a.agent_id: effective_agent_major(a.current_version, a.tags) for a in candidates}
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
        index = self._decisions.get(subtask.skill, 0) + 1

        def tie(agent_id: str) -> str:
            return tie_key(self.tie_seed, index, subtask.id, agent_id)

        scored.sort(key=lambda c: (-c.terms.utility, tie(c.agent_id)))
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
        trials = {c.agent_id: c.terms.s + c.terms.f for c in scored}
        under = any(t < cfg.min_trials for t in trials.values())
        if len(ranked) > 1 and cfg.explore_every > 0 and index % cfg.explore_every == 0 and under:
            chosen = min(ranked, key=lambda a: (trials[a], tie(a)))
            reason = EXPLORATION
        return SelectionDecision(
            subtask_id=subtask.id,
            skill=subtask.skill,
            candidates=tuple(scored),
            chosen=chosen,
            fallbacks=tuple(a for a in ranked if a != chosen),
            reason=reason,
        )


RANDOM = "uniform random among ACTIVE (seeded)"


class RandomPolicy:
    """ADR-0051 baseline B1: uniform random choice among ACTIVE candidates for the skill.

    Seeded: the same seed and the same sequence of decisions give the same choices. The
    remaining candidates follow in a seeded random order as fallbacks.
    """

    def __init__(self, seed: str) -> None:
        self.seed = seed
        self._rng = random.Random(seed)

    async def refresh(self, skill: SkillId, candidates: Sequence[AgentRecord]) -> None:
        return None

    def choose(self, subtask: Subtask, candidates: Sequence[AgentRecord]) -> SelectionDecision:
        ids = sorted(a.agent_id for a in candidates if subtask.skill in a.skills)
        self._rng.shuffle(ids)
        neutral = UtilityTerms(reliability_lb=0.0, cost=0.0, latency=0.0, utility=0.0)
        return SelectionDecision(
            subtask_id=subtask.id,
            skill=subtask.skill,
            candidates=tuple(CandidateScore(agent_id=a, terms=neutral) for a in ids),
            chosen=ids[0] if ids else None,
            fallbacks=tuple(ids[1:]),
            reason=RANDOM if ids else "no ACTIVE agent offers this skill",
        )
