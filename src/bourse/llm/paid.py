"""Paid OpenAI-compatible provider. Hard-disabled unless allow_paid and usd_cap > 0 (ADR-0005)."""

from collections.abc import Sequence
from decimal import Decimal

import httpx

from bourse.contracts.metering import BudgetSpec
from bourse.llm.base import ChatMessage, ChatResult, ToolSpec
from bourse.llm.omniroute import OpenAICompatProvider


class PaidDisabled(RuntimeError):
    pass


class PaidProvider(OpenAICompatProvider):
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        spec: BudgetSpec,
        usd_per_mtok_in: Decimal,
        usd_per_mtok_out: Decimal,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url, model, api_key, client, provider_name="paid")
        self._spec = spec
        self._usd_per_mtok_in = usd_per_mtok_in
        self._usd_per_mtok_out = usd_per_mtok_out

    @property
    def paid(self) -> bool:
        return True

    def price(self, tokens_in: int, tokens_out: int) -> Decimal:
        mtok = Decimal(1_000_000)
        return (tokens_in * self._usd_per_mtok_in + tokens_out * self._usd_per_mtok_out) / mtok

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        deadline_s: float,
    ) -> ChatResult:
        if not self._spec.paid_enabled:
            raise PaidDisabled("paid providers require --allow-paid and --usd-cap > 0")
        return await super().chat(messages, tools, max_tokens, deadline_s)
