"""add source raw_text_permitted

Revision ID: 239a6ea1c6a4
Revises: 09febdbf1143
Create Date: 2026-10-07 20:17:23.128759
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '239a6ea1c6a4'
down_revision: str | None = '09febdbf1143'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows predate the flag; default them to False (we do not store
    # publisher full text unless a source explicitly opts in).
    op.add_column(
        "sources",
        sa.Column(
            "raw_text_permitted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("sources", "raw_text_permitted")
