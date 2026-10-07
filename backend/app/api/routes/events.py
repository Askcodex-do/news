from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.enums import EventStatus
from app.models.event import Event
from app.schemas import EventOut

router = APIRouter(prefix="/events", tags=["events"])

# Events that are safe to expose publicly (verified and beyond).
_PUBLIC_STATUSES = {
    EventStatus.VERIFIED,
    EventStatus.EDITORIAL_REVIEW,
    EventStatus.PUBLISHED,
    EventStatus.UPDATING,
    EventStatus.ARCHIVED,
}


@router.get("", response_model=list[EventOut])
async def list_events(
    session: AsyncSession = Depends(get_session),
    limit: int = 20,
) -> list[Event]:
    limit = max(1, min(limit, 100))
    stmt = (
        select(Event)
        .where(Event.status.in_(_PUBLIC_STATUSES))
        .order_by(Event.importance_score.desc(), Event.last_updated_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
