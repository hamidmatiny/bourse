"""Registry tables: vendors, agents, card_revisions, violations, state_transitions.

Revision ID: 0001_registry
Revises:
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_registry"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
APPEND_ONLY_TABLES = ("card_revisions", "state_transitions")


def upgrade() -> None:
    op.create_table(
        "vendors",
        sa.Column("domain", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("jwks_url", sa.Text, nullable=False),
        sa.Column("jwks", postgresql.JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "agents",
        sa.Column("agent_id", sa.Text, primary_key=True),
        sa.Column("vendor_domain", sa.Text, sa.ForeignKey("vendors.domain"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("card_url", sa.Text),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("current_version", sa.Text, nullable=False),
        sa.Column("current_digest", sa.Text, nullable=False),
        sa.Column("skills", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("search_text", sa.Text, nullable=False),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR,
            sa.Computed("to_tsvector('simple', search_text)", persisted=True),
        ),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "state IN ('PENDING','ACTIVE','QUARANTINED','REVOKED')", name="ck_agents_state"
        ),
    )
    op.create_index("ix_agents_search_tsv", "agents", ["search_tsv"], postgresql_using="gin")
    op.create_index("ix_agents_skills", "agents", ["skills"], postgresql_using="gin")
    op.create_table(
        "card_revisions",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("agent_id", sa.Text, sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("digest_sha256", sa.Text, nullable=False),
        sa.Column("verify_result", sa.Text, nullable=False),
        sa.Column("card", postgresql.JSONB, nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.UniqueConstraint("agent_id", "version", name="uq_card_revisions_agent_version"),
    )
    op.create_table(
        "violations",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("agent_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("severity", sa.Text, nullable=False),
        sa.Column("evidence", sa.Text, nullable=False),
        sa.Column("at", TS, nullable=False),
    )
    op.create_index("ix_violations_agent_id", "violations", ["agent_id"])
    op.create_table(
        "state_transitions",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("agent_id", sa.Text, sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("from_state", sa.Text, nullable=False),
        sa.Column("to_state", sa.Text, nullable=False),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("approval", postgresql.JSONB),
        sa.Column("at", TS, nullable=False),
    )
    op.create_index("ix_state_transitions_agent_id", "state_transitions", ["agent_id"])
    op.execute(
        """
        CREATE FUNCTION bourse_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
        END
        $$
        """
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bourse_reject_mutation()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("state_transitions")
    op.drop_table("violations")
    op.drop_table("card_revisions")
    op.drop_index("ix_agents_skills", table_name="agents")
    op.drop_index("ix_agents_search_tsv", table_name="agents")
    op.drop_table("agents")
    op.drop_table("vendors")
    op.execute("DROP FUNCTION IF EXISTS bourse_reject_mutation()")
