"""In-process factory for the three tool servers (tests) and HTTP serving (CLI)."""

from collections.abc import Callable, Mapping
from typing import Any, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from bourse.mcp_servers import pii, table, units
from bourse.mcp_servers.common import MAX_REQUEST_BODY_BYTES, SERVER_PORTS
from bourse.mcp_servers.usage import (
    SqlToolUsageRepository,
    ToolUsageMiddleware,
    ToolUsageRepository,
)

ServerName = Literal["table", "pii", "units"]

FACTORIES: dict[str, Callable[[], MCPServer[Any]]] = {
    "table": table.build_server,
    "pii": pii.build_server,
    "units": units.build_server,
}


def build_server(name: ServerName) -> MCPServer[Any]:
    return FACTORIES[name]()


def http_app(
    name: ServerName,
    keys: Mapping[str, str],
    repo: ToolUsageRepository,
    bind_host: str = "127.0.0.1",
) -> ASGIApp:
    """Streamable HTTP, stateless, JSON responses; agent key required, usage recorded.

    DNS-rebinding protection stays on: localhost names on the host; the compose service name
    (mcp-<name>) inside compose.
    """
    security = None
    if bind_host not in ("127.0.0.1", "localhost", "::1"):
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"mcp-{name}:*", f"mcp-{name}", "127.0.0.1:*", "localhost:*"],
            allowed_origins=[],
        )
    app = build_server(name).streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        host=bind_host,
        transport_security=security,
    )
    return ToolUsageMiddleware(app, server=name, keys=keys, repo=repo)


def serve(name: ServerName, port: int | None = None) -> None:
    """Bound to 127.0.0.1 only. Keys: BOURSE_METER_KEYS; rows: tool_usage in Postgres."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bourse.config import get_settings
    from bourse.meter.keys import parse_keys
    from bourse.telemetry import setup_tracing

    settings = get_settings()
    setup_tracing(f"bourse-mcp-{name}", settings.otel_exporter_otlp_endpoint)
    keys = parse_keys(settings.bourse_meter_keys.get_secret_value())
    repo = SqlToolUsageRepository(create_async_engine(settings.bourse_database_url))
    host = settings.bourse_bind_host
    uvicorn.run(http_app(name, keys, repo, host), host=host, port=port or SERVER_PORTS[name])
