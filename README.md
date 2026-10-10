# News AI — Global & Local News Platform

An accuracy-first news platform. It watches a fixed set of international and
local news sources, groups reports that describe the same real-world event,
verifies facts across independent sources, and then publishes **original**
AI-written articles — never a paraphrase of a single source.

> **Core rule.** Accuracy outranks speed, volume, SEO and engagement. If an
> event cannot be sufficiently verified, the platform does not publish it as
> fact. "Not enough verified information to publish" is a valid, expected
> outcome — it is a product requirement, not a prompt suggestion.

## Status

This repository is being built in phases (see the task specification).

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Foundation: repo, Docker, PostgreSQL, FastAPI, Next.js, migrations, config, tests | **implemented** |
| 2 | News ingestion: source management, RSS/API ingestion, normalization, URL dedup, hashing, source health | **implemented** |
| 3 | Event intelligence: semantic dedup, clustering, fact extraction, source independence, confidence | **implemented** |
| 4 | AI editorial system: importance, generation, fact validation, attribution, versions | **implemented** |
| 5 | Localization: IP → country, country → configured source, local/global ranking, 20 + 20 feeds | **implemented** |
| 6 | Images: generation with transient handling, no permanent storage | **implemented** |
| 7 | Continuous operation: queues, retries, scheduling, monitoring, 24/7 workers | **implemented** |
| 8 | Production hardening: security, load/accuracy/duplicate testing, DR, backups, cost controls | **implemented** |

What Phase 1 delivers today:

- A YAML-driven source configuration layer (exactly 30 international sources,
  17 local sources, 32 country→source mappings) with validation and no
  hard-coded URLs in application code.
- A full PostgreSQL schema (16 tables) created by Alembic migrations, with
  `pgvector` and `pg_trgm` for later clustering.
- A FastAPI backend exposing health, geo, feed, articles, events and sources.
- IP → country resolution with a safe global fallback (never blocks the site).
- A Next.js frontend that renders the global + local 20/20 feed shell.
- Docker Compose for local and single-VM deployment, including a
  least-privilege database role.
- Unit tests plus integration tests that exercise real database code paths.

What Phase 2 adds:

- An asynchronous ingestion pipeline (`app/services/ingestion.py`):
  fetch → parse (RSS/Atom) → normalize → validate → deduplicate → store, per
  source, with per-entry rejection instead of a whole-feed failure.
- Three-level duplicate detection: canonical URL (Level 1), SHA-256 of
  normalized content (Level 2), and a near-duplicate hash (SimHash) stored for
  Level 3 clustering in Phase 3.
- A PostgreSQL-backed job queue (`app/services/queue.py`) with idempotency
  keys, `FOR UPDATE SKIP LOCKED` claiming, exponential backoff, dead-lettering
  and stale-lock recovery. No duplicate jobs, even across worker crashes.
- A scheduler (`app/services/scheduler.py`) that enqueues poll jobs for due
  sources, with per-source backoff when a source is failing.
- Source health tracking (`app/services/source_health.py`): success/failure
  counts, rolling latency and `unknown → healthy → degraded → down` status.
- A continuous worker loop (`app/services/worker_loop.py`) that runs the whole
  thing 24/7 and survives any single failure.
- Admin/ops endpoints under `/admin` (token-protected) for source health, queue
  depth, ingestion counters, manual polling and scheduler control.

What Phase 3 adds:

- Event clustering (`app/services/clustering.py`): reports about the same
  real-world event collapse into one `event`, compared on headline semantics
  plus entity and number overlap, bounded by a time window. Ten articles about
  one earthquake become one event, not ten stories.
- Fact extraction (`app/services/facts.py`): entities, quantities and an event
  type per report, with unit aliases so "killed" and "deaths" agree.
- Source independence (`app/services/independence.py`): near-duplicate reports
  count as one independent chain, so ten sites republishing one wire story are
  not ten confirmations.
- Verification and confidence (`app/services/verification.py`,
  `app/services/confidence.py`): facts, conflicts, recency-based supersession
  and a 0-100 confidence score with configurable publish thresholds.
- Admin observability: `GET /admin/events`, `GET /admin/intelligence/stats`
  and `POST /admin/events/{id}/verify` for operator review.
- `python -m app.cli cluster-backfill` clusters reports ingested before Phase 3
  so an upgrade does not wait for the next poll cycle.

What Phase 4 adds:

- Importance scoring (`app/services/importance.py`), kept separate from
  confidence: a confirmed trivial event is not news, and a huge developing
  event still needs verification.
- An AI writer abstraction (`app/services/ai.py`): an OpenAI writer for
  production and a deterministic, offline writer for tests and local dev.
- A structured evidence package (`app/services/evidence.py`): the writer only
  ever receives facts deterministic verification already attributed, split into
  confirmed vs single-source support, plus conflicts and source links.
- A deterministic publish gate (`app/services/article_validation.py`) that
  rejects a draft with unsupported numbers, fabricated names or URLs, duplicated
  paragraphs or verbatim source copying, followed by an AI fact-check pass.
- Article generation (`app/services/article_generation.py`): one article per
  event, versioned, with transparent source attribution. A developing story
  keeps one article and appends versions rather than publishing a new story.
- Admin endpoints `POST /admin/events/{id}/generate` and
  `GET /admin/articles/rejected` (the accuracy dashboard's do-not-publish queue).

What Phase 5 adds:

- Geolocation (`app/services/geoip.py`): IP → ISO country with a pluggable
  backend (`null`, `static`, `maxmind`), selected from configuration and wired
  at startup. The visitor IP is used only to resolve a country and is never
  stored; an unresolved IP or unknown country falls back to the global edition
  and never blocks the site.
- Country → local source (`config/country_sources.yaml`, `country_sources`
  table): the mapping is data, never an LLM prompt.
- Feed ranking (`app/services/ranking.py`): a transparent, bounded blend of
  importance, confidence and recency — explicitly not an engagement score — that
  produces a ranked global top-20 and local top-20.
- Localized editions (`generate_localized_articles`): an event can carry a
  global article plus one country-angle edition per locale, each written from
  the same verified evidence so localization re-angles facts without inventing
  local impact.
- `GET /feed` now serves the ranked global + local feed, and the Next.js page
  forwards the visitor IP so the local column matches their country.

What Phase 6 adds:

- An image provider abstraction (`app/services/images.py`): an OpenAI generator
  (`IMAGE_PROVIDER=openai`) and a null provider. The image prompt is derived from
  the *event* (headline, location, category) with explicit neutrality
  constraints — not from a source's photo or framing.
- No permanent image storage (spec section 20): the platform keeps only
  metadata — provider, generation id, prompt hash, and a transient URL with an
  expiry. A base64 response is reduced to a hash and its bytes are discarded;
  nothing is written to disk or the database.
- Optional, non-blocking images (spec section 27): image generation runs as its
  own `generate_image` job, enqueued only after an article is published and only
  when a provider is configured. A failing or absent provider is swallowed and
  the article stands without an image.
- Expiry hygiene: `purge_expired_image_references` (and `POST /admin/images/purge`)
  drops transient URL references once they expire, so no stale third-party link
  lingers.
- Article detail exposes image metadata only, and
  `POST /admin/articles/{id}/image` generates or reuses a reference on demand.

What Phase 7 adds:

- Single-flight scheduling (spec section 25): `scheduler_state` is a one-row
  lease, so exactly one worker scans for due sources per tick while every
  replica executes jobs. A dead holder's lease ages out and another worker takes
  over; losing the race just skips a scheduling pass. Scale workers with
  `docker compose up --scale worker=3`.
- Observability endpoints (spec section 33): `GET /admin/metrics` returns an
  operational snapshot (sources online/offline, ingestion/hour, event counts,
  queue depth and job outcomes, last scheduling run), and
  `GET /admin/accuracy` is the accuracy dashboard — published articles, fact
  validation failures, corrections (versions beyond the first), open source
  conflicts and low-confidence publications.
- Housekeeping (spec sections 22, 25): the worker periodically archives active
  events with no new reports for `STALE_EVENT_HOURS` and prunes SUCCEEDED jobs
  past `JOB_RETENTION_DAYS`. Dead jobs are kept for diagnosis.
  `python -m app.cli maintenance` runs a sweep by hand; `POST
  /admin/maintenance/run` does the same over HTTP.
- Failure recovery already in place from Phase 2 is unchanged: stale locks are
  returned to PENDING, retries use exponential backoff, and one bad source or
  job never stalls the loop.

## Quick start (Docker Compose)

```bash
cp .env.example .env          # then edit secrets
docker compose up --build
```

Then:

- Frontend: <http://localhost:12000>
- Backend API: <http://localhost:8000>
- API docs: <http://localhost:8000/docs>

The `migrate` service applies migrations and seeds the database from
`config/*.yaml` before the API and worker start.

## Deploying to Render

A Render Blueprint (`render.yaml`) provisions the API, the 24/7 worker, the
frontend and a managed Postgres from this repository. Applying it needs a Render
account; see `docs/deployment-render.md` for the one-time dashboard steps and
the post-deploy verification commands.

## Local development (no Docker for the app)

```bash
# 1. Start only the datastores
docker compose up -d postgres redis

# 2. Backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
python -m app.cli seed
uvicorn app.main:app --reload

# 3. Frontend
cd ../frontend
npm install
npm run dev
```

### Tests and lint

```bash
cd backend
. .venv/bin/activate

# Database-backed tests use an isolated `news_test` database (never the live
# one). Create and migrate it once:
psql -c "CREATE DATABASE news_test OWNER news_app"
DATABASE_URL=postgresql+asyncpg://news_app:change-me@localhost:5432/news_test \
  alembic upgrade head
pytest
ruff check app tests
```

CI (`.github/workflows/ci.yml`) runs this on every pull request: a backend job
brings up the `pgvector/pg16` and `redis` images, provisions `news_test` with
the least-privilege role and the `vector`/`pg_trgm` extensions, migrates it,
then runs `ruff check`, `ruff format --check`, and `pytest`; a second job runs
`npm ci && npm run build` for the frontend.

Integration and ingestion tests skip automatically when the test database is
missing or unmigrated, so the unit suite is safe to run anywhere.

The Phase 8 suites cover hardening and accuracy:

- `tests/test_phase8.py` — SSRF guard, rate-limit stores/middleware, AI cost
  guard, production config guard, security logging.
- `tests/test_phase8_db.py` — a synthetic "one event, many sources" corpus that
  must collapse to one event and one article with the independent-chain count
  (not the website count), plus a set of fabricated drafts that must all be
  rejected.

Load testing runs against a live server (start the API first):

```bash
cd backend && . .venv/bin/activate
python tests/load/loadtest.py --base-url http://localhost:8000 \
  --users 50 --duration 30 --max-p95-ms 400 --max-error-rate 0.01
```

### Running the ingestion worker

```bash
cd backend
. .venv/bin/activate

python -m app.cli worker              # continuous 24/7 loop
python -m app.cli ingest-once         # one scheduling + execution pass
python -m app.cli poll-source bbc-world   # poll a single source now
```

Under Docker Compose the `worker` service runs `python -m app.cli worker`.

## Configuration

All source URLs live in `config/`, never in application code:

- `config/international_sources.yaml` — exactly 30 international sources.
- `config/local_sources.yaml` — one local source per supported country.
- `config/country_sources.yaml` — the `country → source` mapping
  (`IN → indiatoday`, `PK → dawn`, `JP → nhk-world`, `GB → bbc-world`, …).

`python -m app.cli export-seeds` writes reviewable JSON snapshots of this
configuration to `database/seeds/`.

The platform never asks a model "find a reputable Indian news site". It already
knows that `IN → India Today`; the AI only analyzes the information that source
reports.

## Repository layout

```
backend/            FastAPI app, domain models, services, API routes, tests
  app/api/          HTTP layer (routes, deps, middleware, router)
  app/core/         settings, logging
  app/db/           engine, session, metadata base
  app/models/       SQLAlchemy models (16 tables)
  app/schemas/      Pydantic request/response models
  app/services/     config loading, feed parsing, text normalization, ingestion,
                    queue, scheduler, source health, geoip, seeding
frontend/           Next.js App Router UI
workers/            worker layout notes (the Phase 2 loop lives in app/services)
config/             source + country configuration (source of truth)
database/           migrations and generated seeds
infrastructure/     Dockerfiles, postgres init, deployment notes
docs/               architecture and operations notes
tests/              (backend tests live in backend/tests)
```

## Architecture in one line

```
source reports → duplicate check → event clustering → fact extraction
→ multi-source verification → confidence + importance → AI editor
→ fact validation → original article → publish → continuous update
```

See `docs/architecture.md` for the pipeline, data model and the accuracy
safeguards that make this different from "scrape → rewrite → publish".

## Security notes

- Secrets are read from environment variables only; the browser never receives
  AI keys or the admin token.
- Admin/ops endpoints require the `X-Admin-Token` header.
- Visitor IPs are used only to resolve a country and are not stored.
- The application connects to PostgreSQL as a least-privilege role, not the
  superuser.
- Ingestion only fetches configured `http(s)` URLs, strips HTML from feed
  content, and rejects non-`http(s)` links; `raw_text_permitted` gates whether
  publisher full text is stored at all.
- Outbound fetches pass an SSRF guard (`app/services/url_safety.py`): only
  `http(s)` schemes are allowed and private, loopback, link-local and cloud
  metadata addresses are refused unless explicitly opted out for local dev.
- Responses carry security headers (`X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, and a strict `Content-Security-Policy`).
- Read endpoints are rate limited (in-process by default, Redis when configured)
  and rate-limit decisions are logged as security events without the raw IP.
- An AI cost budget (`ai_max_requests_per_hour`) defers generation when the
  hourly allowance is spent, and the spend is exposed on `/admin/metrics`.
- Startup refuses to run in `production` with default secrets, a weak database
  password, or untrusted proxy headers (`settings.production_problems()`).
