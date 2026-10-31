"""tool_usage: MCP tool calls recorded server-side (append-only).

Revision ID: 0004_tool_usage
Revises: 0003_broker_runs
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_tool_usage"
down_revision: str | None = "0003_broker_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_usage",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("task_id", sa.Text),
        sa.Column("tool", sa.Text, nullable=False),
        sa.Column("wall_ms", sa.Float, nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tool_usage_task_id", "tool_usage", ["task_id"])
    # bourse_reject_mutation() is created by 0001_registry.
    op.execute(
        "CREATE TRIGGER tool_usage_append_only BEFORE UPDATE OR DELETE ON tool_usage "
        "FOR EACH ROW EXECUTE FUNCTION bourse_reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS tool_usage_append_only ON tool_usage")
    op.drop_index("ix_tool_usage_task_id", table_name="tool_usage")
    op.drop_table("tool_usage")
