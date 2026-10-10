from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SchedulerState(Base, TimestampMixin):
    """Single-row lease that lets only one worker run the scheduler (spec section 25).

    Scheduling (deciding which sources are due and enqueueing their poll jobs) is
    not idempotent at the *source* level: every worker that scans would enqueue
    the same due source. Idempotency keys make that harmless for correctness, but
    it wastes work. A row-level lease means one worker schedules and the rest
    only execute, so N workers scale execution without N-way scheduling churn.

    The lease is advisory: if the holder dies, ``locked_at`` ages out and another
    worker takes over. Losing a scheduling tick is always safe — the next holder
    simply re-enqueues anything still due.
    """

    __tablename__ = "scheduler_state"

    # A fixed key so this table holds exactly one row ("singleton").
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    locked_by: Mapped[str | None] = mapped_column(String(120))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_result: Mapped[str | None] = mapped_column(Text)
