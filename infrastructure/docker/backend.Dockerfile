# Backend image: FastAPI API + workers share this image.
# The repo layout is mirrored under /repo so that the repo-relative paths used
# by app.core.config, alembic.ini and database/migrations resolve identically
# inside the container and on a developer machine.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo

COPY backend/pyproject.toml ./backend/pyproject.toml
COPY backend/app ./backend/app
COPY backend/alembic.ini ./backend/alembic.ini
COPY database ./database
COPY config ./config

RUN pip install --upgrade pip && pip install ./backend

# Run as an unprivileged user (least privilege).
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /repo
USER appuser

WORKDIR /repo/backend
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
