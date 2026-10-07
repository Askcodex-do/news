# Workers

Background workers run continuously (24×7). Phase 2 implements the ingestion
worker; later phases add the rest. The worker *logic* lives in
`backend/app/services` so it ships in the backend image and is importable by
tests; this directory documents the layout and conventions.

Implemented (Phase 2):

```
backend/app/services/worker_loop.py     continuous scheduling + execution loop
backend/app/services/scheduler.py       due-source detection, job enqueueing, backoff
backend/app/services/queue.py           PostgreSQL job queue (idempotent, retryable)
backend/app/services/ingestion.py       fetch → parse → normalize → validate → dedup → store
backend/app/services/source_health.py   per-source health + rolling latency
```

Planned layout:

```
workers/
├── clustering_worker/     near-duplicate + event clustering
├── verification_worker/   fact extraction, multi-source verification, confidence
├── ranking_worker/        importance scoring, global/local ranking
├── article_worker/        AI editorial synthesis + fact validation
├── image_worker/          transient image generation (no permanent storage)
└── update_worker/         developing-story updates + stale-claim protection
```

Design rules that apply to every worker:

- **Idempotent.** Every job carries an idempotency key
  (e.g. `generate_article:event_18472:version_3`). A retry must never produce a
  second artifact.
- **Isolated failure.** One failing source or job must not stop the others.
  Failures are recorded and retried with backoff.
- **Accuracy first.** A worker that cannot verify enough facts emits
  "not enough verified information to publish" rather than a claim.
