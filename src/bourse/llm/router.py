"""Ordered provider router.

Every call: breaker.guard -> deadline -> provider -> budget.charge. Errors are classified by
the breaker first; billing/auth/quota trips it and stops the run. 5xx, plain 429 and
transport errors fall through to the next provider. Paid providers are skipped unless the
budget spec enables them. PAID_CALL approval (ADR-0048) when projected USD > 50% of usd_cap.
"""

from collections.abc import Sequence
from decimal import Decimal

import httpx

from bourse.approval.base import Approver
from bourse.approval.triggers import paid_call_needs_approval, request_paid_call
from bourse.llm.base import ChatMessage, ChatResult, Provider, ToolSpec
from bourse.llm.paid import PaidDisabled
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
        approver: Approver | None = None,
        paid_timeout_s: float = 900.0,
    ) -> None:
        self._providers = tuple(providers)
        self._ledger = ledger
        self._breaker = breaker
        self._approver = approver
        self._paid_timeout_s = paid_timeout_s

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    async def _gate_paid(self, provider: Provider, capped: int) -> None:
        if not provider.paid or self._approver is None:
            return
        price_fn = getattr(provider, "price", None)
        if price_fn is None:
            return
        spec = self._ledger.spec
        projected = Decimal(price_fn(capped, capped))
        if not paid_call_needs_approval(
            projected_usd=projected, usd_cap=spec.usd_cap, allow_paid=spec.allow_paid
        ):
            return
        decision = await request_paid_call(
            self._approver,
            projected_usd=projected,
            usd_cap=spec.usd_cap,
            usd_spent=self._ledger.usd_used,
            timeout_s=self._paid_timeout_s,
            subject_id=provider.name,
        )
        if not decision.approved:
            raise PaidDisabled(f"PAID_CALL denied: {decision.reason}")

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
                await self._gate_paid(provider, capped)
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
