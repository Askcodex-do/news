from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Country(Base, TimestampMixin):
    """Reference table of countries (ISO 3166-1 alpha-2)."""

    __tablename__ = "countries"

    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    region: Mapped[str | None] = mapped_column(String(80))
    default_language: Mapped[str | None] = mapped_column(String(16))
    is_supported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
