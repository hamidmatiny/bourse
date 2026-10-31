"""runs and delegations: broker run records (delegations append-only).

Revision ID: 0003_broker_runs
Revises: 0002_llm_usage
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_broker_runs"
down_revision: str | None = "0002_llm_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("request_text", sa.Text, nullable=False),
        sa.Column("plan", postgresql.JSONB, nullable=False),
        sa.Column("planner_used", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("wall_ms", sa.Float),
        sa.CheckConstraint("planner_used IN ('llm', 'template')", name="ck_runs_planner_used"),
    )
    op.create_table(
        "delegations",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("subtask_id", sa.Text, nullable=False),
        sa.Column("skill", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("revision_digest", sa.Text, nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("error_kind", sa.Text),
        sa.Column("wall_ms", sa.Float, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt >= 1", name="ck_delegations_attempt"),
    )
    op.create_index("ix_delegations_run_id", "delegations", ["run_id"])
    # bourse_reject_mutation() is created by 0001_registry.
    op.execute(
        "CREATE TRIGGER delegations_append_only BEFORE UPDATE OR DELETE ON delegations "
        "FOR EACH ROW EXECUTE FUNCTION bourse_reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS delegations_append_only ON delegations")
    op.drop_index("ix_delegations_run_id", table_name="delegations")
    op.drop_table("delegations")
    op.drop_table("runs")
