"""Bench statistics (ADR-0051): Wilson 95% CI for n/N, order-statistic CIs for quantiles.

Wilson applies to proportions only. For p50/p95 of cost and latency the CI is the
distribution-free binomial order-statistic interval: the smallest symmetric-in-probability
pair of order statistics (l, u) with P(B(N, q) in [l, u-1]) >= 0.95. Deterministic, no RNG.
"""

import math
from collections.abc import Sequence

from bourse.contracts.bench import Proportion, Quantile

Z95 = 1.959963984540054


def wilson(n: int, total: int, z: float = Z95) -> Proportion:
    if total < 0 or n < 0 or n > total:
        raise ValueError(f"invalid n/N {n}/{total}")
    if total == 0:
        return Proportion(n=0, N=0, ci_low=0.0, ci_high=1.0)
    p = n / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return Proportion(n=n, N=total, ci_low=max(0.0, centre - half), ci_high=min(1.0, centre + half))


def wilson_half_width(p: float, total: int, z: float = Z95) -> float:
    """Half-width of the Wilson interval at proportion p and size N (for sizing tables)."""
    if total <= 0:
        return 0.5
    denom = 1 + z * z / total
    return z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom


def _binom_cdf(k: int, n: int, q: float) -> float:
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(math.comb(n, i) * q**i * (1 - q) ** (n - i) for i in range(k + 1))


def quantile_value(sorted_values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile (ceil(q*N)-th smallest); deterministic and an observed value."""
    idx = max(0, math.ceil(q * len(sorted_values)) - 1)
    return sorted_values[idx]


def quantile(values: Sequence[float], q: float, level: float = 0.95) -> Quantile:
    data = sorted(values)
    total = len(data)
    if total == 0:
        return Quantile(q=q, value=None, ci_low=None, ci_high=None, N=0)
    value = quantile_value(data, q)
    alpha = (1 - level) / 2
    # [X(l), X(u)] (1-based order statistics) covers the q-quantile with probability
    # P(l <= B <= u-1), B ~ Binomial(N, q). l = largest with P(B < l) <= alpha;
    # u = smallest with P(B >= u) <= alpha. Missing bound (sample too small) -> None.
    ls = [r for r in range(1, total + 1) if _binom_cdf(r - 1, total, q) <= alpha]
    us = [r for r in range(1, total + 1) if 1 - _binom_cdf(r - 1, total, q) <= alpha]
    low = data[max(ls) - 1] if ls else None
    high = data[min(us) - 1] if us else None
    return Quantile(q=q, value=value, ci_low=low, ci_high=high, N=total)
