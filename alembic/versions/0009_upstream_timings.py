"""llm_usage upstream Ollama timings + reload flag (ADR-0044).

Revision ID: 0009_upstream_timings
Revises: 0008_queue_wait_ms
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_upstream_timings"
down_revision: str | None = "0008_queue_wait_ms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("total_duration_ms", sa.Float(), nullable=True))
    op.add_column("llm_usage", sa.Column("load_duration_ms", sa.Float(), nullable=True))
    op.add_column("llm_usage", sa.Column("prompt_eval_duration_ms", sa.Float(), nullable=True))
    op.add_column("llm_usage", sa.Column("eval_duration_ms", sa.Float(), nullable=True))
    op.add_column(
        "llm_usage",
        sa.Column("reload", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("llm_usage", "reload")
    op.drop_column("llm_usage", "eval_duration_ms")
    op.drop_column("llm_usage", "prompt_eval_duration_ms")
    op.drop_column("llm_usage", "load_duration_ms")
    op.drop_column("llm_usage", "total_duration_ms")
