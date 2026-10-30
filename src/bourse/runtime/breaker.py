"""Global circuit breaker for billing/auth/quota errors (ADR-0005).

TRIP: HTTP 401/402/403; 429 whose body mentions quota/credit/billing; any body containing
insufficient_quota, credit balance, or billing. IGNORE: plain 5xx, plain 429 rate limits,
timeouts, connection errors. Once tripped, guard() raises BreakerOpen for the whole process.
"""

import threading
from enum import StrEnum

import httpx


class Verdict(StrEnum):
    TRIP = "TRIP"
    IGNORE = "IGNORE"


class BreakerOpen(RuntimeError):
    pass


class ProviderHTTPError(RuntimeError):
    """Non-2xx response from an LLM provider, with the body already read."""

    def __init__(self, provider: str, status_code: int, body: str) -> None:
        super().__init__(f"{provider} returned HTTP {status_code}: {body[:200]}")
        self.provider = provider
        self.status_code = status_code
        self.body = body


_TRIP_STATUSES = frozenset({401, 402, 403})
_TRIP_BODY_MARKERS = ("insufficient_quota", "credit balance", "billing")
_TRIP_429_MARKERS = ("quota", "credit", "billing")


def _status_and_body(item: BaseException | httpx.Response) -> tuple[int | None, str]:
    if isinstance(item, httpx.Response):
        try:
            return item.status_code, item.text
        except httpx.ResponseNotRead:
            return item.status_code, ""
    if isinstance(item, ProviderHTTPError):
        return item.status_code, item.body
    if isinstance(item, httpx.HTTPStatusError):
        return _status_and_body(item.response)
    return None, ""


class CircuitBreaker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: str | None = None

    @staticmethod
    def classify(item: BaseException | httpx.Response) -> Verdict:
        status, body = _status_and_body(item)
        if status is None:
            return Verdict.IGNORE
        lowered = body.lower()
        if status in _TRIP_STATUSES:
            return Verdict.TRIP
        if any(m in lowered for m in _TRIP_BODY_MARKERS):
            return Verdict.TRIP
        if status == 429 and any(m in lowered for m in _TRIP_429_MARKERS):
            return Verdict.TRIP
        return Verdict.IGNORE

    def record(self, item: BaseException | httpx.Response) -> Verdict:
        verdict = self.classify(item)
        if verdict is Verdict.TRIP:
            status, body = _status_and_body(item)
            self.trip(f"HTTP {status}: {body[:200]}")
        return verdict

    def trip(self, reason: str) -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._reason is not None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def guard(self) -> None:
        with self._lock:
            reason = self._reason
        if reason is not None:
            raise BreakerOpen(f"circuit breaker open: {reason}")

    def reset(self) -> None:
        """Operator/test use only. A run never resets its own breaker."""
        with self._lock:
            self._reason = None


GLOBAL_BREAKER = CircuitBreaker()
