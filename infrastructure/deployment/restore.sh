#!/usr/bin/env sh
# Database restore from a pg_dump custom-format file (spec section 36: DR).
#
# Usage:
#   infrastructure/deployment/restore.sh backups/news-20260101T000000Z.dump
#
# Restores into an EMPTY database. To be safe by default it refuses to run
# against a database that already has tables unless FORCE=1 is set, because a
# restore is destructive and is normally done during an incident.
#
# Configuration from the environment (see .env.example):
#   POSTGRES_*   - connection details
#   PGPASSWORD   - required
#   FORCE=1      - allow restoring over an existing schema
set -eu

DUMP="${1:?usage: restore.sh <dump-file>}"
[ -f "$DUMP" ] || { echo "[restore] no such dump: $DUMP" >&2; exit 1; }

PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"
PGUSER="${POSTGRES_USER:-news}"
PGDATABASE="${POSTGRES_DB:-news}"
FORCE="${FORCE:-0}"

: "${PGPASSWORD:?PGPASSWORD must be set}"
export PGPASSWORD

echo "[restore] verifying archive ${DUMP}"
pg_restore --list "$DUMP" >/dev/null

if [ "$FORCE" != "1" ]; then
  EXISTING="$(psql --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" \
    --dbname="$PGDATABASE" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null || echo 0)"
  if [ "$EXISTING" != "0" ]; then
    echo "[restore] target database already has ${EXISTING} tables; set FORCE=1 to overwrite" >&2
    exit 1
  fi
fi

echo "[restore] restoring into ${PGDATABASE}@${PGHOST}:${PGPORT}"
# --clean --if-exists makes the restore idempotent when FORCE=1 is set.
pg_restore \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --dbname="$PGDATABASE" \
  --clean --if-exists \
  --no-owner \
  --no-privileges \
  "$DUMP"

echo "[restore] done"
