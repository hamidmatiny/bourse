"""In-process factory for the three tool servers (tests) and HTTP serving (CLI)."""

from collections.abc import Callable
from typing import Any, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer

from bourse.mcp_servers import pii, table, units
from bourse.mcp_servers.common import MAX_REQUEST_BODY_BYTES, SERVER_PORTS

ServerName = Literal["table", "pii", "units"]

FACTORIES: dict[str, Callable[[], MCPServer[Any]]] = {
    "table": table.build_server,
    "pii": pii.build_server,
    "units": units.build_server,
}


def build_server(name: ServerName) -> MCPServer[Any]:
    return FACTORIES[name]()


def serve(name: ServerName, port: int | None = None) -> None:
    """Streamable HTTP, stateless, JSON responses, bound to 127.0.0.1 only."""
    app = build_server(name).streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        host="127.0.0.1",
    )
    uvicorn.run(app, host="127.0.0.1", port=port or SERVER_PORTS[name])
