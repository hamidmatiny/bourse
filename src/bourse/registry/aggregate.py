"""Inline-card violation aggregation (ADR-0043).

Unauthenticated pushed cards can flood `violations`. The first N per vendor per UTC hour are
stored individually; further rows increment one counter in `violation_aggregates`.
"""

from collections.abc import Callable
from datetime import UTC, datetime

from bourse.contracts.trust import Violation
from bourse.registry.repo import Repository


def hour_floor(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class ViolationAggregator:
    def __init__(
        self,
        repo: Repository,
        *,
        max_individual: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.max_individual = max_individual
        self.clock = clock
        # In-memory count of individual rows this process stored this hour (tests + single proc).
        self._stored: dict[tuple[str, datetime], int] = {}

    async def add(self, violation: Violation, vendor_domain: str) -> str:
        """Returns "row" or "aggregate"."""
        hour = hour_floor(violation.at)
        key = (vendor_domain, hour)
        stored = self._stored.get(key, 0)
        if stored < self.max_individual:
            await self.repo.add_violation(violation)
            self._stored[key] = stored + 1
            return "row"
        await self.repo.bump_aggregate(vendor_domain, hour, kind=violation.kind)
        return "aggregate"
