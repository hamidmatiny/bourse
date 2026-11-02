"""One ledger row per delegation attempt, from measured sources only (ADR-0029).

Tokens come from llm_usage (written by the metering proxy) and tool calls from tool_usage
(written by the MCP servers), both joined by task_id = run_id:subtask_id:attempt. Both are
written before the agent's response reaches the broker, so the join is complete when the
delegation returns.
"""

from typing import Literal

from bourse.contracts.broker import DelegationRecord, LedgerRow
from bourse.ledger.prices import PriceTable
from bourse.ledger.repo import LedgerRepository
from bourse.mcp_servers.usage import ToolUsageRepository
from bourse.meter.repo import MeterRepository


def agent_major(version: str) -> int:
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else 0


class LedgerRecorder:
    def __init__(
        self,
        repo: LedgerRepository,
        usage: MeterRepository,
        tools: ToolUsageRepository,
        prices: PriceTable,
    ) -> None:
        self.repo = repo
        self.usage = usage
        self.tools = tools
        self.prices = prices

    async def record(
        self,
        delegation: DelegationRecord,
        *,
        major: int,
        spot_check_passed: bool | None,
        kind: Literal["task", "canary"] = "task",
    ) -> LedgerRow:
        task_id = f"{delegation.run_id}:{delegation.subtask_id}:{delegation.attempt}"
        llm = await self.usage.list_usage(task_id=task_id)
        tool_calls = len(await self.tools.list_tool_usage(task_id=task_id))
        row = LedgerRow(
            run_id=delegation.run_id,
            subtask_id=delegation.subtask_id,
            attempt=delegation.attempt,
            agent_id=delegation.agent_id,
            skill=delegation.skill,
            revision_digest=delegation.revision_digest,
            agent_major=major,
            state=delegation.state,
            error_kind=delegation.error_kind,
            tokens_in=sum(u.tokens_in for u in llm),
            tokens_out=sum(u.tokens_out for u in llm),
            tool_calls=tool_calls,
            shadow_usd=self.prices.usd(llm, tool_calls),
            price_table_version=self.prices.version,
            wall_ms=delegation.wall_ms,
            spot_check_passed=spot_check_passed,
            at=delegation.at,
            kind=kind,
        )
        await self.repo.add_row(row)
        return row
