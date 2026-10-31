from pathlib import Path

import duckdb
import pandera.errors
import pytest

from bourse.contracts.broker import DelegationRecord, DelegationState
from bourse.contracts.metering import LlmUsageRecord, ToolUsageRecord
from bourse.contracts.skills import SkillId
from bourse.ledger.export import LEDGER_SCHEMA, export_parquet, ledger_frame
from bourse.ledger.prices import load_prices
from bourse.ledger.recorder import LedgerRecorder, agent_major
from bourse.ledger.repo import InMemoryLedgerRepository
from bourse.mcp_servers.usage import InMemoryToolUsageRepository
from bourse.meter.repo import InMemoryMeterRepository
from bourse.meter.stats import maxcall_rows, nearest_rank, recommend_num_ctx
from tests.ledger_helpers import T0, row

PRICES = load_prices(Path("config/prices.toml"))


def _usage(task_id: str, model: str, tin: int, tout: int) -> LlmUsageRecord:
    return LlmUsageRecord(
        agent_id="a",
        task_id=task_id,
        provider="ollama",
        model=model,
        tokens_in=tin,
        tokens_out=tout,
        wall_ms=1.0,
        status=200,
        error_kind=None,
        at=T0,
    )


def _tool(task_id: str) -> ToolUsageRecord:
    return ToolUsageRecord(
        agent_id="a", task_id=task_id, tool="pii.luhn_check", wall_ms=1.0, ok=True, at=T0
    )


def test_prices_table_loaded_and_unknown_model_priced_high() -> None:
    assert PRICES.version
    assert set(PRICES.models) == {"gemma4:12b", "qwen3.5:27b-q4_K_M"}
    worst = PRICES.model_price("unknown-model")
    assert worst.in_per_1m == max(p.in_per_1m for p in PRICES.models.values())


async def test_ledger_row_joins_tokens_and_tool_calls_by_task_id() -> None:
    usage, tools, ledger = (
        InMemoryMeterRepository(),
        InMemoryToolUsageRepository(),
        InMemoryLedgerRepository(),
    )
    await usage.add_usage(_usage("run-1:pii:2", "gemma4:12b", 1000, 100))
    await usage.add_usage(_usage("run-1:pii:2", "gemma4:12b", 500, 50))
    await usage.add_usage(_usage("run-1:pii:1", "gemma4:12b", 9999, 9999))  # other attempt
    await usage.add_usage(_usage("run-10:pii:2", "gemma4:12b", 9999, 9999))  # other run
    for _ in range(3):
        await tools.add_tool_usage(_tool("run-1:pii:2"))
    await tools.add_tool_usage(_tool("run-1:pii:1"))
    recorder = LedgerRecorder(ledger, usage, tools, PRICES)
    delegation = DelegationRecord(
        run_id="run-1",
        subtask_id="pii",
        skill=SkillId.PII_DETECT,
        agent_id="a",
        revision_digest="0" * 64,
        attempt=2,
        state=DelegationState.COMPLETED,
        wall_ms=12.0,
        at=T0,
    )
    out = await recorder.record(delegation, major=agent_major("2.3.1"), spot_check_passed=True)
    assert (out.tokens_in, out.tokens_out, out.tool_calls, out.agent_major) == (1500, 150, 3, 2)
    gemma = PRICES.models["gemma4:12b"]
    expected = (
        1500 * gemma.in_per_1m / 1e6 + 150 * gemma.out_per_1m / 1e6 + 3 * PRICES.tool_call_usd
    )
    assert out.shadow_usd == pytest.approx(expected)
    assert out.price_table_version == PRICES.version
    assert ledger.rows == [out]


def test_export_parquet_round_trip(tmp_path: Path) -> None:
    rows = [
        row("a", i=0),
        row("a", ok=False, i=1),
        row("b", state=DelegationState.FAILED_SPOTCHECK, i=2),
    ]
    out = tmp_path / "ledger.parquet"
    assert export_parquet(rows, out) == 3
    con = duckdb.connect(":memory:")
    got = con.execute(
        f"SELECT agent_id, state, spot_check_passed FROM '{out}' ORDER BY \"at\""
    ).fetchall()
    con.close()
    assert got == [("a", "COMPLETED", True), ("a", "FAILED", None), ("b", "FAILED_SPOTCHECK", None)]


def test_pandera_schema_rejects_bad_rows() -> None:
    frame = ledger_frame([row("a")])
    frame.loc[0, "state"] = "NOT_A_STATE"
    with pytest.raises(pandera.errors.SchemaError):
        LEDGER_SCHEMA.validate(frame)


def test_maxcall_and_num_ctx_rule() -> None:
    usage = [_usage("t", "m1", 100, 10), _usage("t", "m1", 300, 30), _usage("t", "m2", 1500, 166)]
    rows = {r.model: r for r in maxcall_rows(usage)}
    assert (rows["m1"].calls, rows["m1"].max, rows["m1"].p50, rows["m1"].p95) == (2, 330, 110, 330)
    assert rows["m2"].max == 1666
    assert nearest_rank([1, 2, 3, 4], 0.5) == 2
    assert recommend_num_ctx(1666) == 4096
    assert recommend_num_ctx(2049) == 6144
    assert recommend_num_ctx(3500) == 8192
    assert recommend_num_ctx(9000) == 8192
