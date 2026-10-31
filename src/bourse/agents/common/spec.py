"""Static agent specs and runtime wiring (ADR-0021)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer

from bourse.contracts.skills import SkillId

Framework = Literal["langgraph", "crewai", "a2a-sdk"]

MAX_DEADLINE_S = 600.0
METER_TASK_HEADER = "X-Bourse-Task-Id"


@dataclass(frozen=True)
class AgentSpec:
    name: str
    framework: Framework
    vendor_domain: str
    vendor_org: str
    display_name: str
    description: str
    version: str
    skills: tuple[SkillId, ...]
    model: str
    port: int
    mcp: Mapping[str, frozenset[str]]  # server -> allowlisted tool names
    max_tool_calls: int = 40
    max_tokens_per_call: int = 1024
    max_tokens_run: int = 20_000
    default_deadline_s: float = 300.0


@dataclass
class AgentRuntime:
    """Process wiring. Tests pass in-process MCP servers and mocked HTTP clients."""

    meter_url: str
    meter_key: str
    http_client: httpx.AsyncClient
    mcp_servers: Mapping[str, str | MCPServer[Any]] = field(default_factory=dict)
    mcp_deadline_s: float = 30.0
    # CrewAI only: extra kwargs for its OpenAI client (tests inject an httpx2 mock client).
    crewai_client_params: dict[str, Any] | None = None
