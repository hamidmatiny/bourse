"""Ledger and selection-decision repositories (ADR-0014 pattern; both tables append-only)."""

from typing import Protocol

from bourse.contracts.broker import LedgerRow, SelectionRecord
from bourse.contracts.skills import SkillId


class LedgerRepository(Protocol):
    async def add_row(self, row: LedgerRow) -> None: ...

    async def list_rows(
        self, run_id: str | None = None, skill: SkillId | None = None
    ) -> list[LedgerRow]:
        """In insertion order."""
        ...

    async def add_selection(self, record: SelectionRecord) -> None: ...

    async def list_selections(
        self, run_id: str | None = None, skill: SkillId | None = None
    ) -> list[SelectionRecord]: ...


class InMemoryLedgerRepository:
    def __init__(self) -> None:
        self.rows: list[LedgerRow] = []
        self.selections: list[SelectionRecord] = []

    async def add_row(self, row: LedgerRow) -> None:
        self.rows.append(row)

    async def list_rows(
        self, run_id: str | None = None, skill: SkillId | None = None
    ) -> list[LedgerRow]:
        return [
            r
            for r in self.rows
            if (run_id is None or r.run_id == run_id) and (skill is None or r.skill is skill)
        ]

    async def add_selection(self, record: SelectionRecord) -> None:
        self.selections.append(record)

    async def list_selections(
        self, run_id: str | None = None, skill: SkillId | None = None
    ) -> list[SelectionRecord]:
        return [
            s
            for s in self.selections
            if (run_id is None or s.run_id == run_id) and (skill is None or s.skill is skill)
        ]
