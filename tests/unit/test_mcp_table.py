from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from bourse.contracts.skills_io import SchemaProfileOut
from bourse.mcp_servers.common import MAX_INPUT_BYTES
from bourse.mcp_servers.table import (
    build_server,
    column_stats_impl,
    profile_columns_impl,
    sample_rows_impl,
)
from tests.datasets import NAMES, csv_text, truth

TOL = 1e-9


@pytest.mark.parametrize("name", NAMES)
def test_profile_matches_truth(name: str) -> None:
    assert profile_columns_impl(csv_text(name)).model_dump() == truth(name)["profile"]


@pytest.mark.parametrize("name", NAMES)
def test_stats_match_truth(name: str) -> None:
    got = column_stats_impl(csv_text(name)).model_dump()["stats"]
    want: list[dict[str, Any]] = truth(name)["stats"]["stats"]
    assert [s["column"] for s in got] == [s["column"] for s in want]
    for g, w in zip(got, want, strict=True):
        for key in ("mean", "std", "min", "max", "p50"):
            assert g[key] == pytest.approx(w[key], abs=TOL, rel=0), (g["column"], key)


def test_stats_selected_and_non_numeric_columns() -> None:
    text = csv_text("measurements")
    assert [s.column for s in column_stats_impl(text, ["temp_F"]).stats] == ["temp_F"]
    with pytest.raises(ValueError, match="not numeric"):
        column_stats_impl(text, ["site"])
    with pytest.raises(ValueError, match="unknown column"):
        column_stats_impl(text, ["nope"])


def test_sample_rows() -> None:
    out = sample_rows_impl(csv_text("orders"), 3)
    assert out.columns[0] == "order_id"
    assert [r[0] for r in out.rows] == ["5000", "5001", "5002"]
    with pytest.raises(ValueError):
        sample_rows_impl(csv_text("orders"), 21)


def test_null_and_type_rules() -> None:
    out = profile_columns_impl("a,b,c,d\n1,,true,2024-02-30\n2.5,,FALSE,2024-02-01\n")
    assert [(c.dtype, c.null_count) for c in out.columns] == [
        ("float", 0),
        ("string", 2),
        ("bool", 0),
        ("string", 0),  # 2024-02-30 is not a valid date
    ]


def test_size_cap_rejected() -> None:
    text = "a\n" + "1\n" * (MAX_INPUT_BYTES // 2 + 1)
    with pytest.raises(ToolError, match="bytes"):
        profile_columns_impl(text)


def test_ragged_rows_rejected() -> None:
    with pytest.raises(ValueError, match="cells"):
        profile_columns_impl("a,b\n1\n")


async def test_mcp_round_trip_profile_columns() -> None:
    async with Client(build_server()) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {"profile_columns", "column_stats", "sample_rows"}
        result = await client.call_tool("profile_columns", {"csv_text": csv_text("people")})
    assert not result.is_error
    assert (
        SchemaProfileOut.model_validate(result.structured_content).model_dump()
        == truth("people")["profile"]
    )


async def test_mcp_size_cap_is_tool_error() -> None:
    text = "a\n" + "1\n" * (MAX_INPUT_BYTES // 2 + 1)
    async with Client(build_server()) as client:
        result = await client.call_tool("profile_columns", {"csv_text": text})
    assert result.is_error
