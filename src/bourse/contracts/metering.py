"""Token/wall-time usage, ledger entries and budget limits (ADR-0005, ADR-0007)."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    wall_ms: float = Field(ge=0)
    model: str
    provider: str


class LedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    seq: int = Field(ge=0)
    usage: Usage
    usd: Decimal = Field(default=Decimal(0), ge=0)
    tokens_run_total: int = Field(ge=0)
    at: datetime


class TaskBudget(BaseModel):
    """Per-delegation ceiling registered by the broker before send (ADR-0041)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=200)
    max_tokens: int = Field(gt=0)
    max_calls: int = Field(gt=0)


class TaskBudgetStatus(BaseModel):
    """Meter-side state of one task (GET /budgets/status, broker key; ADR-0049 amendment 2).

    The exam reads this when a task fails or times out, to attribute the failure from meter
    evidence instead of the agent's own report. calls/tokens count admitted, completed calls;
    in_flight counts requests inside the meter (gate queue + upstream) right now; refusals are
    the budget codes the meter returned for this task.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    registered: bool
    max_tokens: int | None = None
    max_calls: int | None = None
    calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    in_flight: int = Field(default=0, ge=0)
    refusals: tuple[str, ...] = ()


class BudgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tokens_per_call: int = Field(gt=0)
    max_tokens_run: int = Field(gt=0)
    wall_budget_s: float = Field(gt=0)
    usd_cap: Decimal = Field(default=Decimal(0), ge=0)
    allow_paid: bool = False

    @property
    def paid_enabled(self) -> bool:
        return self.allow_paid and self.usd_cap > 0


class ToolCallRecord(BaseModel):
    """One MCP tool call made through the adapter (persisted in Phase 4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    wall_ms: float = Field(ge=0)
    ok: bool
    error_kind: str | None = None


class LlmUsageRecord(BaseModel):
    """One request through the metering proxy. Token counts come from the upstream response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    task_id: str | None
    provider: str | None
    model: str
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    wall_ms: float = Field(ge=0)
    status: int
    error_kind: str | None
    at: datetime
    # Fair-share + global OLLAMA_NUM_PARALLEL gate wait (ADR-0042 r2 / ADR-0044).
    queue_wait_ms: float = Field(default=0.0, ge=0)
    # Upstream Ollama timings (ns→ms); null when the call failed before a body.
    total_duration_ms: float | None = None
    load_duration_ms: float | None = None
    prompt_eval_duration_ms: float | None = None
    eval_duration_ms: float | None = None
    reload: bool = False  # load_duration_ms > 1000
    # Meter's deterministic prompt estimate used by the hard task cap (ADR-0041 amendment);
    # None for rows written before Phase 6b or for requests that did not parse.
    prompt_tokens_est: int | None = Field(default=None, ge=0)


class ToolUsageRecord(BaseModel):
    """One MCP tool call, recorded by the MCP server (not by the agent). Append-only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    task_id: str | None
    tool: str  # "<server>.<tool>"
    wall_ms: float = Field(ge=0)
    ok: bool
    at: datetime
