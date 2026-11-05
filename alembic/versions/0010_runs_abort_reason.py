"""runs.abort_reason for ABORTED runs (RESIDENT_LIMIT / BREAKER / WATCHDOG / WALL_BUDGET).

Revision ID: 0010_runs_abort_reason
Revises: 0009_upstream_timings
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_runs_abort_reason"
down_revision: str | None = "0009_upstream_timings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("abort_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "abort_reason")
