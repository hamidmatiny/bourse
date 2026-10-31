"""ledger_entries and selection_decisions (both append-only).

Revision ID: 0005_ledger
Revises: 0004_tool_usage
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_ledger"
down_revision: str | None = "0004_tool_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("ledger_entries", "selection_decisions")


def upgrade() -> None:
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("subtask_id", sa.Text, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("skill", sa.Text, nullable=False),
        sa.Column("revision_digest", sa.Text, nullable=False),
        sa.Column("agent_major", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("error_kind", sa.Text),
        sa.Column("tokens_in", sa.Integer, nullable=False),
        sa.Column("tokens_out", sa.Integer, nullable=False),
        sa.Column("tool_calls", sa.Integer, nullable=False),
        sa.Column("shadow_usd", sa.Float, nullable=False),
        sa.Column("price_table_version", sa.Text, nullable=False),
        sa.Column("wall_ms", sa.Float, nullable=False),
        sa.Column("spot_check_passed", sa.Boolean),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt >= 1", name="ck_ledger_attempt"),
    )
    op.create_index("ix_ledger_run_id", "ledger_entries", ["run_id"])
    op.create_index("ix_ledger_agent_skill", "ledger_entries", ["agent_id", "skill"])
    op.create_table(
        "selection_decisions",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("subtask_id", sa.Text, nullable=False),
        sa.Column("skill", sa.Text, nullable=False),
        sa.Column("candidates", postgresql.JSONB, nullable=False),
        sa.Column("chosen", sa.Text),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_selection_skill", "selection_decisions", ["skill"])
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bourse_reject_mutation()"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_index("ix_selection_skill", table_name="selection_decisions")
    op.drop_table("selection_decisions")
    op.drop_index("ix_ledger_agent_skill", table_name="ledger_entries")
    op.drop_index("ix_ledger_run_id", table_name="ledger_entries")
    op.drop_table("ledger_entries")
