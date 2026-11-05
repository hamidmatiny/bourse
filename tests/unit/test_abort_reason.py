"""abort_reason normalization + sticky GLOBAL_BREAKER session reset."""

from bourse.broker.abort import (
    BREAKER,
    RESIDENT_LIMIT,
    WALL_BUDGET,
    WATCHDOG,
    normalize_abort_reason,
    resident_limit_reason,
)
from bourse.runtime.breaker import GLOBAL_BREAKER


def test_normalize_resident_limit_prefix() -> None:
    raw = resident_limit_reason(peak_bytes=29_000_000_000, over_s=31.0, limit_bytes=26_000_000_000)
    assert raw.startswith(f"{RESIDENT_LIMIT}:")
    assert "peak=29000000000" in raw
    assert normalize_abort_reason(raw) == raw


def test_normalize_maps_resident_free_text() -> None:
    out = normalize_abort_reason("resident memory over limit for 30s")
    assert out is not None
    assert out.startswith(f"{RESIDENT_LIMIT}:")


def test_normalize_breaker_and_watchdog_and_wall() -> None:
    assert normalize_abort_reason("HTTP 402: credit balance") == f"{BREAKER}: HTTP 402: credit balance"
    assert normalize_abort_reason("watchdog: heartbeat stale") == f"{WATCHDOG}: heartbeat stale"
    assert normalize_abort_reason("wall budget exceeded") == f"{WALL_BUDGET}: wall budget exceeded"


def test_normalize_none_and_blank() -> None:
    assert normalize_abort_reason(None) is None
    assert normalize_abort_reason("") is None
    assert normalize_abort_reason("   ") is None


def test_global_breaker_reset_clears_sticky_trip() -> None:
    GLOBAL_BREAKER.reset()
    try:
        GLOBAL_BREAKER.trip("resident memory over for test")
        assert GLOBAL_BREAKER.tripped
        was = normalize_abort_reason(GLOBAL_BREAKER.reason)
        assert was is not None and was.startswith(RESIDENT_LIMIT)
        GLOBAL_BREAKER.reset()
        assert not GLOBAL_BREAKER.tripped
        assert GLOBAL_BREAKER.reason is None
    finally:
        GLOBAL_BREAKER.reset()
