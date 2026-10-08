#!/usr/bin/env sh
# Database backup with retention (spec section 36: backups, disaster recovery).
#
# Runs pg_dump in the custom format (compressed, restorable selectively with
# pg_restore). Designed to run from cron on the host or as a scheduled task:
#
#   0 */6 * * *  /repo/infrastructure/deployment/backup.sh >> /var/log/news-backup.log 2>&1
#
# Configuration comes from the environment (see .env.example):
#   DATABASE_URL / POSTGRES_*  - connection details
#   BACKUP_DIR                 - where dumps are written (default: ./backups)
#   BACKUP_RETENTION_DAYS      - dumps older than this are deleted (default: 14)
#
# The dump is written to a temp file and renamed on success, so a crashed run
# never leaves a truncated file that looks like a valid backup.
set -eu

BACKUP_DIR="${BACKUP_DIR:-backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"
PGUSER="${POSTGRES_USER:-news}"
PGDATABASE="${POSTGRES_DB:-news}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/news-${STAMP}.dump"
TMP="${TARGET}.partial"

echo "[backup] dumping ${PGDATABASE}@${PGHOST}:${PGPORT} -> ${TARGET}"

# PGPASSWORD must be supplied by the environment (never on the command line,
# which would leak it to `ps`).
: "${PGPASSWORD:?PGPASSWORD must be set (use the postgres superuser for a full dump)}"
export PGPASSWORD

pg_dump \
  --host="$PGHOST" \
  --port="$PGPORT" \
  --username="$PGUSER" \
  --dbname="$PGDATABASE" \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="$TMP"

mv "$TMP" "$TARGET"
echo "[backup] wrote ${TARGET} ($(wc -c < "$TARGET") bytes)"

# Retention: delete dumps older than the retention window.
find "$BACKUP_DIR" -name 'news-*.dump' -type f -mtime "+${BACKUP_RETENTION_DAYS}" -print -delete

echo "[backup] done"
