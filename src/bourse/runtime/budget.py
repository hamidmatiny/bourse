"""Thread-safe token/USD ledger for one run (ADR-0005, ADR-0007)."""

import threading
from datetime import UTC, datetime
from decimal import Decimal

from bourse.contracts.metering import BudgetSpec, LedgerEntry, Usage


class BudgetExceeded(RuntimeError):
    pass


class BudgetLedger:
    def __init__(self, spec: BudgetSpec, run_id: str = "run") -> None:
        self._spec = spec
        self._run_id = run_id
        self._lock = threading.Lock()
        self._tokens = 0
        self._usd = Decimal(0)
        self._entries: list[LedgerEntry] = []

    @property
    def spec(self) -> BudgetSpec:
        return self._spec

    @property
    def tokens_used(self) -> int:
        with self._lock:
            return self._tokens

    @property
    def usd_used(self) -> Decimal:
        with self._lock:
            return self._usd

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def cap_call(self, requested_max_tokens: int) -> int:
        """Return the max_tokens to send to the provider; refuse if the run is spent."""
        with self._lock:
            if self._tokens >= self._spec.max_tokens_run:
                raise BudgetExceeded(
                    f"run token budget spent: {self._tokens}/{self._spec.max_tokens_run}"
                )
        return max(1, min(requested_max_tokens, self._spec.max_tokens_per_call))

    def charge(self, usage: Usage, usd: Decimal = Decimal(0)) -> LedgerEntry:
        """Record usage. The entry is kept even when it pushes the run over budget."""
        with self._lock:
            self._tokens += usage.tokens_in + usage.tokens_out
            self._usd += usd
            entry = LedgerEntry(
                run_id=self._run_id,
                seq=len(self._entries),
                usage=usage,
                usd=usd,
                tokens_run_total=self._tokens,
                at=datetime.now(UTC),
            )
            self._entries.append(entry)
            tokens, spent = self._tokens, self._usd
        if tokens > self._spec.max_tokens_run:
            raise BudgetExceeded(f"run token budget exceeded: {tokens}/{self._spec.max_tokens_run}")
        if spent > self._spec.usd_cap:
            raise BudgetExceeded(f"USD cap exceeded: {spent}/{self._spec.usd_cap}")
        return entry
