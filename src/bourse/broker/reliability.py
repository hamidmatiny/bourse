"""Reliability from ledger rows (ADR-0030).

Per (agent_id, skill, agent_major):
- s = attempts that COMPLETED with a contract-valid output that passed the spot check;
- f = every other recorded attempt (FAILED, REJECTED, FAILED_CONTRACT, FAILED_SPOTCHECK,
  TIMEOUT, INTERRUPTED, CARD_DRIFT, ERROR). Broker-side aborts never write a ledger row, so
  they are excluded by construction.
- score = Wilson 95% lower bound of s / (s + f); a new major version starts a fresh counter.
Cost and latency = EWMA of shadow_usd and wall_ms over the same rows, oldest first.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from bourse.contracts.broker import AgentScore, DelegationState, LedgerRow
from bourse.contracts.skills import SkillId

Z_95 = 1.959963984540054  # two-sided 95% normal quantile


def wilson_lower_bound(s: int, f: int, z: float = Z_95) -> float:
    n = s + f
    if n == 0:
        return 0.0
    p = s / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - margin) / (1 + z2 / n))


def ewma(values: Iterable[float], alpha: float) -> float | None:
    out: float | None = None
    for v in values:
        out = v if out is None else alpha * v + (1 - alpha) * out
    return out


def succeeded(row: LedgerRow) -> bool:
    return row.state is DelegationState.COMPLETED and row.spot_check_passed is True


@dataclass(frozen=True)
class AgentStats:
    s: int
    f: int
    wilson_lb: float
    ewma_usd: float | None
    ewma_wall_ms: float | None

    @property
    def trials(self) -> int:
        return self.s + self.f


def agent_stats(rows: Sequence[LedgerRow], alpha: float) -> AgentStats:
    s = sum(succeeded(r) for r in rows)
    f = len(rows) - s
    return AgentStats(
        s=s,
        f=f,
        wilson_lb=wilson_lower_bound(s, f),
        ewma_usd=ewma((r.shadow_usd for r in rows), alpha),
        ewma_wall_ms=ewma((r.wall_ms for r in rows), alpha),
    )


def stats_by_agent(
    rows: Iterable[LedgerRow], skill: SkillId, majors: dict[str, int], alpha: float
) -> dict[str, AgentStats]:
    """Stats for each agent in `majors` (agent_id -> current major) on `skill`."""
    grouped: dict[str, list[LedgerRow]] = {a: [] for a in majors}
    for r in rows:
        if r.skill is skill and r.agent_id in majors and r.agent_major == majors[r.agent_id]:
            grouped[r.agent_id].append(r)
    return {a: agent_stats(rs, alpha) for a, rs in grouped.items()}


def scores(rows: Iterable[LedgerRow], alpha: float) -> list[AgentScore]:
    """Every (agent, skill, major) seen in the ledger, for `bourse scores`."""
    grouped: dict[tuple[str, SkillId, int], list[LedgerRow]] = {}
    for r in rows:
        grouped.setdefault((r.agent_id, r.skill, r.agent_major), []).append(r)
    out: list[AgentScore] = []
    for (agent, skill, major), rs in sorted(
        grouped.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2])
    ):
        st = agent_stats(rs, alpha)
        out.append(
            AgentScore(
                agent_id=agent,
                skill=skill,
                agent_major=major,
                s=st.s,
                f=st.f,
                wilson_lb=st.wilson_lb,
                ewma_usd=st.ewma_usd,
                ewma_wall_ms=st.ewma_wall_ms,
            )
        )
    return out
