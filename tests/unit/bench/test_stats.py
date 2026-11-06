"""Wilson 95% CI and order-statistic quantile CIs (ADR-0051)."""

import pytest

from bourse.bench.stats import quantile, wilson, wilson_half_width


@pytest.mark.parametrize(
    ("n", "total", "low", "high"),
    [
        # Reference values: Wilson score interval, z = 1.959964.
        (0, 10, 0.0, 0.2775),
        (10, 10, 0.7225, 1.0),
        (5, 10, 0.2366, 0.7634),
        (45, 50, 0.7864, 0.9565),
        (1, 1, 0.2065, 1.0),
    ],
)
def test_wilson_reference_values(n: int, total: int, low: float, high: float) -> None:
    p = wilson(n, total)
    assert (p.n, p.N) == (n, total)
    assert p.ci_low == pytest.approx(low, abs=1e-4)
    assert p.ci_high == pytest.approx(high, abs=1e-4)


def test_wilson_empty_and_invalid() -> None:
    p = wilson(0, 0)
    assert (p.n, p.N, p.ci_low, p.ci_high) == (0, 0, 0.0, 1.0)
    with pytest.raises(ValueError):
        wilson(3, 2)


def test_half_width_shrinks_with_n() -> None:
    widths = [wilson_half_width(0.5, n) for n in (25, 50, 100, 200)]
    assert widths == sorted(widths, reverse=True)
    assert wilson_half_width(0.5, 100) == pytest.approx(0.0962, abs=1e-4)  # [0.4038, 0.5962]


def test_quantile_ci_known_values() -> None:
    q = quantile([float(i) for i in range(1, 101)], 0.5)
    assert q.value == 50.0 and q.N == 100
    assert (q.ci_low, q.ci_high) == (40.0, 61.0)  # binomial order statistics
    q95 = quantile([float(i) for i in range(1, 101)], 0.95)
    assert q95.value == 95.0 and q95.ci_low == 90.0 and q95.ci_high == 100.0


def test_quantile_small_n_has_no_bound_and_is_order_free() -> None:
    q = quantile([5.0, 1.0, 3.0, 2.0, 4.0], 0.5)
    assert q.value == 3.0 and q.ci_low is None and q.ci_high is None
    assert quantile([], 0.5).value is None
