"""phase 7 continuous operation

Add ``scheduler_state``, a single-row lease so exactly one worker schedules
poll jobs while the rest execute (spec section 25). Advisory: a dead holder's
lease ages out and another worker takes over.

Revision ID: 8d2b6f0a1c34
Revises: 7c1a4d9e2f30
Create Date: 2026-10-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d2b6f0a1c34"
down_revision: str | None = "7c1a4d9e2f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_state",
        sa.Column("name", sa.String(length=64), primary_key=True),
        sa.Column("locked_by", sa.String(length=120), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("scheduler_state")
