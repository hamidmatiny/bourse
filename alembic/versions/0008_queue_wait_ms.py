"""llm_usage.queue_wait_ms for meter fair-share wait (ADR-0042 r2).

Revision ID: 0008_queue_wait_ms
Revises: 0007_trust_aggregates
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_queue_wait_ms"
down_revision: str | None = "0007_trust_aggregates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_usage",
        sa.Column("queue_wait_ms", sa.Float, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("llm_usage", "queue_wait_ms")
