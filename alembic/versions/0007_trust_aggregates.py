"""violation_aggregates (updatable counter; not append-only).

Revision ID: 0007_trust_aggregates
Revises: 0006_egress_trust
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_trust_aggregates"
down_revision: str | None = "0006_egress_trust"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "violation_aggregates",
        sa.Column("vendor_domain", sa.Text, primary_key=True),
        sa.Column("hour", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("kind", sa.Text, primary_key=True),
        sa.Column("count", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("violation_aggregates")
