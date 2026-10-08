"""FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import set_geoip_provider
from app.api.middleware import RateLimitMiddleware
from app.api.router import api_router
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.services.geoip import build_geoip_provider

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    configure_logging()
    logger.info("starting news-ai backend (env=%s)", settings.app_env)
    provider = build_geoip_provider(
        settings.geoip_provider,
        database_path=settings.geoip_database_path,
        static_map=settings.geoip_static_map,
    )
    set_geoip_provider(provider)
    logger.info("geolocation backend: %s", type(provider).__name__)
    yield
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
    app.add_middleware(RateLimitMiddleware, requests_per_minute=120)

    @app.middleware("http")
    async def security_headers(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    app.include_router(api_router)
    return app


app = create_app()
