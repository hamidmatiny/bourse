"""Shared limits for the pure MCP tool servers (ADR-0016)."""

from collections.abc import Callable
from typing import TypeVar

from anyio import to_thread
from mcp.server.mcpserver.exceptions import ToolError

from bourse.runtime.deadline import DeadlineExceeded, deadline

T = TypeVar("T")

MAX_INPUT_BYTES = 256 * 1024
TOOL_DEADLINE_S = 10.0
# HTTP body cap: the 256 KiB payload plus JSON-RPC framing and escaping.
MAX_REQUEST_BODY_BYTES = 1024 * 1024

SERVER_PORTS = {"table": 55101, "pii": 55102, "units": 55103}


def check_size(*texts: str) -> None:
    size = sum(len(t.encode("utf-8")) for t in texts)
    if size > MAX_INPUT_BYTES:
        raise ToolError(f"input is {size} bytes > {MAX_INPUT_BYTES}")


async def run_limited(fn: Callable[[], T], deadline_s: float = TOOL_DEADLINE_S) -> T:
    """Run blocking tool work in a worker thread under a wall-clock deadline."""
    try:
        async with deadline(deadline_s):
            result = await to_thread.run_sync(fn, abandon_on_cancel=True)
    except DeadlineExceeded as exc:
        raise ToolError(f"tool deadline of {deadline_s}s exceeded") from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return result
