"""Image generation with transient handling (spec sections 20, 27).

The platform never stores image bytes on its own infrastructure. This service:

* asks a provider for an image, deriving a neutral prompt from the *event* (not
  from a source's framing, so the picture is not a source's picture);
* keeps only metadata — provider, generation id, prompt hash, and a temporary
  provider URL with an expiry (spec section 20);
* treats a missing or failing provider as non-fatal: the article is published
  without an image rather than delayed (spec section 27).

Two providers ship, mirroring ``ai.py``:

* ``OpenAIImageProvider`` — the real generator (``IMAGE_PROVIDER=openai`` with
  ``IMAGE_API_KEY``). Requests ``b64_json`` and stores only its hash, never the
  bytes.
* ``NullImageProvider`` — no provider configured; returns nothing so the
  article simply has no image.
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.article import Article, ArticleImage

logger = get_logger(__name__)

_IMAGE_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_OPENAI_IMAGE_URL = "https://api.openai.com/v1/images/generations"

# The image must illustrate the event, not reproduce a source's photo or framing.
_PROMPT_TEMPLATE = (
    "A neutral, factual editorial illustration for a news article about: {subject}. "
    "Setting: {location}. Category: {category}. "
    "Documentary style, no text, no logos, no identifiable real people, no gore."
)


@dataclass
class GeneratedImage:
    """A generated image reference. Never holds bytes — only where to show it."""

    provider: str
    prompt: str
    generation_id: str | None = None
    # Temporary URL to display; may expire. ``None`` when the provider returns
    # inline data (base64) that we deliberately do not persist.
    ephemeral_url: str | None = None
    expires_at: datetime | None = None
    # SHA-256 of whatever the provider returned, for audit only.
    content_hash: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class ImageProvider(ABC):
    """Contract for an image generator. Implementations never persist bytes."""

    name: str = "unknown"

    @abstractmethod
    async def generate(self, *, prompt: str) -> GeneratedImage:
        """Generate one image for ``prompt`` and return a transient reference."""

    async def aclose(self) -> None:  # pragma: no cover - optional cleanup
        return None


class NullImageProvider(ImageProvider):
    """No image provider configured: articles are published without an image."""

    name = "none"

    async def generate(self, *, prompt: str) -> GeneratedImage:
        return GeneratedImage(provider=self.name, prompt=prompt, usage={"provider": "none"})


class OpenAIImageProvider(ImageProvider):
    """Real image generation via the OpenAI images endpoint.

    We request ``b64_json`` and keep only its hash; we never write the bytes to
    disk or the database (spec section 20). If the provider instead returns a
    temporary URL, we store that reference with an expiry.
    """

    name = "openai"

    def __init__(self, *, api_key: str, model: str, base_url: str = "") -> None:
        if not api_key:
            raise ValueError("IMAGE_API_KEY is required for IMAGE_PROVIDER=openai")
        self._api_key = api_key
        self._model = model
        self._url = (base_url or _OPENAI_IMAGE_URL).rstrip("/")

    async def generate(self, *, prompt: str) -> GeneratedImage:
        payload = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
        }
        async with httpx.AsyncClient(timeout=_IMAGE_TIMEOUT) as client:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        items = data.get("data") or []
        if not items:
            raise RuntimeError("image provider returned no image data")
        item = items[0]

        content_hash: str | None = None
        ephemeral_url: str | None = None
        if item.get("b64_json"):
            content_hash = hashlib.sha256(item["b64_json"].encode("utf-8")).hexdigest()
        elif item.get("url"):
            ephemeral_url = str(item["url"])
            content_hash = hashlib.sha256(ephemeral_url.encode("utf-8")).hexdigest()

        expires_at = None
        if ephemeral_url:
            expires_at = datetime.now(UTC) + timedelta(seconds=settings.image_url_ttl_seconds)

        return GeneratedImage(
            provider=self.name,
            prompt=prompt,
            generation_id=str(data.get("id")) if data.get("id") else None,
            ephemeral_url=ephemeral_url,
            expires_at=expires_at,
            content_hash=content_hash,
            usage={"provider": "openai", "model": self._model},
        )


_provider: ImageProvider | None = None


def _build_provider() -> ImageProvider:
    name = (settings.image_provider or "none").strip().lower()
    if name in ("", "none", "null", "offline"):
        return NullImageProvider()
    if name == "openai":
        return OpenAIImageProvider(
            api_key=settings.image_api_key,
            model=settings.image_model,
            base_url=settings.image_base_url,
        )
    raise ValueError(f"unknown IMAGE_PROVIDER: {settings.image_provider!r}")


def get_image_provider() -> ImageProvider:
    """Return the configured image provider, built once and cached."""
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def set_image_provider(provider: ImageProvider | None) -> None:
    """Override the provider (tests, or a deployment that injects its own)."""
    global _provider
    _provider = provider


def image_provider_configured() -> bool:
    """Whether a real image provider is configured.

    With no provider (or images disabled) we never enqueue image jobs, so the
    worker does not spin on a deployment that has no image key.
    """
    if not settings.image_enabled:
        return False
    name = (settings.image_provider or "none").strip().lower()
    if name in ("", "none", "null", "offline"):
        return False
    return bool(settings.image_api_key)


def build_image_prompt(article: Article, *, event_type: str | None = None) -> str:
    """Derive a neutral image prompt from the event, not from a source's copy."""
    subject = (article.headline or "").strip() or "a world news event"
    location = (article.location or "unspecified location").strip()
    category = (event_type or article.category or "general news").strip()
    return _PROMPT_TEMPLATE.format(subject=subject, location=location, category=category)


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


async def _existing_image(session: AsyncSession, article_id: uuid.UUID) -> ArticleImage | None:
    return (
        await session.execute(
            select(ArticleImage).where(ArticleImage.article_id == article_id).limit(1)
        )
    ).scalar_one_or_none()


async def attach_image_to_article(
    session: AsyncSession,
    article_id: uuid.UUID,
    *,
    event_type: str | None = None,
) -> ArticleImage | None:
    """Generate an image for a published article and record metadata only.

    Idempotent per article: an existing image reference is returned unchanged.
    A provider failure is logged and swallowed — the caller must be able to
    publish the article regardless (spec section 27).
    """
    article = await session.get(Article, article_id)
    if article is None:
        return None

    existing = await _existing_image(session, article_id)
    if existing is not None:
        return existing

    prompt = build_image_prompt(article, event_type=event_type)
    provider = get_image_provider()

    try:
        generated = await provider.generate(prompt=prompt)
    except Exception as exc:  # noqa: BLE001 - an image must never block publishing
        logger.warning("image generation failed for article %s: %s", article_id, exc)
        return None

    if generated.ephemeral_url is None and generated.content_hash is None:
        # Null provider / no usable output: nothing to show, nothing to store.
        logger.info("no image generated for article %s (provider=%s)", article_id, provider.name)
        return None

    image = ArticleImage(
        article_id=article_id,
        image_provider=generated.provider,
        generation_id=generated.generation_id,
        prompt_hash=_prompt_hash(generated.prompt),
        ephemeral_url=generated.ephemeral_url,
        expires_at=generated.expires_at,
    )
    session.add(image)
    await session.flush()
    logger.info(
        "image reference recorded for article %s (provider=%s, hash=%s)",
        article_id,
        generated.provider,
        (generated.content_hash or "")[:12],
    )
    return image


async def purge_expired_image_references(session: AsyncSession) -> int:
    """Delete expired transient image references (spec section 20).

    We hold a provider URL only for as long as it is valid; once it expires the
    reference is dropped so no stale third-party link lingers.
    """
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(ArticleImage).where(
                    ArticleImage.expires_at.is_not(None), ArticleImage.expires_at <= now
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)
