import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from pydantic import BaseModel

from bourse.contracts.metering import ToolCallRecord
from bourse.mcp_adapter.adapter import (
    ToolBudget,
    ToolBudgetExceeded,
    ToolCallFailed,
    ToolClient,
    ToolNotAllowed,
    ToolResultInvalid,
    mcp_tools_as_langchain,
)
from bourse.mcp_servers.pii import build_server as build_pii
from bourse.mcp_servers.units import build_server as build_units
from bourse.runtime.deadline import DeadlineExceeded


async def test_only_allowlisted_tools_exposed() -> None:
    tools = await mcp_tools_as_langchain(build_pii(), {"luhn_check"}, deadline_s=5, max_calls=5)
    assert [t.name for t in tools] == ["luhn_check"]
    out = await tools[0].ainvoke({"numbers": ["4111 1111 1111 1111"]})
    assert out == {"results": [True]}


async def test_missing_allowlisted_tool_raises_at_construction() -> None:
    with pytest.raises(ToolNotAllowed, match="shell_exec"):
        await mcp_tools_as_langchain(build_pii(), {"luhn_check", "shell_exec"})


async def test_max_calls_budget_shared_across_tools() -> None:
    records: list[ToolCallRecord] = []
    budget = ToolBudget(3)
    pii = await mcp_tools_as_langchain(
        build_pii(), {"luhn_check"}, budget=budget, on_call=records.append
    )
    units = await mcp_tools_as_langchain(
        build_units(), {"convert"}, budget=budget, on_call=records.append
    )
    await pii[0].ainvoke({"numbers": ["0"]})
    await units[0].ainvoke({"value": 1.0, "from_unit": "km", "to_unit": "m"})
    await pii[0].ainvoke({"numbers": ["0"]})
    with pytest.raises(ToolBudgetExceeded):
        await units[0].ainvoke({"value": 1.0, "from_unit": "km", "to_unit": "m"})
    assert budget.used == 3
    assert [(r.tool, r.ok) for r in records] == [
        ("luhn_check", True),
        ("convert", True),
        ("luhn_check", True),
    ]
    assert all(r.wall_ms >= 0 for r in records)


async def test_tool_error_is_raised_and_recorded() -> None:
    records: list[ToolCallRecord] = []
    tools = await mcp_tools_as_langchain(build_units(), {"convert"}, on_call=records.append)
    with pytest.raises(ToolCallFailed, match="cannot convert"):
        await tools[0].ainvoke({"value": 1.0, "from_unit": "kg", "to_unit": "m"})
    assert records[-1].ok is False and records[-1].error_kind == "tool_error"


class _Out(BaseModel):
    value: float


def _slow_server() -> MCPServer[Any]:
    server = MCPServer("slow")

    @server.tool()
    async def slow() -> _Out:
        """Sleeps far past the deadline."""
        await anyio.sleep(10)
        return _Out(value=1.0)

    return server


async def test_per_call_deadline() -> None:
    records: list[ToolCallRecord] = []
    tools = await mcp_tools_as_langchain(
        _slow_server(), {"slow"}, deadline_s=0.5, on_call=records.append
    )
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        await tools[0].ainvoke({})
    assert 0.5 <= time.monotonic() - started <= 1.0
    assert records[-1].error_kind == "deadline"


class _FakeClient:
    """Lists one tool with an output schema, then returns structured content that violates it."""

    def __init__(self, structured: Any) -> None:
        self.structured = structured

    async def list_tools(self) -> ListToolsResult:
        tool = Tool(
            name="measure",
            description="returns a number",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "properties": {"value": {"type": "number"}},
                "required": ["value"],
            },
        )
        return ListToolsResult(tools=[tool])

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text="{}")], structured_content=self.structured
        )


def _factory(structured: Any) -> Any:
    @asynccontextmanager
    async def factory() -> AsyncIterator[ToolClient]:
        yield _FakeClient(structured)

    return factory


@pytest.mark.parametrize(
    "structured", [{"value": "not a number"}, {}, None], ids=["wrong-type", "missing", "absent"]
)
async def test_schema_invalid_result(structured: Any) -> None:
    records: list[ToolCallRecord] = []
    tools = await mcp_tools_as_langchain(
        "http://unused.test/mcp",
        {"measure"},
        client_factory=_factory(structured),
        on_call=records.append,
    )
    with pytest.raises(ToolResultInvalid):
        await tools[0].ainvoke({})
    assert records[-1].error_kind == "invalid_result"


async def test_valid_fake_result_passes() -> None:
    tools = await mcp_tools_as_langchain(
        "http://unused.test/mcp", {"measure"}, client_factory=_factory({"value": 2.5})
    )
    assert await tools[0].ainvoke({}) == {"value": 2.5}
