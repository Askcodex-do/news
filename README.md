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
| 2 | News ingestion: source management, RSS/API ingestion, normalization, URL dedup, hashing, source health | planned |
| 3 | Event intelligence: semantic dedup, clustering, fact extraction, source independence, confidence | planned |
| 4 | AI editorial system: importance, generation, fact validation, attribution, versions | planned |
| 5 | Localization: IP → country, country → configured source, local/global ranking, 20 + 20 feeds | partial (geo + feed scaffold) |
| 6 | Images: generation with transient handling, no permanent storage | planned |
| 7 | Continuous operation: queues, retries, scheduling, monitoring, 24/7 workers | scaffold only |
| 8 | Production hardening: security, load/accuracy/duplicate testing, DR, backups, cost controls | planned |

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
pytest            # unit tests always run; integration tests need PostgreSQL
ruff check app tests
```

Integration tests skip automatically when no database is reachable, so the
unit suite is safe to run anywhere.

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
  app/services/     config loading, feed parsing, text normalization, geoip, seeding
frontend/           Next.js App Router UI
workers/            background workers (Phase 7)
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
- Fetched content will be sanitized and external URLs validated in Phase 2.
