# Tests

Backend unit and integration tests live in [`../backend/tests`](../backend/tests)
so they can import the application package and run with the backend virtual
environment:

```bash
cd backend
. .venv/bin/activate
pytest
```

- Unit tests (config loading, text normalization, feed parsing, geoip) always run.
- Integration tests exercise the real PostgreSQL models and API handlers and skip
  automatically when no database is reachable.

Cross-cutting end-to-end tests (load, accuracy, duplicate handling) are added in
Phase 8.
