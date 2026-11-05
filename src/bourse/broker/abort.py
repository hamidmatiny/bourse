"""Normalize broker abort reasons to a closed prefix set for runs.abort_reason."""

from __future__ import annotations

RESIDENT_LIMIT = "RESIDENT_LIMIT"
BREAKER = "BREAKER"
WATCHDOG = "WATCHDOG"
WALL_BUDGET = "WALL_BUDGET"
PREFIXES = (RESIDENT_LIMIT, BREAKER, WATCHDOG, WALL_BUDGET)


def normalize_abort_reason(raw: str | None) -> str | None:
    """Map free-text kill/breaker reasons to PREFIX: detail.

    Resident trips go through the global breaker with a 'resident memory …' message;
    those classify as RESIDENT_LIMIT, not BREAKER.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    upper = text.upper()
    for prefix in PREFIXES:
        if upper.startswith(prefix):
            # Re-canon to PREFIX: detail (drop a duplicated lowercase prefix if present).
            rest = text.split(":", 1)[1].strip() if ":" in text else text
            return f"{prefix}: {rest}" if rest else prefix
    lower = text.lower()
    if "resident" in lower:
        return f"{RESIDENT_LIMIT}: {text}"
    if "wall budget" in lower:
        return f"{WALL_BUDGET}: {text}"
    if "heartbeat" in lower or lower.startswith("watchdog"):
        detail = text.split(":", 1)[-1].strip() if lower.startswith("watchdog") else text
        return f"{WATCHDOG}: {detail}"
    return f"{BREAKER}: {text}"


def resident_limit_reason(*, peak_bytes: int, over_s: float, limit_bytes: int) -> str:
    from bourse.runtime.resident import format_bytes_units

    return (
        f"{RESIDENT_LIMIT}: peak={peak_bytes} bytes ({format_bytes_units(peak_bytes)}) "
        f"over_s={over_s:.0f} limit={limit_bytes} ({format_bytes_units(limit_bytes)})"
    )
