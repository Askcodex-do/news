"""phase 3 event embedding index

Revision ID: 5b8c1f2a9d47
Revises: 4634825a4b2d
Create Date: 2026-10-07 21:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "5b8c1f2a9d47"
down_revision: str | None = "4634825a4b2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The clustering worker picks the nearest existing events to a new report by
    # vector distance; without an index that scan grows with the event count.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_embedding_hnsw "
        "ON events USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_events_embedding_hnsw")
