"""llm_usage: metering proxy usage rows (append-only).

Revision ID: 0002_llm_usage
Revises: 0001_registry
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_llm_usage"
down_revision: str | None = "0001_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("task_id", sa.Text),
        sa.Column("provider", sa.Text),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("tokens_in", sa.Integer, nullable=False),
        sa.Column("tokens_out", sa.Integer, nullable=False),
        sa.Column("wall_ms", sa.Float, nullable=False),
        sa.Column("status", sa.Integer, nullable=False),
        sa.Column("error_kind", sa.Text),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("tokens_in >= 0 AND tokens_out >= 0", name="ck_llm_usage_tokens"),
    )
    op.create_index("ix_llm_usage_agent_id", "llm_usage", ["agent_id"])
    op.create_index("ix_llm_usage_task_id", "llm_usage", ["task_id"])
    # bourse_reject_mutation() is created by 0001_registry.
    op.execute(
        "CREATE TRIGGER llm_usage_append_only BEFORE UPDATE OR DELETE ON llm_usage "
        "FOR EACH ROW EXECUTE FUNCTION bourse_reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS llm_usage_append_only ON llm_usage")
    op.drop_index("ix_llm_usage_task_id", table_name="llm_usage")
    op.drop_index("ix_llm_usage_agent_id", table_name="llm_usage")
    op.drop_table("llm_usage")
