"""One broker run = one trace: run -> subtask -> delegation attempt -> agent task (ASGI)."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from opentelemetry.sdk.trace import ReadableSpan

from bourse.contracts.broker import RunStatus
from bourse.mcp_adapter.adapter import ToolClient, mcp_tools_as_langchain
from bourse.mcp_servers.common import MAX_REQUEST_BODY_BYTES
from bourse.mcp_servers.registry import build_server
from bourse.mcp_servers.usage import InMemoryToolUsageRepository, ToolUsageMiddleware
from bourse.telemetry import trace_headers, tracer
from tests.broker_harness import BrokerBed, intake, one_agent_per_skill
from tests.otel_memory import memory_exporter


def _parent(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


async def test_run_is_one_trace_with_parent_chain() -> None:
    bed = await BrokerBed(one_agent_per_skill()).setup()
    exporter = memory_exporter()  # after setup: registration card fetches are not the run
    try:
        report = await bed.broker().start(intake(), run_id="run-otel")
    finally:
        await bed.aclose()
    assert report.status is RunStatus.SUCCEEDED
    spans = exporter.get_finished_spans()
    by_name: dict[str, list[ReadableSpan]] = {}
    for s in spans:
        by_name.setdefault(s.name, []).append(s)
    assert {"run", "plan", "subtask", "delegation attempt", "agent task"} <= set(by_name)
    (run,) = by_name["run"]
    trace_id = run.context.trace_id
    assert report.trace_id == format(trace_id, "032x")
    assert {s.context.trace_id for s in spans} == {trace_id}
    ids = {s.context.span_id: s for s in spans}
    assert _parent(by_name["plan"][0]) == run.context.span_id
    assert len(by_name["subtask"]) == 5
    for sub in by_name["subtask"]:
        assert _parent(sub) == run.context.span_id
    for d in by_name["delegation attempt"]:
        assert ids[_parent(d) or 0].name == "subtask"
        assert d.attributes is not None and d.attributes["outcome"] == "COMPLETED"
    agent_tasks = by_name["agent task"]
    assert len(agent_tasks) == 5
    for a in agent_tasks:
        assert ids[_parent(a) or 0].name == "delegation attempt"


async def test_tool_call_span_is_child_of_caller() -> None:
    exporter = memory_exporter()
    key = secrets.token_hex(16)
    inner = build_server("pii").streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=MAX_REQUEST_BODY_BYTES
    )
    app = ToolUsageMiddleware(
        inner, server="pii", keys={key: "agent-x"}, repo=InMemoryToolUsageRepository()
    )
    url = "http://127.0.0.1:55102/mcp"

    @asynccontextmanager
    async def connect() -> AsyncIterator[ToolClient]:
        headers = {"X-Bourse-Agent-Key": key, **trace_headers()}
        async with (
            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
            Client(streamable_http_client(url, http_client=http)) as client,
        ):
            yield client

    async with inner.router.lifespan_context(inner):
        with tracer().start_as_current_span("caller") as caller:
            (tool,) = await mcp_tools_as_langchain(url, {"luhn_check"}, client_factory=connect)
            await tool.ainvoke({"numbers": ["4111111111111111"]})
    (call,) = [s for s in exporter.get_finished_spans() if s.name == "tool call"]
    assert call.context.trace_id == caller.get_span_context().trace_id
    assert _parent(call) == caller.get_span_context().span_id
