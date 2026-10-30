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
