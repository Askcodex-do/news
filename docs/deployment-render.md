# Deploying to Render

The repository ships a Render Blueprint (`render.yaml` at the repo root) that
provisions the whole stack. This page covers what the blueprint creates, the
one-time steps only a human with a Render account can perform, and the
verification steps to run afterwards.

## What the blueprint creates

| Resource | Type | Purpose |
| --- | --- | --- |
| `news-ai-db` | Postgres 16 (`basic-256mb`) | Primary database; `pgvector` + `pg_trgm` |
| `news-ai-api` | Web service (Docker) | FastAPI API; runs migrations + seed before each deploy |
| `news-ai-worker` | Background worker (Docker) | The continuous 24/7 ingestion loop |
| `news-ai-frontend` | Web service (Docker) | Next.js UI |

Redis is intentionally omitted. The job queue is PostgreSQL-backed
(`processing_jobs`) and the rate limiter / AI cost counter fall back to an
in-process store, so the stack runs without a Key Value instance. Add one later
only if you need shared rate-limit counters across replicas (set
`RATE_LIMIT_REDIS_ENABLED=true` and `REDIS_URL`).

## Prerequisites

1. A Render account and a GitHub connection that can see this repository.
2. Push this repository (including `render.yaml`) to the default branch.

## Apply the blueprint (requires a Render account)

These are the steps this repository cannot perform on its own — they need an
authenticated Render session.

1. In the Render dashboard: **New + → Blueprint**.
2. Select the `news-ai` repository. Render reads `render.yaml` and shows the
   four resources above.
3. When prompted, set the two `sync: false` secrets:
   - `ADMIN_API_TOKEN` — a strong random string (e.g. `openssl rand -hex 32`).
     It protects the `/admin` endpoints.
   - `AI_API_KEY` — leave blank while `AI_PROVIDER=offline`. Set it only after
     switching `AI_PROVIDER` to `openai` for genuine editorial synthesis.
4. **Apply**. Render builds the images, provisions the database, runs the API's
   pre-deploy command (`alembic upgrade head && python -m app.cli seed`), then
   starts the API, worker and frontend.

The blueprint pins `region: oregon` for every resource so the frontend reaches
the API over Render's private network. Keep them in the same region.

## Configuration the blueprint sets for you

| Variable | Value | Why |
| --- | --- | --- |
| `DATABASE_URL` | internal Postgres connection string | The app normalizes `postgresql://` to `postgresql+asyncpg://` automatically |
| `APP_ENV` | `production` | Enables the startup config guard |
| `TRUST_PROXY_HEADERS` | `true` | Render sets `X-Forwarded-For`; needed for country resolution |
| `NEXT_PUBLIC_API_BASE_URL` | the frontend's `onrender.com` URL | The single origin the API's CORS middleware allows |
| `AI_PROVIDER` | `offline` | Deterministic, no-key writer so a fresh deploy runs the full pipeline |

`NEXT_PUBLIC_API_BASE_URL` is read at build time by Next.js and is also the
origin the backend allows in CORS, so the two services are wired without
hard-coding any domain.

## Verification after deploy

```bash
API=https://news-ai-api.onrender.com       # from the dashboard
UI=https://news-ai-frontend.onrender.com

# API is up and the database has the configured sources.
curl -s "$API/health"
# {"status":"ok", ..., "source_count":47}

# The 30 international sources and the country mappings are seeded.
curl -s "$API/sources" | jq 'length'
curl -s "$API/country-sources?country=IN" | jq

# A feed renders (may be empty until the worker has published events).
curl -s "$API/feed?country=IN" | jq '{global: (.global_articles|length), local: (.local_articles|length)}'

# The UI renders and reads the API server-side.
curl -s -o /dev/null -w '%{http_code}\n' "$UI/"

# Admin endpoints reject an unauthenticated request and accept the token.
curl -s -o /dev/null -w '%{http_code}\n' "$API/admin/metrics"
curl -s -H "X-Admin-Token: $ADMIN_API_TOKEN" "$API/admin/metrics" | jq
```

The worker begins polling as soon as it boots. `source_count` should be 47 (30
international + one local source per supported country). Events are only
published once they clear the confidence and importance floors
(`MIN_CONFIDENCE_TO_PUBLISH`, `MIN_IMPORTANCE_TO_PUBLISH`), so an empty feed on a
brand-new deploy is expected until enough independent reports have clustered.

## Switching to real AI generation

1. Set `AI_PROVIDER=openai` and `AI_API_KEY` (and optionally `AI_MODEL`) in the
   `news-ai-shared` environment group.
2. Set `AI_FACT_CHECK_ENABLED=true` (the default) so every draft gets the
   fact-check pass and the deterministic validation (spec section 28).
3. Redeploy. The worker picks up the new provider on its next tick.

Image generation is enabled (`IMAGE_ENABLED=true`) but optional: if no
`IMAGE_PROVIDER` is configured, articles publish without an image. Generated
images are never stored — only provider metadata and a transient URL reference
are kept (spec section 20).

## Notes and limitations

- The blueprint deploys the same Docker images used by `docker-compose.yml`, so
  the container layout (repo mirrored under `/repo`) is identical.
- Free instance types are not available for background workers; the worker uses
  the `starter` plan. Adjust `plan` values to your budget.
- Point-in-time recovery, high availability and larger instance types are Render
  account features; the blueprint uses a single `basic-256mb` database suitable
  for initial deployment.
- Custom domains: set them on the two web services. If you do, also set
  `NEXT_PUBLIC_API_BASE_URL` (backend CORS) and the frontend's
  `BACKEND_INTERNAL_URL`/`NEXT_PUBLIC_API_BASE_URL` accordingly.
