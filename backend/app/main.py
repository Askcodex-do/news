"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import set_geoip_provider
from app.api.middleware import RateLimitMiddleware, build_rate_limit_store
from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.security_log import log_config_problem
from app.services.geoip import build_geoip_provider

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    configure_logging()
    logger.info("starting news-ai backend (env=%s)", settings.app_env)

    problems = settings.production_problems()
    if problems:
        for problem in problems:
            log_config_problem(detail=problem)
        if settings.strict_config:
            raise RuntimeError("refusing to start with a production misconfiguration")

    provider = build_geoip_provider(
        settings.geoip_provider,
        database_path=settings.geoip_database_path,
        static_map=settings.geoip_static_map,
    )
    set_geoip_provider(provider)
    logger.info("geolocation backend: %s", type(provider).__name__)
    yield
    # Release the rate-limit store's Redis connection if one was opened.
    store = getattr(app.state, "rate_limit_store", None)
    if store is not None:
        await store.close()
    logger.info("shutting down news-ai backend")


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Global & Local News Platform",
        version="0.1.0",
        summary="Continuously running, accuracy-first news platform (Phase 1: foundation).",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.next_public_api_base_url],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )
    store = build_rate_limit_store()
    app.state.rate_limit_store = store
    app.add_middleware(RateLimitMiddleware, store=store)

    @app.middleware("http")
    async def security_headers(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        response.headers.setdefault("Content-Security-Policy", settings.content_security_policy)
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        if settings.enable_hsts:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={settings.hsts_max_age_seconds}; includeSubDomains",
            )
        return response

    app.include_router(api_router)
    return app


app = create_app()
