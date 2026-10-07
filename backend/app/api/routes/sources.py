from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.session import get_session
from app.models.source import Source, SourceHealth
from app.schemas import SourceHealthOut, SourceOut

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=list[SourceOut])
async def list_sources(
    session: AsyncSession = Depends(get_session),
    enabled: bool | None = Query(default=None),
    international: bool | None = Query(default=None),
) -> list[Source]:
    stmt = select(Source).order_by(Source.priority.desc(), Source.name)
    if enabled is not None:
        stmt = stmt.where(Source.enabled.is_(enabled))
    if international is not None:
        stmt = stmt.where(Source.is_international.is_(international))
    return list((await session.execute(stmt)).scalars().all())


@router.get("/{slug}", response_model=SourceOut)
async def get_source(slug: str, session: AsyncSession = Depends(get_session)) -> Source:
    source = (await session.execute(select(Source).where(Source.slug == slug))).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    return source


@router.get(
    "/{slug}/health",
    response_model=SourceHealthOut,
    dependencies=[Depends(require_admin)],
)
async def get_source_health(
    slug: str, session: AsyncSession = Depends(get_session)
) -> SourceHealth:
    source = (await session.execute(select(Source).where(Source.slug == slug))).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    health = (
        await session.execute(select(SourceHealth).where(SourceHealth.source_id == source.id))
    ).scalar_one_or_none()
    if health is None:
        raise HTTPException(status_code=404, detail="no health record for source")
    return health
