"""llm_usage.prompt_tokens_est: the meter's prompt estimate for the hard task cap (ADR-0041).

Revision ID: 0012_prompt_tokens_est
Revises: 0011_violations_rel_epoch
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_prompt_tokens_est"
down_revision: str | None = "0011_violations_rel_epoch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_usage", sa.Column("prompt_tokens_est", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "prompt_tokens_est")
