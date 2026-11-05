"""violations.rel_epoch: SOFT strikes count per (agent, rel_epoch) (ADR-0039, ADR-0048).

Existing rows predate reinstatement epochs and get rel_epoch 0.

Revision ID: 0011_violations_rel_epoch
Revises: 0010_runs_abort_reason
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_violations_rel_epoch"
down_revision: str | None = "0010_runs_abort_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "violations",
        sa.Column("rel_epoch", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("violations", "rel_epoch")
