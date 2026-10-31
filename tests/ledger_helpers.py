"""LedgerRow builders shared by ledger, reliability and policy tests."""

from datetime import UTC, datetime, timedelta

from bourse.contracts.broker import DelegationState, LedgerRow
from bourse.contracts.skills import SkillId

T0 = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def row(
    agent_id: str,
    *,
    ok: bool = True,
    skill: SkillId = SkillId.PII_DETECT,
    major: int = 1,
    usd: float = 0.001,
    wall_ms: float = 1000.0,
    state: DelegationState | None = None,
    i: int = 0,
    run_id: str = "run-h",
) -> LedgerRow:
    final = state or (DelegationState.COMPLETED if ok else DelegationState.FAILED)
    return LedgerRow(
        run_id=run_id,
        subtask_id="pii",
        attempt=1,
        agent_id=agent_id,
        skill=skill,
        revision_digest="0" * 64,
        agent_major=major,
        state=final,
        error_kind=None if final is DelegationState.COMPLETED else "internal",
        tokens_in=100,
        tokens_out=10,
        tool_calls=3,
        shadow_usd=usd,
        price_table_version="test",
        wall_ms=wall_ms,
        spot_check_passed=True if final is DelegationState.COMPLETED else None,
        at=T0 + timedelta(seconds=i),
    )


def history(agent_id: str, s: int, f: int, **kw: object) -> list[LedgerRow]:
    out = [row(agent_id, ok=True, i=i, **kw) for i in range(s)]  # type: ignore[arg-type]
    out += [row(agent_id, ok=False, i=s + i, **kw) for i in range(f)]  # type: ignore[arg-type]
    return out
