from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    """Liveness + dependency check used by orchestrators and monitoring."""
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False

    from app.models.source import Source  # local import avoids model import cost at boot

    source_count = 0
    if db_ok:
        source_count = int(await session.scalar(select(func.count()).select_from(Source)) or 0)

    return {
        "status": "ok" if db_ok else "degraded",
        "env": settings.app_env,
        "database": "ok" if db_ok else "unavailable",
        "source_count": source_count,
    }
