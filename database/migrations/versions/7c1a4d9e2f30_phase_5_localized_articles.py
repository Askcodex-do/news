"""phase 5 localized articles

Allow one global article plus one localized article per country for an event
(spec section 18), replacing the old one-article-per-event unique constraint.
A developing story still keeps one article per locale and appends versions
(spec section 22).

Revision ID: 7c1a4d9e2f30
Revises: 5b8c1f2a9d47
Create Date: 2026-10-08 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "7c1a4d9e2f30"
down_revision: str | None = "5b8c1f2a9d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE articles DROP CONSTRAINT IF EXISTS articles_event_id_key")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_event_global "
        "ON articles (event_id) WHERE is_global"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_event_locale "
        "ON articles (event_id, locale_country) WHERE NOT is_global"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_articles_event_locale")
    op.execute("DROP INDEX IF EXISTS uq_articles_event_global")
    op.execute("ALTER TABLE articles ADD CONSTRAINT articles_event_id_key UNIQUE (event_id)")
