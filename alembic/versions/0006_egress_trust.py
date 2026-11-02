"""egress_events, honeytokens (append-only); ledger_entries.kind.

Revision ID: 0006_egress_trust
Revises: 0005_ledger
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_egress_trust"
down_revision: str | None = "0005_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("egress_events", "honeytokens")


def upgrade() -> None:
    op.create_table(
        "egress_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("src_ip", sa.Text, nullable=False),
        sa.Column("agent_id", sa.Text),
        sa.Column("host", sa.Text),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column("payload_sha256", sa.Text, nullable=False),
        sa.Column("honeytoken_hits", sa.Integer, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_egress_events_agent_id", "egress_events", ["agent_id"])
    op.create_table(
        "honeytokens",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("task_id", sa.Text, nullable=False),
        sa.Column("card", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    # bourse_reject_mutation() is created by 0001_registry.
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bourse_reject_mutation()"
        )
    # ADD COLUMN with a constant default does not fire the row-level append-only trigger.
    op.add_column(
        "ledger_entries", sa.Column("kind", sa.Text, nullable=False, server_default="task")
    )


def downgrade() -> None:
    op.drop_column("ledger_entries", "kind")
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("honeytokens")
    op.drop_index("ix_egress_events_agent_id", table_name="egress_events")
    op.drop_table("egress_events")
