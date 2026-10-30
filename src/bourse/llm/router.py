"""Ordered provider router.

Every call: breaker.guard -> deadline -> provider -> budget.charge. Errors are classified by
the breaker first; billing/auth/quota trips it and stops the run. 5xx, plain 429 and
transport errors fall through to the next provider. Paid providers are skipped unless the
budget spec enables them.
"""

from collections.abc import Sequence

import httpx

from bourse.llm.base import ChatMessage, ChatResult, Provider, ToolSpec
from bourse.runtime.breaker import (
    GLOBAL_BREAKER,
    BreakerOpen,
    CircuitBreaker,
    ProviderHTTPError,
    Verdict,
)
from bourse.runtime.budget import BudgetLedger
from bourse.runtime.deadline import deadline


class AllProvidersFailed(RuntimeError):
    def __init__(self, errors: Sequence[tuple[str, BaseException]]) -> None:
        detail = "; ".join(f"{name}: {exc!r}" for name, exc in errors) or "no eligible provider"
        super().__init__(f"all providers failed: {detail}")
        self.errors = tuple(errors)


def _falls_through(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, ProviderHTTPError):
        return exc.status_code >= 500 or exc.status_code == 429
    return False


class Router:
    def __init__(
        self,
        providers: Sequence[Provider],
        ledger: BudgetLedger,
        breaker: CircuitBreaker = GLOBAL_BREAKER,
    ) -> None:
        self._providers = tuple(providers)
        self._ledger = ledger
        self._breaker = breaker

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        max_tokens: int,
        deadline_s: float,
    ) -> ChatResult:
        self._breaker.guard()
        capped = self._ledger.cap_call(max_tokens)
        errors: list[tuple[str, BaseException]] = []
        async with deadline(deadline_s):
            for provider in self._providers:
                if provider.paid and not self._ledger.spec.paid_enabled:
                    continue
                self._breaker.guard()
                try:
                    result = await provider.chat(messages, tools, capped, deadline_s)
                except (ProviderHTTPError, httpx.TransportError) as exc:
                    if self._breaker.record(exc) is Verdict.TRIP:
                        raise BreakerOpen(f"circuit breaker tripped by {provider.name}") from exc
                    if not _falls_through(exc):
                        raise
                    errors.append((provider.name, exc))
                    continue
                self._ledger.charge(result.usage, result.usd)
                return result
        raise AllProvidersFailed(errors)
