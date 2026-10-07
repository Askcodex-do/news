# Architecture

This document describes the target architecture and what Phase 1 has put in
place. It is intentionally explicit about the accuracy guarantees, because they
are the product, not a nice-to-have.

## The one rule that shapes everything

```
source article → paraphrase → publish      ← forbidden
```

```
multiple source reports
        ↓
fact extraction
        ↓
event identification
        ↓
source comparison
        ↓
conflict detection
        ↓
confidence assessment
        ↓
AI editorial synthesis
        ↓
original article
```

Sources are **evidence**, not the article. The published content is
independently synthesized and its sources are shown for transparency.

## End-to-end pipeline

```
                 SOURCE REPORTS
                       ↓
                 DUPLICATE CHECK          (URL → hash → near-duplicate)
                       ↓
                 EVENT CLUSTERING          (many reports → one event)
                       ↓
                 FACT EXTRACTION
                       ↓
               MULTI-SOURCE VERIFY        (count independent chains)
                       ↓
               CONFIDENCE + IMPORTANCE
                       ↓
                  AI EDITOR               (evidence-package in, article out)
                       ↓
                 FACT VALIDATION          (numbers/dates/names/quotes)
                       ↓
                 ORIGINAL ARTICLE
                       ↓
                    PUBLISH
                       ↓
                CONTINUOUS UPDATE
```

## Accuracy safeguards (layered)

1. **Structured evidence.** The writer receives a structured evidence package,
   not raw source text.
2. **Fact extraction.** Facts are extracted and tagged with the sources that
   assert them.
3. **Source comparison.** Agreement and disagreement are computed explicitly.
4. **AI generation.** The model is told: use only the supplied evidence; do not
   invent missing facts.
5. **AI fact-checking pass.** A second pass checks the draft against evidence.
6. **Deterministic validation.** Every number, date, name, location and quote
   in the draft must exist in the evidence; fabricated URLs and officials are
   rejected. Failing validation rejects the article.

Confidence and importance are separate scores. A fully confirmed minor event
might be `confidence 98 / importance 20`; a huge, still-developing event might
be `confidence 82 / importance 98`. The second needs more verification, not
immediate confident publication.

### Confidence bands (configurable)

| Band | Meaning |
| --- | --- |
| 95–100 | highly confirmed |
| 85–94 | strongly supported |
| 70–84 | reasonably supported |
| 50–69 | uncertain |
| < 50 | not published as factual news |

### Publication states

```
DETECTED → CLUSTERING → VERIFYING → VERIFIED → EDITORIAL_REVIEW → PUBLISHED → UPDATING → ARCHIVED
DETECTED → UNVERIFIED → WAIT
```

The system is comfortable not publishing. That is a feature.

## Data model (Phase 1 tables)

```
countries              sources               source_health
country_sources        source_reliability

source_reports         (raw reports, dedup hashes, simhash)

events                 event_reports         event_facts
event_conflicts        event_updates

articles               article_versions      article_sources
article_images         (metadata only — no stored media)

processing_jobs        (idempotency-keyed queue)
```

Notable columns:

- `source_reports.content_hash` — SHA-256 of normalized content (Level 2 dedup).
- `source_reports.normalized_url_hash` / canonical URL — Level 1 dedup.
- `source_reports.simhash` — Level 3 near-duplicate bucketing.
- `events.embedding` (`vector`) — semantic clustering (Phase 3).
- `processing_jobs.idempotency_key` — e.g. `generate_article:event_18472:version_3`
  so a retried job never creates a second article.

## Deduplication levels

1. **URL** — canonicalize (strip tracking params, fragments; sort query; force
   https; lowercase host). Same canonical URL is never ingested twice.
2. **Exact content** — `SHA-256(normalized_content)`.
3. **Near duplicate** — token similarity / SimHash / embeddings.

Event clustering goes further than article dedup: ten articles about one event
become one event, compared on location, time, entities, event type, numbers,
organizations, keywords and semantic similarity. Twenty reports → one event →
one editorial story.

## Independent-source verification

Ten sites that copied one wire story are one reporting chain, not ten
confirmations. Confidence is driven by `independent_sources`, not
`number_of_websites`.

## Continuous operation (Phase 7 target)

```
Scheduler
  ├── source workers        ├── verification worker   ├── image worker
  ├── dedup worker          ├── ranking worker        └── update worker
  ├── clustering worker     └── article worker
```

PostgreSQL + Redis/queue + worker processes. Every task is retryable and
idempotent. A failing source is recorded and retried with backoff without
affecting other sources; a failing image generation publishes the article
without an image.

## Localization

```
IP → geolocation → ISO country → country_sources → local source
```

IPs are used only to resolve a country and are not stored. If detection fails,
the visitor gets the global edition and nothing is blocked. Localization never
fabricates local impact; it only uses verified facts.

## Feed

- Global: rank international events → top 20.
- Local: rank country-specific events → top 20.

## Technology

- Backend: Python + FastAPI (async), SQLAlchemy 2.0, Alembic.
- Database: PostgreSQL 16 with `pgvector` and `pg_trgm`.
- Queue: Redis + Celery (wired in Phase 7; slot reserved in Compose).
- Frontend: Next.js (App Router) + React + TypeScript.
- Search: PostgreSQL full-text first; OpenSearch later if scale requires.
- Deployment: single VM with Docker Compose initially.
