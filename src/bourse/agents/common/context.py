"""Per-task context: metered LLM router, MCP tools under a shared per-task budget."""

from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import StructuredTool

from bourse.agents.common.errors import TaskFailed
from bourse.agents.common.spec import METER_TASK_HEADER, AgentRuntime, AgentSpec
from bourse.contracts.metering import BudgetSpec, ToolCallRecord
from bourse.contracts.skills import SkillId
from bourse.llm.omniroute import OpenAICompatProvider
from bourse.llm.router import Router
from bourse.mcp_adapter.adapter import ToolBudget, mcp_tools_as_langchain
from bourse.runtime.breaker import CircuitBreaker
from bourse.runtime.budget import BudgetLedger
from bourse.telemetry import trace_headers

AGENT_KEY_HEADER = "X-Bourse-Agent-Key"


@dataclass
class TaskContext:
    spec: AgentSpec
    runtime: AgentRuntime
    skill: SkillId
    task_id: str
    deadline_s: float
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    _tools: dict[str, dict[str, StructuredTool]] = field(default_factory=dict)
    _budget: ToolBudget | None = None
    _router: Router | None = None

    @property
    def budget(self) -> ToolBudget:
        if self._budget is None:
            self._budget = ToolBudget(self.spec.max_tool_calls)
        return self._budget

    def router(self) -> Router:
        """LLM calls go only to the metering proxy, tagged with the task id."""
        if self._router is None:
            provider = OpenAICompatProvider(
                self.runtime.meter_url,
                self.spec.model,
                self.runtime.meter_key,
                self.runtime.http_client,
                provider_name="meter",
                headers={METER_TASK_HEADER: self.task_id},
            )
            ledger = BudgetLedger(
                BudgetSpec(
                    max_tokens_per_call=self.spec.max_tokens_per_call,
                    max_tokens_run=self.spec.max_tokens_run,
                    wall_budget_s=self.deadline_s,
                ),
                run_id=self.task_id,
            )
            self._router = Router([provider], ledger, CircuitBreaker())
        return self._router

    def mcp_headers(self) -> dict[str, str]:
        """Identify the agent and task to the MCP server; carry the trace (ADR-0029/0032)."""
        return {
            AGENT_KEY_HEADER: self.runtime.meter_key,
            METER_TASK_HEADER: self.task_id,
            **trace_headers(),
        }

    async def call_tool(self, server: str, tool: str, **arguments: Any) -> Any:
        allowed = self.spec.mcp.get(server, frozenset())
        if tool not in allowed:
            raise TaskFailed("tool_not_allowed", f"{server}.{tool}")
        if server not in self._tools:
            target = self.runtime.mcp_servers.get(server)
            if target is None:
                raise TaskFailed("tool_unavailable", server)
            built = await mcp_tools_as_langchain(
                target,
                set(allowed),
                deadline_s=self.runtime.mcp_deadline_s,
                budget=self.budget,
                on_call=self.tool_calls.append,
                headers=self.mcp_headers,
            )
            self._tools[server] = {t.name: t for t in built}
        return await self._tools[server][tool].ainvoke(arguments)
