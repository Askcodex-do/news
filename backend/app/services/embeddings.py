"""Sentence embeddings for event clustering (spec sections 8-9).

The clustering worker needs a semantic vector per report so that two headlines
such as "Earthquake strikes Japan killing 12" and "Twelve killed after
earthquake hits Japan" land near each other.

Two providers ship:

* ``HashingEmbeddingProvider`` — the default. Deterministic, offline, no API
  key, and stable across processes, which makes clustering reproducible and
  testable. It is a bag-of-token hashing vector, so it captures lexical overlap
  rather than deep semantics; it is deliberately a stand-in, not a substitute
  for a trained model.
* ``OpenAIEmbeddingProvider`` — a real embedding model, selected by setting
  ``EMBEDDING_PROVIDER=openai`` and ``EMBEDDING_API_KEY``.

Both return L2-normalized vectors, so cosine similarity is a plain dot product.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

import httpx

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")
_OPENAI_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

# Common words carry no topical signal and would otherwise dominate a
# bag-of-tokens vector. Kept small on purpose; stopword lists are easy to
# over-tune.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in is it its of on or that the
    this to was were will with after before over under into during amid say says
    said new news report reports""".split()
)


class EmbeddingProvider(ABC):
    """Interface every embedding backend implements."""

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one L2-normalized vector each."""


def _tokens(text: str) -> list[str]:
    # Numbers are dropped here on purpose: quantities carry their own dedicated
    # clustering signal (number overlap), and leaving them in would make
    # "flood kills 5" and "flood kills 12" look semantically different.
    return [t for t in _TOKEN.findall(text.casefold()) if t not in _STOPWORDS and not t.isdigit()]


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


class HashingEmbeddingProvider(EmbeddingProvider):
    """Deterministic bag-of-token hashing embedding.

    Each token is hashed to a bucket and weighted by sublinear term frequency
    (``1 + log(count)``), which is a standard cheap text vectorization. No
    network, no model download, and identical output for identical input.
    """

    def __init__(self, dim: int) -> None:
        if dim <= 0:
            raise ValueError("embedding dim must be positive")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _embed_one(self, text: str) -> list[float]:
        counts: dict[int, int] = {}
        for token in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % self._dim
            counts[bucket] = counts.get(bucket, 0) + 1
        vector = [0.0] * self._dim
        for bucket, count in counts.items():
            vector[bucket] = 1.0 + math.log(count)
        return _l2_normalize(vector)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Embeddings from the OpenAI embeddings endpoint."""

    def __init__(self, api_key: str, model: str, dim: int, base_url: str = "") -> None:
        if not api_key:
            raise ValueError("EMBEDDING_API_KEY is required for the openai provider")
        self._api_key = api_key
        self._model = model
        self._dim = dim
        self._url = (base_url.rstrip("/") + "/embeddings") if base_url else _OPENAI_EMBEDDINGS_URL

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with httpx.AsyncClient(timeout=_OPENAI_TIMEOUT) as client:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={"model": self._model, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()

        # The API documents that results are ordered by `index`.
        ordered = sorted(payload["data"], key=lambda item: item["index"])
        return [_l2_normalize(list(item["embedding"])) for item in ordered]


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Return the configured provider, built once and cached."""
    global _provider
    if _provider is None:
        _provider = _build_provider()
    return _provider


def _build_provider() -> EmbeddingProvider:
    name = (settings.embedding_provider or "hashing").strip().lower()
    if name in ("hashing", "hash", "local", ""):
        return HashingEmbeddingProvider(settings.embedding_dim)
    if name == "openai":
        return OpenAIEmbeddingProvider(
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            base_url=settings.ai_base_url,
        )
    raise ValueError(f"unknown EMBEDDING_PROVIDER: {settings.embedding_provider!r}")


def set_embedding_provider(provider: EmbeddingProvider | None) -> None:
    """Override the provider (tests, or a deployment that injects its own)."""
    global _provider
    _provider = provider


async def embed_texts(texts: list[str]) -> list[list[float]]:
    return await get_embedding_provider().embed(texts)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Vectors are normalized, so this is a dot product."""
    if len(a) != len(b):
        raise ValueError("vectors must have equal length")
    return sum(x * y for x, y in zip(a, b, strict=True))
