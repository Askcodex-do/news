"""Enable required PostgreSQL extensions.

Revision ID: 0001_extensions
Revises:
Create Date: 2026-10-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_extensions"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Extensions are created by the database provisioning step
    # (infrastructure/docker/postgres-init/01-app-role.sh) because CREATE
    # EXTENSION requires superuser, which the application role must not have.
    # This migration only verifies that they are present.
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN "
        "RAISE EXCEPTION 'extension \"vector\" is not installed; run database provisioning'; "
        "END IF; END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN "
        "RAISE EXCEPTION 'extension \"pg_trgm\" is not installed; run database provisioning'; "
        "END IF; END $$;"
    )


def downgrade() -> None:
    # Extensions are owned by the provisioning role; the app role must not
    # drop them.
    pass
