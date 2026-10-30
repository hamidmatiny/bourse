"""MCP tools as LangChain StructuredTools, built on the mcp 2.x Client (ADR-0003, ADR-0018).

- Only allowlisted tools are exposed; an allowlisted name the server does not list raises at
  construction.
- Every call: shared per-agent budget (ToolBudgetExceeded once spent, before contacting the
  server) -> wall-clock deadline around connect + call -> result checked against the tool's
  output schema (ToolResultInvalid) -> ToolCallRecord to the on_call callback.
- Servers are stateless, so each call opens its own client connection.
"""

import threading
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from langchain_core.tools import StructuredTool
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from bourse.contracts.metering import ToolCallRecord
from bourse.runtime.deadline import DeadlineExceeded, deadline


class ToolNotAllowed(ValueError):
    pass


class ToolResultInvalid(RuntimeError):
    pass


class ToolCallFailed(RuntimeError):
    """The server reported the call as an error (is_error=True)."""


class ToolBudgetExceeded(RuntimeError):
    pass


class ToolClient(Protocol):
    async def list_tools(self) -> ListToolsResult: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> CallToolResult: ...


ClientFactory = Callable[[], AbstractAsyncContextManager[ToolClient]]


class ToolBudget:
    """Per-agent call budget shared by every tool built with it. Thread-safe."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def take(self, tool: str) -> None:
        with self._lock:
            if self._used >= self.max_calls:
                raise ToolBudgetExceeded(f"{tool}: tool budget of {self.max_calls} calls spent")
            self._used += 1


def _error_text(result: CallToolResult) -> str:
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    return " ".join(texts)[:500] or "tool reported an error"


def _make_tool(
    tool: Tool,
    factory: ClientFactory,
    budget: ToolBudget,
    deadline_s: float,
    on_call: Callable[[ToolCallRecord], None] | None,
) -> StructuredTool:
    validator = Draft202012Validator(tool.output_schema) if tool.output_schema else None
    name = tool.name

    async def run(**kwargs: Any) -> Any:
        budget.take(name)
        started = time.monotonic()
        error_kind: str | None = None
        try:
            async with deadline(deadline_s), factory() as client:
                result = await client.call_tool(name, kwargs)
            if result.is_error:
                error_kind = "tool_error"
                raise ToolCallFailed(f"{name}: {_error_text(result)}")
            if validator is not None:
                content = result.structured_content
                if content is None:
                    error_kind = "invalid_result"
                    raise ToolResultInvalid(f"{name}: no structured content")
                errors = sorted(str(e.message) for e in validator.iter_errors(content))
                if errors:
                    error_kind = "invalid_result"
                    raise ToolResultInvalid(f"{name}: {errors[0]}")
                return content
            return _error_text(result) if result.content else None
        except DeadlineExceeded:
            error_kind = "deadline"
            raise
        except (ToolCallFailed, ToolResultInvalid):
            raise
        except RuntimeError as exc:
            # mcp's ClientSession validates structured content itself and raises RuntimeError.
            if "structured content" in str(exc):
                error_kind = "invalid_result"
                raise ToolResultInvalid(f"{name}: {exc}") from exc
            error_kind = "client_error"
            raise
        except Exception:
            error_kind = error_kind or "client_error"
            raise
        finally:
            if on_call is not None:
                wall_ms = (time.monotonic() - started) * 1000
                on_call(
                    ToolCallRecord(
                        tool=name, wall_ms=wall_ms, ok=error_kind is None, error_kind=error_kind
                    )
                )

    return StructuredTool.from_function(
        coroutine=run,
        name=name,
        description=tool.description or name,
        args_schema=tool.input_schema,
    )


async def mcp_tools_as_langchain(
    server: str | MCPServer[Any],
    allowlist: set[str],
    deadline_s: float = 30.0,
    max_calls: int = 20,
    *,
    budget: ToolBudget | None = None,
    on_call: Callable[[ToolCallRecord], None] | None = None,
    client_factory: ClientFactory | None = None,
) -> list[StructuredTool]:
    """Build LangChain tools for the allowlisted MCP tools on `server` (URL or in-process)."""

    def default_factory() -> AbstractAsyncContextManager[ToolClient]:
        return Client(server)

    factory: ClientFactory = client_factory or default_factory
    async with deadline(deadline_s), factory() as client:
        listing = await client.list_tools()
    listed = {t.name: t for t in listing.tools}
    missing = sorted(allowlist - listed.keys())
    if missing:
        raise ToolNotAllowed(f"allowlisted tools not offered by server: {missing}")
    shared = budget or ToolBudget(max_calls)
    return [
        _make_tool(listed[name], factory, shared, deadline_s, on_call) for name in sorted(allowlist)
    ]
