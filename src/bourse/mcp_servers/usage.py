"""Server-side MCP tool usage (ADR-0029).

`ToolUsageMiddleware` wraps a tool server's streamable HTTP app:
- every request must carry `X-Bourse-Agent-Key` (a per-agent key, same keys as the metering
  proxy); unknown or missing key -> 401, the tool is never called;
- for JSON-RPC `tools/call` it times the call and appends one tool_usage row
  (agent_id, task_id from X-Bourse-Task-Id, "<server>.<tool>", wall_ms, ok) BEFORE the response
  reaches the agent, so a ledger row written after the delegation always sees it;
- it opens a "tool call" span whose parent comes from the request's traceparent.
Agents never report their own tool usage.
"""

import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from opentelemetry import context, propagate
from opentelemetry.trace import SpanKind
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    Identity,
    MetaData,
    Table,
    Text,
    insert,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bourse.contracts.metering import ToolUsageRecord
from bourse.mcp_servers.common import MAX_REQUEST_BODY_BYTES
from bourse.meter.keys import agent_for
from bourse.telemetry import tracer

AGENT_KEY_HEADER = "x-bourse-agent-key"
TASK_HEADER = "x-bourse-task-id"


class ToolUsageRepository(Protocol):
    async def add_tool_usage(self, record: ToolUsageRecord) -> None: ...

    async def list_tool_usage(
        self, task_id: str | None = None, run_id: str | None = None
    ) -> list[ToolUsageRecord]:
        """`run_id` matches task ids run_id:subtask:attempt (prefix run_id + ":")."""
        ...


class InMemoryToolUsageRepository:
    def __init__(self) -> None:
        self.rows: list[ToolUsageRecord] = []

    async def add_tool_usage(self, record: ToolUsageRecord) -> None:
        self.rows.append(record)

    async def list_tool_usage(
        self, task_id: str | None = None, run_id: str | None = None
    ) -> list[ToolUsageRecord]:
        return [
            r
            for r in self.rows
            if (task_id is None or r.task_id == task_id)
            and (run_id is None or (r.task_id or "").startswith(run_id + ":"))
        ]


metadata = MetaData()
tool_usage = Table(
    "tool_usage",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("agent_id", Text, nullable=False),
    Column("task_id", Text),
    Column("tool", Text, nullable=False),
    Column("wall_ms", Float, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("at", DateTime(timezone=True), nullable=False),
)


class SqlToolUsageRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_tool_usage(self, record: ToolUsageRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(insert(tool_usage).values(**record.model_dump()))

    async def list_tool_usage(
        self, task_id: str | None = None, run_id: str | None = None
    ) -> list[ToolUsageRecord]:
        stmt = select(tool_usage).order_by(tool_usage.c.id)
        if task_id is not None:
            stmt = stmt.where(tool_usage.c.task_id == task_id)
        if run_id is not None:
            stmt = stmt.where(tool_usage.c.task_id.startswith(run_id + ":", autoescape=True))
        async with self._engine.connect() as conn:
            rows = (await conn.execute(stmt)).mappings().all()
        return [
            ToolUsageRecord.model_validate({k: v for k, v in r.items() if k != "id"}) for r in rows
        ]


def _tool_name(body: bytes) -> str | None:
    try:
        msg: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return None
    params = msg.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return name if isinstance(name, str) else None


def _call_ok(status: int, body: bytes) -> bool:
    if status != 200:
        return False
    try:
        msg: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(msg, dict) or "error" in msg:
        return False
    result = msg.get("result")
    return isinstance(result, dict) and not result.get("isError", False)


class ToolUsageMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        server: str,
        keys: Mapping[str, str],
        repo: ToolUsageRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.app = app
        self.server = server
        self.keys = keys
        self.repo = repo
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        agent_id = agent_for(f"Bearer {headers.get(AGENT_KEY_HEADER, '')}", self.keys)
        if agent_id is None:
            await _reject(send, 401, "unknown or missing X-Bourse-Agent-Key")
            return

        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunks.append(message.get("body", b""))
            if sum(len(c) for c in chunks) > MAX_REQUEST_BODY_BYTES:
                await _reject(send, 413, "request body too large")
                return
            more = bool(message.get("more_body", False))
        body = b"".join(chunks)
        tool = _tool_name(body)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        if tool is None:
            await self.app(scope, replay, send)
            return

        held: list[Message] = []
        status = 0

        async def hold(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            held.append(message)

        token = context.attach(propagate.extract(headers))
        started = time.monotonic()
        try:
            with tracer().start_as_current_span("tool call", kind=SpanKind.SERVER) as span:
                await self.app(scope, replay, hold)
                response = b"".join(
                    m.get("body", b"") for m in held if m["type"] == "http.response.body"
                )
                ok = _call_ok(status, response)
                full = f"{self.server}.{tool}"
                span.set_attribute("tool", full)
                span.set_attribute("agent_id", agent_id)
                span.set_attribute("outcome", "ok" if ok else "error")
                task_id = headers.get(TASK_HEADER)
                if task_id:
                    span.set_attribute("task_id", task_id)
                await self.repo.add_tool_usage(
                    ToolUsageRecord(
                        agent_id=agent_id,
                        task_id=task_id,
                        tool=full,
                        wall_ms=(time.monotonic() - started) * 1000,
                        ok=ok,
                        at=self.clock(),
                    )
                )
        finally:
            context.detach(token)
        for message in held:
            await send(message)


async def _reject(send: Send, status: int, detail: str) -> None:
    payload = json.dumps({"error": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": payload})
