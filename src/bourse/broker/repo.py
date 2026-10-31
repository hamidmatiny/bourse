"""Run repository protocol + in-memory implementation (ADR-0014 pattern, ADR-0027)."""

from datetime import datetime
from typing import Protocol

from bourse.contracts.broker import DelegationRecord, RunRecord, RunStatus


class RunRepository(Protocol):
    async def create_run(self, run: RunRecord) -> None: ...

    async def finish_run(
        self, run_id: str, status: RunStatus, ended_at: datetime, wall_ms: float
    ) -> None: ...

    async def get_run(self, run_id: str) -> RunRecord | None: ...

    async def add_delegation(self, record: DelegationRecord) -> None:
        """Append only; delegation rows are never updated or deleted."""
        ...

    async def list_delegations(self, run_id: str) -> list[DelegationRecord]: ...


class InMemoryRunRepository:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._delegations: list[DelegationRecord] = []

    async def create_run(self, run: RunRecord) -> None:
        if run.run_id in self._runs:
            raise ValueError(f"run {run.run_id} exists")
        self._runs[run.run_id] = run

    async def finish_run(
        self, run_id: str, status: RunStatus, ended_at: datetime, wall_ms: float
    ) -> None:
        run = self._runs[run_id]
        self._runs[run_id] = run.model_copy(
            update={"status": status, "ended_at": ended_at, "wall_ms": wall_ms}
        )

    async def get_run(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    async def add_delegation(self, record: DelegationRecord) -> None:
        self._delegations.append(record)

    async def list_delegations(self, run_id: str) -> list[DelegationRecord]:
        return [d for d in self._delegations if d.run_id == run_id]
