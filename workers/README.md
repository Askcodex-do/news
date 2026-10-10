# Workers

Background workers run continuously (24×7). Phase 2 implements the ingestion
worker; later phases add the rest. The worker *logic* lives in
`backend/app/services` so it ships in the backend image and is importable by
tests; this directory documents the layout and conventions.

Implemented:

```
backend/app/services/worker_loop.py     continuous scheduling + execution loop
backend/app/services/scheduler.py       due-source detection, job enqueueing, backoff
backend/app/services/queue.py           PostgreSQL job queue (idempotent, retryable) + scheduler lease
backend/app/services/ingestion.py       fetch → parse → normalize → validate → dedup → store
backend/app/services/clustering.py      near-duplicate + event clustering
backend/app/services/verification.py    fact extraction, multi-source verification, confidence
backend/app/services/ranking.py         importance scoring, global/local ranking
backend/app/services/article_generation.py  AI editorial synthesis + fact validation
backend/app/services/images.py          transient image generation (no permanent storage)
backend/app/services/maintenance.py     stale-event archiving, job retention (Phase 7)
backend/app/services/observability.py   ops + accuracy snapshots (Phase 7)
backend/app/services/source_health.py   per-source health + rolling latency
```

The single worker process runs every stage; the job types below are dispatched
by `scheduler.process_pending_jobs`:

```
poll_source → cluster_event → verify_event → generate_article → generate_image
```

Scaling: `--scale worker=N`. The scheduler lease (`scheduler_state`) means one
replica schedules poll jobs while all of them execute, so adding replicas adds
execution throughput without N-way scheduling churn.

Design rules that apply to every worker:

- **Idempotent.** Every job carries an idempotency key
  (e.g. `generate_article:event_18472:version_3`). A retry must never produce a
  second artifact.
- **Isolated failure.** One failing source or job must not stop the others.
  Failures are recorded and retried with backoff.
- **Accuracy first.** A worker that cannot verify enough facts emits
  "not enough verified information to publish" rather than a claim.
