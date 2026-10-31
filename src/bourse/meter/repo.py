"""Usage repository protocol + in-memory implementation (ADR-0014 pattern)."""

from typing import Protocol

from bourse.contracts.metering import LlmUsageRecord


class MeterRepository(Protocol):
    async def add_usage(self, record: LlmUsageRecord) -> None: ...

    async def list_usage(
        self, agent_id: str | None = None, task_id: str | None = None, run_id: str | None = None
    ) -> list[LlmUsageRecord]:
        """`run_id` matches task_id == run_id or task_id starting with run_id + ":" (broker
        task ids are run_id:subtask_id:attempt)."""
        ...


def in_run(task_id: str | None, run_id: str) -> bool:
    return task_id is not None and (task_id == run_id or task_id.startswith(run_id + ":"))


class InMemoryMeterRepository:
    def __init__(self) -> None:
        self._rows: list[LlmUsageRecord] = []

    async def add_usage(self, record: LlmUsageRecord) -> None:
        self._rows.append(record)

    async def list_usage(
        self, agent_id: str | None = None, task_id: str | None = None, run_id: str | None = None
    ) -> list[LlmUsageRecord]:
        return [
            r
            for r in self._rows
            if (agent_id is None or r.agent_id == agent_id)
            and (task_id is None or r.task_id == task_id)
            and (run_id is None or in_run(r.task_id, run_id))
        ]
