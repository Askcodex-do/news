#!/bin/sh
# Creates the least-privilege application role on first database boot.
# Runs inside the postgres container as part of docker-entrypoint-initdb.d.
# The role is used by the API and workers; the superuser is used only by
# database administration.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'news_app') THEN
            CREATE ROLE news_app LOGIN PASSWORD '${APP_DB_PASSWORD:-change-me}';
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO news_app;
    GRANT USAGE, CREATE ON SCHEMA public TO news_app;

    -- Extensions require superuser, so they are created here at provisioning
    -- time. The application role is then granted their functions/operators.
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOSQL
