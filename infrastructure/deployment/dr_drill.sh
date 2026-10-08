#!/usr/bin/env sh
# Disaster-recovery drill (spec section 36: disaster recovery).
#
# Proves a backup is actually restorable and complete: it takes a fresh dump,
# restores it into a scratch database, and compares row counts for the critical
# tables against the source. It then drops the scratch database. Run it on a
# schedule (or before trusting a backup) so "we have backups" is a verified
# claim, not an assumption.
#
# Usage:
#   PGPASSWORD=... infrastructure/deployment/dr_drill.sh
#
# Exit status is non-zero if the restored copy differs from the source.
set -eu

PGHOST="${POSTGRES_HOST:-localhost}"
PGPORT="${POSTGRES_PORT:-5432}"
PGUSER="${POSTGRES_USER:-news}"
PGDATABASE="${POSTGRES_DB:-news}"
SCRATCH="${PGDATABASE}_drill"
BACKUP_DIR="${BACKUP_DIR:-backups}"

: "${PGPASSWORD:?PGPASSWORD must be set}"
export PGPASSWORD

# The drill creates and drops a scratch database, so it needs a role with
# CREATEDB. The application's least-privilege role intentionally does not have
# it: run the drill as an admin/backup role (POSTGRES_USER / PGPASSWORD).
CAN_CREATE="$(psql --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" \
  --dbname="$PGDATABASE" -tAc \
  "SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user" 2>/dev/null || echo f)"
if [ "$CAN_CREATE" != "t" ]; then
  echo "[drill] role '${PGUSER}' cannot CREATE DATABASE; run the drill as an" >&2
  echo "[drill] admin/backup role (set POSTGRES_USER / PGPASSWORD)" >&2
  exit 1
fi

# Tables whose contents must survive a restore intact.
TABLES="sources source_reports events event_facts event_reports articles article_versions processing_jobs country_sources"

# Admin queries (CREATE/DROP DATABASE) connect to the source database so psql
# does not fall back to the username as a database name.
psql_run() {
  psql --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" \
    --dbname="$PGDATABASE" -tAc "$1"
}

count_rows() {
  # $1 = database, $2 = table
  psql --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" \
    --dbname="$1" -tAc "SELECT count(*) FROM $2" 2>/dev/null || echo "ERR"
}

echo "[drill] taking a fresh backup"
DUMP="$(BACKUP_DIR="$BACKUP_DIR" "$(dirname "$0")/backup.sh" \
  | sed -n 's/^\[backup\] wrote \([^ ]*\).*/\1/p' | tail -n1)"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
  echo "[drill] backup did not produce a file" >&2
  exit 1
fi

echo "[drill] recreating scratch database ${SCRATCH}"
psql_run "DROP DATABASE IF EXISTS ${SCRATCH}" >/dev/null
psql_run "CREATE DATABASE ${SCRATCH}" >/dev/null

echo "[drill] restoring ${DUMP} into ${SCRATCH}"
pg_restore --host="$PGHOST" --port="$PGPORT" --username="$PGUSER" \
  --dbname="$SCRATCH" --no-owner --no-privileges "$DUMP"

FAILED=0
for TABLE in $TABLES; do
  SRC="$(count_rows "$PGDATABASE" "$TABLE")"
  DST="$(count_rows "$SCRATCH" "$TABLE")"
  if [ "$SRC" = "$DST" ]; then
    echo "[drill] OK   ${TABLE}: ${SRC}"
  else
    echo "[drill] FAIL ${TABLE}: source=${SRC} restored=${DST}" >&2
    FAILED=1
  fi
done

echo "[drill] dropping scratch database ${SCRATCH}"
psql_run "DROP DATABASE IF EXISTS ${SCRATCH}" >/dev/null

if [ "$FAILED" != "0" ]; then
  echo "[drill] FAILED: restored copy does not match the source" >&2
  exit 1
fi
echo "[drill] PASSED: backup is restorable and complete"
