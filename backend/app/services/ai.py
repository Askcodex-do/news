"""AI provider abstraction for editorial synthesis (spec sections 16, 28).

The writer is never asked to "rewrite" a source. It receives a structured
evidence package (see ``evidence.py``) and is instructed to independently write
an original article using only the supplied facts.

Two providers ship:

* ``OpenAIProvider`` — the real writer, selected with ``AI_PROVIDER=openai`` and
  ``AI_API_KEY``. Without a key it refuses to build, so a misconfigured
  deployment fails loudly instead of publishing invented text.
* ``DeterministicAIProvider`` — offline, no key, stable output. It composes a
  template article strictly from the evidence's confirmed facts. It exists for
  tests and offline development and is *not* editorial synthesis; it is
  labelled as such so it can never be mistaken for a published voice.

Both return the same ``ArticleDraft`` so downstream validation and storage are
provider-agnostic.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_OPENAI_TIMEOUT = httpx.Timeout(90.0, connect=10.0)
_OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# The writer contract. Kept explicit so the model returns a stable shape.
_SYSTEM_PROMPT = """You are an original news writer for an accuracy-first publication.

You will receive a JSON evidence package. Write an original article from it.

Rules, in priority order:
1. Use ONLY facts present in the evidence package. Never invent a fact, number,
   date, name, location, quote or official that is not in the evidence.
2. Where the evidence marks a claim as conflicting or uncertain, say so plainly
   (for example "authorities have reported differing figures"). Do not pick a
   side.
3. Do not copy the structure, sentence order or distinctive wording of any
   source. Synthesize; do not paraphrase.
4. If the evidence is too thin to write a responsible article, return an empty
   body and set "insufficient_evidence" to true.
5. Attribute nothing to a source that did not report it.

Return a single JSON object with keys: headline, subtitle, body, key_points
(array of strings), timeline (array of {"at": ISO-8601, "summary": string}),
category, location, seo_title, seo_description, insufficient_evidence (bool).
"""


@dataclass(frozen=True)
class ArticleDraft:
    """The writer's output, before validation."""

    headline: str
    body: str
    subtitle: str | None = None
    key_points: list[str] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    category: str | None = None
    location: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    insufficient_evidence: bool = False
    # Token/cost accounting, when the provider reports it.
    usage: dict[str, Any] = field(default_factory=dict)


class AIProvider(ABC):
    """Interface every writer backend implements."""

    #: Identifier stored for auditing (spec section 33: API cost tracking).
    name: str = "unknown"

    @abstractmethod
    async def draft_article(
        self, *, evidence: dict[str, Any], audience_country: str | None = None
    ) -> ArticleDraft:
        """Write an original article from the evidence package."""

    @abstractmethod
    async def fact_check(self, *, evidence: dict[str, Any], draft: ArticleDraft) -> list[str]:
        """Return claims in the draft that the evidence does not support.

        This is the AI fact-checking pass (spec section 28, layer 5). The
        deterministic validator (layer 6) runs afterwards regardless; this pass
        exists to catch paraphrase-level drift the deterministic checks miss.
        """


def _extract_json(content: str) -> dict[str, Any]:
    """Parse a JSON object from a model response, tolerating code fences."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


class OpenAIProvider(AIProvider):
    """Original article synthesis via the OpenAI chat completions API."""

    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str = "") -> None:
        if not api_key:
            raise ValueError("AI_API_KEY is required for the openai provider")
        self._api_key = api_key
        self._model = model
        self._url = (base_url.rstrip("/") + "/chat/completions") if base_url else _OPENAI_CHAT_URL

    async def _chat(self, system: str, user: str, *, max_tokens: int) -> tuple[str, dict[str, Any]]:
        async with httpx.AsyncClient(timeout=_OPENAI_TIMEOUT) as client:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        usage = payload.get("usage") or {}
        return content, usage

    async def draft_article(
        self, *, evidence: dict[str, Any], audience_country: str | None = None
    ) -> ArticleDraft:
        user = (
            "Evidence package:\n"
            + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            + "\n\nWrite the original article now."
        )
        content, usage = await self._chat(
            _SYSTEM_PROMPT, user, max_tokens=settings.ai_max_tokens_per_article
        )
        data = _extract_json(content)
        timeline = data.get("timeline")
        return ArticleDraft(
            headline=str(data.get("headline") or "").strip(),
            body=str(data.get("body") or "").strip(),
            subtitle=(str(data["subtitle"]).strip() if data.get("subtitle") else None),
            key_points=_as_str_list(data.get("key_points")),
            timeline=timeline if isinstance(timeline, list) else [],
            category=(str(data["category"]) if data.get("category") else None),
            location=(str(data["location"]) if data.get("location") else None),
            seo_title=(str(data["seo_title"]) if data.get("seo_title") else None),
            seo_description=(str(data["seo_description"]) if data.get("seo_description") else None),
            insufficient_evidence=bool(data.get("insufficient_evidence")),
            usage=usage,
        )

    async def fact_check(self, *, evidence: dict[str, Any], draft: ArticleDraft) -> list[str]:
        system = (
            "You are a fact-checker. Compare the article against the evidence "
            'package. Return JSON {"unsupported": [string, ...]} listing every '
            "claim, number, name, date or quote in the article that the evidence "
            "does not support. Return an empty list if everything is supported."
        )
        user = (
            "Evidence package:\n"
            + json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            + "\n\nArticle:\n"
            + json.dumps(
                {"headline": draft.headline, "body": draft.body, "key_points": draft.key_points},
                ensure_ascii=False,
            )
        )
        content, _ = await self._chat(system, user, max_tokens=1000)
        data = _extract_json(content)
        return _as_str_list(data.get("unsupported"))


class DeterministicAIProvider(AIProvider):
    """Offline writer that composes strictly from the evidence's confirmed facts.

    Deterministic and dependency-free, so the pipeline, validation and storage
    are testable without a network or an API key. It performs no editorial
    synthesis — the output is a labelled template — so it must not be used for
    a real publication.
    """

    name = "offline"

    async def draft_article(
        self, *, evidence: dict[str, Any], audience_country: str | None = None
    ) -> ArticleDraft:
        event = evidence.get("event") or {}
        confirmed = evidence.get("confirmed_facts") or []
        conflicts = evidence.get("conflicting_claims") or []
        title = str(event.get("title") or "").strip()

        if not title or not confirmed:
            return ArticleDraft(headline=title, body="", insufficient_evidence=True)

        facts = [str(item.get("fact") or "").strip() for item in confirmed]
        facts = [f for f in facts if f]
        paragraphs = [" ".join(facts[:4])]
        if len(facts) > 4:
            paragraphs.append(" ".join(facts[4:]))
        if conflicts:
            paragraphs.append(
                "Some details remain disputed: "
                + "; ".join(str(item.get("claim") or "") for item in conflicts)
            )
        body = "\n\n".join(p for p in paragraphs if p)

        return ArticleDraft(
            headline=title,
            subtitle=None,
            body=body,
            key_points=facts[:5],
            timeline=[],
            category=event.get("event_type"),
            location=event.get("location"),
            seo_title=title[:70] or None,
            seo_description=(facts[0][:160] if facts else None),
            usage={"provider": "offline"},
        )

    async def fact_check(self, *, evidence: dict[str, Any], draft: ArticleDraft) -> list[str]:
        # The offline writer only copies evidence text, so it has no unsupported
        # claims to report; the deterministic validator still runs afterwards.
        return []


_provider: AIProvider | None = None


def _build_provider() -> AIProvider:
    name = (settings.ai_provider or "openai").strip().lower()
    if name in ("offline", "deterministic", "none"):
        return DeterministicAIProvider()
    if name == "openai":
        return OpenAIProvider(
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            base_url=settings.ai_base_url,
        )
    raise ValueError(f"unknown AI_PROVIDER: {settings.ai_provider!r}")


def get_ai_provider() -> AIProvider:
    """Return the configured writer, built once and cached."""
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def set_ai_provider(provider: AIProvider | None) -> None:
    """Override the writer (tests, or a deployment that injects its own)."""
    global _provider
    _provider = provider


def ai_provider_configured() -> bool:
    """Whether a real writer is configured.

    Used to keep the generation worker from retrying forever on a deployment
    that simply has no AI key: with no writer, articles are not generated and
    nothing is published. Accuracy first (spec section 35).
    """
    name = (settings.ai_provider or "openai").strip().lower()
    if name in ("offline", "deterministic", "none"):
        return True
    return bool(settings.ai_api_key)
