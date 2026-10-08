"""Deterministic article validation (spec sections 28-29).

Layer 6 of the hallucination safeguards, and the last gate before publication.
It does not trust the writer: every number, date and name in the draft must
appear in the evidence package, and the draft must not reproduce a source
article.

Checks (spec section 29):

* every number in the draft exists in the evidence
* every year/date in the draft exists in the evidence
* no fabricated URLs
* no unsupported proper nouns (organisations, places, people)
* no duplicated paragraphs
* no source article copying (n-gram overlap with the source reports)

A draft that fails is rejected and never published. Rejection is a valid
outcome: "not enough verified information to publish" is a product behaviour,
not an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.services.ai import ArticleDraft
from app.services.evidence import evidence_text

# Numbers are compared as values, so "12" in the draft matches "12" in evidence
# regardless of surrounding punctuation.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_URL = re.compile(r"https?://[^\s)\]]+", re.IGNORECASE)
# A single capitalised word: a candidate proper noun (place, person, org).
# Checked word-by-word rather than as a run, because evidence may name the same
# entities separately ("Japan" and "Earthquake" in different facts).
_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]+)\b")

# Minimum length of a proper noun before it is checked; short words are too
# ambiguous to treat as fabricated names.
_MIN_NAME_LENGTH = 4
# n-gram size for the source-copying check.
_COPY_NGRAM = 8
# Duplicate-paragraph check: identical paragraph seen twice.
_MIN_PARAGRAPH_CHARS = 40


@dataclass
class ValidationResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    # Non-fatal observations, recorded for the accuracy dashboard.
    warnings: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.failures)


def _numbers(text: str) -> set[float]:
    values: set[float] = set()
    for match in _NUMBER.finditer(text):
        raw = match.group(0).replace(",", "")
        try:
            values.add(float(raw))
        except ValueError:  # pragma: no cover - regex guarantees numeric
            continue
    return values


def _ngrams(text: str, size: int) -> set[tuple[str, ...]]:
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    if len(tokens) < size:
        return set()
    return {tuple(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def _is_sentence_initial(text: str, start: int) -> bool:
    """Whether the word at ``start`` begins a sentence.

    Sentence-initial capitalisation is grammatical, not a name, so those words
    are not treated as candidate proper nouns. Only mid-sentence capitals are
    checked, which is what catches a fabricated official without flagging
    ordinary prose.
    """
    prefix = text[:start].rstrip()
    if not prefix:
        return True
    return prefix[-1] in '.!?:;"\n()-'


def _proper_nouns(text: str) -> set[str]:
    names: set[str] = set()
    for match in _PROPER_NOUN.finditer(text):
        candidate = match.group(1)
        if len(candidate) < _MIN_NAME_LENGTH:
            continue
        if _is_sentence_initial(text, match.start()):
            continue
        names.add(candidate.casefold())
    return names


def validate_draft(
    draft: ArticleDraft,
    evidence: dict[str, Any],
    *,
    source_texts: list[str] | None = None,
) -> ValidationResult:
    """Validate a draft against the evidence. Never publishes on doubt."""
    failures: list[str] = []
    warnings: list[str] = []

    if draft.insufficient_evidence:
        return ValidationResult(ok=False, failures=["writer reported insufficient evidence"])

    body = draft.body or ""
    headline = draft.headline or ""
    if not headline.strip():
        failures.append("missing headline")
    if not body.strip():
        failures.append("missing body")

    evidence_text_lower = evidence_text(evidence)
    evidence_numbers = _numbers(evidence_text_lower)
    article_text = f"{headline}\n{body}\n" + "\n".join(draft.key_points or [])

    # --- Numbers (spec section 29: no invented statistics) ---
    unsupported_numbers = sorted(_numbers(article_text) - evidence_numbers)
    if unsupported_numbers:
        failures.append(
            "numbers not in evidence: " + ", ".join(str(n) for n in unsupported_numbers)
        )

    # --- URLs (spec section 29: no fabricated URLs) ---
    if _URL.search(article_text):
        failures.append("article contains a URL; sources are attributed separately")

    # --- Proper nouns (spec section 29: no invented officials/places) ---
    unsupported_names = sorted(
        name for name in _proper_nouns(article_text) if name not in evidence_text_lower
    )
    if unsupported_names:
        failures.append("names not in evidence: " + ", ".join(unsupported_names))

    # --- Duplicated paragraphs ---
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    seen: set[str] = set()
    for paragraph in paragraphs:
        if len(paragraph) < _MIN_PARAGRAPH_CHARS:
            continue
        key = paragraph.casefold()
        if key in seen:
            failures.append("duplicated paragraph")
            break
        seen.add(key)

    # --- Source copying (spec section 17: articles must be different) ---
    # Threshold is configurable: synthesis reuses some phrasing, transcription
    # reuses most of it.
    limit = settings.max_source_copy_overlap
    if source_texts:
        draft_ngrams = _ngrams(article_text, _COPY_NGRAM)
        if draft_ngrams:
            worst = 0.0
            for source in source_texts:
                source_ngrams = _ngrams(source, _COPY_NGRAM)
                if not source_ngrams:
                    continue
                overlap = len(draft_ngrams & source_ngrams) / len(draft_ngrams)
                worst = max(worst, overlap)
            if worst > limit:
                failures.append(f"copies {worst:.0%} of a source article (limit {limit:.0%})")

    if len(body) < 200:
        warnings.append("short body")

    return ValidationResult(ok=not failures, failures=failures, warnings=warnings)
