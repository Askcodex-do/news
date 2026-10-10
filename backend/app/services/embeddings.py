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
    said new news report reports force forces forced prompt prompts prompted
    leave leaves left""".split()
)

# Number words are folded to digits so "twelve killed" and "12 killed" tokenize
# identically; digits are then dropped (see `_tokens`).
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1000,
}

# News-register synonyms folded onto one token so paraphrases share vocabulary
# (spec section 7). This is what lets the offline bag-of-tokens stand-in treat
# "quake" and "earthquake", or "killed" and "dead", as the same term. Kept small
# and deliberate: it helps a lexical embedder, it does not model language.
_SYNONYMS = {
    "quake": "earthquake",
    "tremor": "earthquake",
    "aftershock": "earthquake",
    "seismic": "earthquake",
    "temblor": "earthquake",
    "bushfire": "wildfire",
    "forestfire": "wildfire",
    "blaze": "wildfire",
    "flooding": "flood",
    "floods": "flood",
    "deluge": "flood",
    "kill": "death",
    "kills": "death",
    "killed": "death",
    "killing": "death",
    "deaths": "death",
    "dead": "death",
    "died": "death",
    "fatalities": "death",
    "fatal": "death",
    "toll": "death",
    "injuries": "injury",
    "wounded": "injury",
    "hits": "hit",
    "struck": "hit",
    "strike": "hit",
    "strikes": "hit",
    "striking": "hit",
    "evacuations": "evacuation",
    "evacuated": "evacuation",
    "displaced": "displacement",
    "displaces": "displacement",
    "thousands": "thousand",
}

# Light suffix stemming. Applied after synonym folding so "killed" -> "death"
# is not re-stemmed. Only strips common inflectional suffixes, and never below a
# three-character stem, to avoid mangling short words.
_STEM_SUFFIXES = ("ings", "ing", "ies", "ied", "es", "ed", "s")


def _stem(word: str) -> str:
    for suffix in _STEM_SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


class EmbeddingProvider(ABC):
    """Interface every embedding backend implements."""

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, returning one L2-normalized vector each."""


def _tokens(text: str) -> list[str]:
    # Tokens are folded (number words -> digits, news synonyms -> one term) and
    # lightly stemmed so paraphrases overlap, then digits are dropped: a changed
    # quantity ("5 killed" vs "12 killed") must not make two reports about the
    # same event look dissimilar. Quantities carry their own dedicated
    # clustering signal (number overlap) instead.
    tokens: list[str] = []
    for token in _TOKEN.findall(text.casefold()):
        if token in _STOPWORDS:
            continue
        if token in _NUMBER_WORDS:
            continue
        if token.isdigit():
            continue
        tokens.append(_stem(_SYNONYMS.get(token, token)))
    return tokens


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
