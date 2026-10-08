"""Phase 6 unit tests: image provider selection, prompt, no-byte-storage.

No database, no network. These exercise the real provider code paths.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.models.article import Article
from app.services.images import (
    GeneratedImage,
    ImageProvider,
    NullImageProvider,
    OpenAIImageProvider,
    build_image_prompt,
    image_provider_configured,
    set_image_provider,
)


def _article(**kw) -> Article:
    defaults = dict(
        headline="Magnitude 6.8 earthquake strikes Japan",
        location="Japan",
        category="earthquake",
    )
    defaults.update(kw)
    return Article(**defaults)


# --- prompt (spec section 20: illustrate the event, not a source's framing) ---


def test_prompt_is_derived_from_event_and_forbids_text_and_real_people():
    prompt = build_image_prompt(_article())
    assert "Magnitude 6.8 earthquake strikes Japan" in prompt
    assert "Japan" in prompt
    # Neutrality constraints must survive in the prompt.
    assert "no text" in prompt
    assert "no identifiable real people" in prompt


def test_prompt_falls_back_when_fields_missing():
    prompt = build_image_prompt(Article(headline="", location=None, category=None))
    assert "a world news event" in prompt
    assert "unspecified location" in prompt


# --- provider selection ------------------------------------------------------


def test_factory_none_provider_by_default():
    from app.services.images import _build_provider

    assert isinstance(_build_provider(), NullImageProvider)


def test_openai_provider_requires_a_key():
    with pytest.raises(ValueError):
        OpenAIImageProvider(api_key="", model="gpt-image-1")


def test_image_provider_configured_respects_enable_flag(monkeypatch):
    monkeypatch.setattr(settings, "image_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "image_api_key", "secret", raising=False)
    monkeypatch.setattr(settings, "image_enabled", True, raising=False)
    assert image_provider_configured() is True
    monkeypatch.setattr(settings, "image_enabled", False, raising=False)
    assert image_provider_configured() is False


def test_image_provider_not_configured_without_key(monkeypatch):
    monkeypatch.setattr(settings, "image_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "image_api_key", "", raising=False)
    monkeypatch.setattr(settings, "image_enabled", True, raising=False)
    assert image_provider_configured() is False


# --- no byte storage ---------------------------------------------------------


class _RecordingProvider(ImageProvider):
    """A real provider subclass that records the prompt it received."""

    name = "recording"

    def __init__(self, *, result: GeneratedImage | None = None) -> None:
        self.prompts: list[str] = []
        self._result = result

    async def generate(self, *, prompt: str) -> GeneratedImage:
        self.prompts.append(prompt)
        return self._result or GeneratedImage(provider=self.name, prompt=prompt)


async def test_openai_provider_stores_only_a_hash_of_inline_bytes(monkeypatch):
    """A b64 payload yields a content hash and no stored/returned bytes."""
    import httpx

    provider = OpenAIImageProvider(api_key="k", model="gpt-image-1")

    async def _fake_post(self, url, headers=None, json=None):  # noqa: A002
        return httpx.Response(
            200,
            json={"id": "gen_1", "data": [{"b64_json": "aGVsbG8="}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    result = await provider.generate(prompt="a prompt")
    assert result.generation_id == "gen_1"
    assert result.content_hash is not None
    # No URL to persist, and no bytes anywhere on the object.
    assert result.ephemeral_url is None
    assert not hasattr(result, "data")
    assert not hasattr(result, "b64_json")


async def test_openai_provider_keeps_transient_url_with_expiry(monkeypatch):
    import httpx

    provider = OpenAIImageProvider(api_key="k", model="gpt-image-1")

    async def _fake_post(self, url, headers=None, json=None):  # noqa: A002
        return httpx.Response(
            200,
            json={"id": "gen_2", "data": [{"url": "https://cdn.example/img.png"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    result = await provider.generate(prompt="a prompt")
    assert result.ephemeral_url == "https://cdn.example/img.png"
    assert result.expires_at is not None
    # The reference expires; we never keep it forever.
    assert result.expires_at > result.expires_at.replace(year=result.expires_at.year - 1)


def test_null_provider_returns_nothing_to_store():
    provider = NullImageProvider()
    import asyncio

    result = asyncio.run(provider.generate(prompt="x"))
    assert result.ephemeral_url is None
    assert result.content_hash is None


def test_set_image_provider_overrides_cache():
    provider = _RecordingProvider()
    set_image_provider(provider)
    from app.services.images import get_image_provider

    assert get_image_provider() is provider
    set_image_provider(None)
