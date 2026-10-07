from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import articles, events, feed, geo, health, sources

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(geo.router)
api_router.include_router(feed.router)
api_router.include_router(articles.router)
api_router.include_router(events.router)
api_router.include_router(sources.router)
