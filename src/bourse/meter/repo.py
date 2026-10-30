"""Usage repository protocol + in-memory implementation (ADR-0014 pattern)."""

from typing import Protocol

from bourse.contracts.metering import LlmUsageRecord


class MeterRepository(Protocol):
    async def add_usage(self, record: LlmUsageRecord) -> None: ...

    async def list_usage(self, agent_id: str | None = None) -> list[LlmUsageRecord]: ...


class InMemoryMeterRepository:
    def __init__(self) -> None:
        self._rows: list[LlmUsageRecord] = []

    async def add_usage(self, record: LlmUsageRecord) -> None:
        self._rows.append(record)

    async def list_usage(self, agent_id: str | None = None) -> list[LlmUsageRecord]:
        return [r for r in self._rows if agent_id is None or r.agent_id == agent_id]
