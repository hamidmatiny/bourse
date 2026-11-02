"""Ledger export to parquet, validated by a pandera schema first (ADR-0029)."""

from pathlib import Path

import duckdb
import pandas as pd
import pandera.pandas as pa

from bourse.contracts.broker import DelegationState, LedgerRow
from bourse.contracts.skills import SkillId

LEDGER_SCHEMA = pa.DataFrameSchema(
    {
        "run_id": pa.Column(str),
        "subtask_id": pa.Column(str),
        "attempt": pa.Column(int, pa.Check.ge(1)),
        "agent_id": pa.Column(str),
        "skill": pa.Column(str, pa.Check.isin([s.value for s in SkillId])),
        "revision_digest": pa.Column(str, pa.Check.str_matches(r"^[0-9a-f]{64}$")),
        "agent_major": pa.Column(int, pa.Check.ge(0)),
        "state": pa.Column(str, pa.Check.isin([s.value for s in DelegationState])),
        "error_kind": pa.Column(str, nullable=True),
        "tokens_in": pa.Column(int, pa.Check.ge(0)),
        "tokens_out": pa.Column(int, pa.Check.ge(0)),
        "tool_calls": pa.Column(int, pa.Check.ge(0)),
        "shadow_usd": pa.Column(float, pa.Check.ge(0)),
        "price_table_version": pa.Column(str),
        "wall_ms": pa.Column(float, pa.Check.ge(0)),
        "spot_check_passed": pa.Column(object, nullable=True),
        "at": pa.Column(str),
        "kind": pa.Column(str, pa.Check.isin(["task", "canary"])),
    },
    strict=True,
    ordered=True,
)


def ledger_frame(rows: list[LedgerRow]) -> pd.DataFrame:
    columns = list(LEDGER_SCHEMA.columns)
    records = [r.model_dump(mode="json") for r in rows]
    frame = pd.DataFrame.from_records(records, columns=columns)
    for col in ("attempt", "agent_major", "tokens_in", "tokens_out", "tool_calls"):
        frame[col] = frame[col].astype("int64")
    for col in ("shadow_usd", "wall_ms"):
        frame[col] = frame[col].astype("float64")
    frame["spot_check_passed"] = frame["spot_check_passed"].astype(object)
    frame["error_kind"] = frame["error_kind"].astype(object)
    return frame


def export_parquet(rows: list[LedgerRow], out: Path) -> int:
    """Validate, then write; returns the row count. Raises pandera SchemaError if invalid."""
    frame = LEDGER_SCHEMA.validate(ledger_frame(rows))
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    try:
        con.register("ledger", frame)
        con.execute(f"COPY ledger TO '{out.as_posix()}' (FORMAT parquet)")
    finally:
        con.close()
    return len(frame)
