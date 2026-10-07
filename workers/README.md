# Workers

Background workers run continuously (24×7) and are introduced in Phase 7.
Phase 1 reserves this structure and the Compose `worker` service; no worker
logic runs yet.

Planned layout:

```
workers/
├── ingestion_worker/      fetch → parse → normalize → validate → dedup → store
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
