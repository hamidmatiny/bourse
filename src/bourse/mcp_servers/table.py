"""Table tools over CSV text: profile_columns, column_stats, sample_rows (ADR-0016).

Pure: each call parses its own csv_text into a fresh in-memory DuckDB; nothing persists.
Empty cells are NULL. Type rule per column over non-null cells, first match wins:
bool (true/false, any case) > int (-?[0-9]+) > float > date (YYYY-MM-DD, valid) > string;
a column with no non-null cells is string.
"""

import csv
import io
from typing import Annotated, Any

import duckdb
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

from bourse.contracts.skills_io import (
    ColumnProfile,
    ColumnStats,
    Dtype,
    SchemaProfileOut,
    StatsOut,
)
from bourse.mcp_servers.common import check_size, run_limited

INT_RE = "-?[0-9]+"
FLOAT_RE = "-?([0-9]+[.]?[0-9]*|[.][0-9]+)([eE][-+]?[0-9]+)?"
DATE_RE = "[0-9]{4}-[0-9]{2}-[0-9]{2}"
MAX_SAMPLE_ROWS = 20


class SampleRowsOut(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    columns: list[str]
    rows: list[list[str | None]]


class _Table:
    def __init__(self, csv_text: str) -> None:
        check_size(csv_text)
        rows = list(csv.reader(io.StringIO(csv_text)))
        if not rows or not rows[0]:
            raise ValueError("csv_text has no header row")
        header = rows[0]
        if any(not h for h in header) or len(set(header)) != len(header):
            raise ValueError("header names must be non-empty and unique")
        data = rows[1:]
        for i, row in enumerate(data):
            if len(row) != len(header):
                raise ValueError(f"row {i} has {len(row)} cells, header has {len(header)}")
        self.header = header
        self.rows: list[list[str | None]] = [[c if c != "" else None for c in r] for r in data]
        self.con = duckdb.connect(":memory:")
        cols = ", ".join(f"c{i} VARCHAR" for i in range(len(header)))
        self.con.execute(f"CREATE TABLE t ({cols})")
        if self.rows:
            marks = ", ".join("?" for _ in header)
            self.con.executemany(f"INSERT INTO t VALUES ({marks})", self.rows)

    def close(self) -> None:
        self.con.close()

    def profile(self) -> SchemaProfileOut:
        columns: list[ColumnProfile] = []
        for i, name in enumerate(self.header):
            c = f"c{i}"
            row = self.con.execute(
                f"""
                SELECT count(*) - count({c}), count(DISTINCT {c}), count({c}),
                  count(*) FILTER (WHERE lower({c}) IN ('true', 'false')),
                  count(*) FILTER (WHERE regexp_full_match({c}, '{INT_RE}')),
                  count(*) FILTER (WHERE regexp_full_match({c}, '{FLOAT_RE}')),
                  count(*) FILTER (WHERE regexp_full_match({c}, '{DATE_RE}')
                                   AND TRY_CAST({c} AS DATE) IS NOT NULL)
                FROM t
                """
            ).fetchone()
            assert row is not None
            nulls, distinct, non_null, n_bool, n_int, n_float, n_date = (int(v) for v in row)
            dtype: Dtype = "string"
            if non_null:
                if n_bool == non_null:
                    dtype = "bool"
                elif n_int == non_null:
                    dtype = "int"
                elif n_float == non_null:
                    dtype = "float"
                elif n_date == non_null:
                    dtype = "date"
            columns.append(
                ColumnProfile(name=name, dtype=dtype, null_count=nulls, distinct_count=distinct)
            )
        return SchemaProfileOut(columns=columns)

    def stats(self, columns: list[str] | None) -> StatsOut:
        profile = {c.name: c.dtype for c in self.profile().columns}
        numeric = [n for n in self.header if profile[n] in ("int", "float")]
        wanted = columns if columns else numeric
        out: list[ColumnStats] = []
        for name in wanted:
            if name not in profile:
                raise ValueError(f"unknown column {name!r}")
            if profile[name] not in ("int", "float"):
                raise ValueError(f"column {name!r} is {profile[name]}, not numeric")
            c = f"c{self.header.index(name)}"
            row = self.con.execute(
                f"""
                SELECT avg(x), stddev_samp(x), min(x), max(x), quantile_cont(x, 0.5)
                FROM (SELECT CAST({c} AS DOUBLE) AS x FROM t WHERE {c} IS NOT NULL)
                """
            ).fetchone()
            assert row is not None
            mean, std, lo, hi, p50 = row
            out.append(
                ColumnStats(
                    column=name,
                    mean=float(mean),
                    std=None if std is None else float(std),
                    min=float(lo),
                    max=float(hi),
                    p50=float(p50),
                )
            )
        return StatsOut(stats=out)

    def sample(self, n: int) -> SampleRowsOut:
        return SampleRowsOut(columns=list(self.header), rows=[list(r) for r in self.rows[:n]])


def profile_columns_impl(csv_text: str) -> SchemaProfileOut:
    table = _Table(csv_text)
    try:
        return table.profile()
    finally:
        table.close()


def column_stats_impl(csv_text: str, columns: list[str] | None = None) -> StatsOut:
    table = _Table(csv_text)
    try:
        return table.stats(columns)
    finally:
        table.close()


def sample_rows_impl(csv_text: str, n: int = 5) -> SampleRowsOut:
    if not 1 <= n <= MAX_SAMPLE_ROWS:
        raise ValueError(f"n must be in 1..{MAX_SAMPLE_ROWS}")
    table = _Table(csv_text)
    try:
        return table.sample(n)
    finally:
        table.close()


def build_server() -> MCPServer[Any]:
    server = MCPServer("bourse-table", version="0.1.0")

    @server.tool()
    async def profile_columns(csv_text: str) -> SchemaProfileOut:
        """Infer dtype, null count and distinct count for every CSV column."""
        return await run_limited(lambda: profile_columns_impl(csv_text))

    @server.tool()
    async def column_stats(csv_text: str, columns: list[str] | None = None) -> StatsOut:
        """Mean, sample std, min, max and median for numeric columns (all if columns empty)."""
        return await run_limited(lambda: column_stats_impl(csv_text, columns))

    @server.tool()
    async def sample_rows(
        csv_text: str, n: Annotated[int, Field(ge=1, le=MAX_SAMPLE_ROWS)] = 5
    ) -> SampleRowsOut:
        """First n data rows (n <= 20)."""
        return await run_limited(lambda: sample_rows_impl(csv_text, n))

    return server
