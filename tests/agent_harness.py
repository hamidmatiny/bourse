"""In-process agent harness: fake metering proxy (respx), in-process MCP servers,
A2A client over ASGI. No sockets are opened."""

import json
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import httpx2
import respx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from bourse.agents.catalog import EXECUTORS, agent_specs
from bourse.agents.common.app import build_agent_app
from bourse.agents.common.card import signed_card
from bourse.agents.common.spec import AgentRuntime, AgentSpec
from bourse.contracts.skills_io import DatasetRef, SchemaProfileOut, StatsOut
from bourse.mcp_servers.pii import ChecksOut, RegexScanOut
from bourse.mcp_servers.pii import build_server as build_pii
from bourse.mcp_servers.table import SampleRowsOut
from bourse.mcp_servers.table import build_server as build_table
from bourse.mcp_servers.units import ConvertOut, HeaderUnitOut
from bourse.mcp_servers.units import build_server as build_units
from bourse.stack.smoke import CallResult, send_task
from tests.datasets import csv_text
from tests.registry_helpers import Party, make_party

METER = "http://meter.test"
METER_CHAT = f"{METER}/v1/chat/completions"
AGENT_URL = "http://agent.test"
LARGE, SMALL = "qwen-test", "gemma-test"


def openai_response(message: dict[str, Any]) -> dict[str, Any]:
    finish = "tool_calls" if message.get("tool_calls") else "stop"
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "fake",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def submit_call(arguments: dict[str, Any]) -> httpx.Response:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_0",
                "type": "function",
                "function": {"name": "submit", "arguments": json.dumps(arguments)},
            }
        ],
    }
    return httpx.Response(200, json=openai_response(message))


def react_response(text: str) -> httpx2.Response:
    return httpx2.Response(200, json=openai_response({"role": "assistant", "content": text}))


def dataset_payload(name: str) -> dict[str, Any]:
    return {"dataset": DatasetRef.from_csv(csv_text(name)).model_dump(mode="json")}


def broken_server(kind: str) -> MCPServer[Any]:
    """Same tool names and signatures as the real servers; every call is a tool error."""
    server: MCPServer[Any] = MCPServer(f"broken-{kind}")
    if kind == "table":

        @server.tool()
        async def profile_columns(csv_text: str) -> SchemaProfileOut:
            raise ToolError("broken")

        @server.tool()
        async def column_stats(csv_text: str, columns: list[str] | None = None) -> StatsOut:
            raise ToolError("broken")

        @server.tool()
        async def sample_rows(csv_text: str, n: int = 5) -> SampleRowsOut:
            raise ToolError("broken")

    elif kind == "pii":

        @server.tool()
        async def regex_scan(values: list[str]) -> RegexScanOut:
            raise ToolError("broken")

        @server.tool()
        async def luhn_check(numbers: list[str]) -> ChecksOut:
            raise ToolError("broken")

        @server.tool()
        async def sin_check(numbers: list[str]) -> ChecksOut:
            raise ToolError("broken")

    else:

        @server.tool()
        async def parse_header_unit(header: str) -> HeaderUnitOut:
            raise ToolError("broken")

        @server.tool()
        async def convert(value: float, from_unit: str, to_unit: str) -> ConvertOut:
            raise ToolError("broken")

    return server


MeterHandler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
CrewHandler = Callable[[httpx2.Request], httpx2.Response]


class AgentHarness:
    def __init__(
        self,
        name: str,
        meter: MeterHandler | None = None,
        *,
        crew: CrewHandler | None = None,
        broken: set[str] | None = None,
    ) -> None:
        self.spec: AgentSpec = agent_specs(LARGE, SMALL)[name]
        self.party: Party = make_party(self.spec.vendor_domain)
        self.meter_requests: list[httpx.Request] = []
        self.router = respx.Router(assert_all_called=False)

        async def record(request: httpx.Request) -> httpx.Response:
            self.meter_requests.append(request)
            if meter is None:
                return httpx.Response(500, json={"error": {"message": "unexpected LLM call"}})
            result = meter(request)
            if isinstance(result, httpx.Response):
                return result
            return await result

        self.router.post(METER_CHAT).mock(side_effect=record)
        servers: dict[str, Any] = {
            "table": build_table(),
            "pii": build_pii(),
            "units": build_units(),
        }
        for kind in broken or set():
            servers[kind] = broken_server(kind)
        crew_params = (
            {"http_client": httpx2.Client(transport=httpx2.MockTransport(crew))} if crew else None
        )
        self.runtime = AgentRuntime(
            meter_url=METER,
            meter_key=secrets.token_hex(16),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.router.async_handler)),
            mcp_servers=servers,
            mcp_deadline_s=10.0,
            crewai_client_params=crew_params,
        )
        executor = EXECUTORS[name](self.spec, self.runtime)
        self.card = signed_card(self.spec, AGENT_URL, self.party.key)
        self.app = build_agent_app(executor, self.card)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=AGENT_URL, timeout=60
        )

    async def send(
        self,
        skill_id: str,
        payload: Any,
        *,
        task_id: str = "t-1",
        deadline_s: float = 30.0,
    ) -> CallResult:
        async with self.client() as http:
            return await send_task(AGENT_URL, skill_id, payload, task_id, deadline_s, http)
