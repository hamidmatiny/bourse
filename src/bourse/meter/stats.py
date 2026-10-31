"""Per-call token statistics from llm_usage (ADR-0033 num_ctx decision)."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from bourse.contracts.metering import LlmUsageRecord

NUM_CTX_CHOICES = (4096, 6144, 8192)


@dataclass(frozen=True)
class MaxCallRow:
    model: str
    calls: int
    max: int
    p50: int
    p95: int


def nearest_rank(sorted_values: Sequence[int], q: float) -> int:
    """Nearest-rank percentile of an ascending list (q in (0, 1])."""
    rank = max(1, math.ceil(q * len(sorted_values)))
    return sorted_values[rank - 1]


def maxcall_rows(usage: Sequence[LlmUsageRecord]) -> list[MaxCallRow]:
    by_model: dict[str, list[int]] = {}
    for u in usage:
        if u.status == 200:
            by_model.setdefault(u.model, []).append(u.tokens_in + u.tokens_out)
    rows = []
    for model, values in sorted(by_model.items()):
        values.sort()
        rows.append(
            MaxCallRow(
                model=model,
                calls=len(values),
                max=values[-1],
                p50=nearest_rank(values, 0.5),
                p95=nearest_rank(values, 0.95),
            )
        )
    return rows


def recommend_num_ctx(max_tokens: int) -> int:
    """Smallest choice >= 2 x the observed maximum; the largest choice if none is."""
    for choice in NUM_CTX_CHOICES:
        if choice >= 2 * max_tokens:
            return choice
    return NUM_CTX_CHOICES[-1]
