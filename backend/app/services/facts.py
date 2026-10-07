"""Deterministic fact extraction (spec sections 9-10, 23-24).

The article generator must never invent facts, so facts are extracted here
without an LLM: numbers, dates, named entities and a coarse event type are
pulled from each report's title/summary and given a **stable key**. The same
claim found in two reports therefore collapses onto one row, and two reports
that disagree about a number (5 vs 12 deaths) share a key but differ in value —
which is exactly what conflict detection needs.

This is intentionally conservative: it extracts what it can prove from the text
and records nothing it cannot. Semantic nuance is left to the Phase 4 AI writer,
which is constrained to this evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

# --- Event type classification -------------------------------------------------

# Ordered: the first matching bucket wins, so more specific types come first.
_EVENT_TYPE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("earthquake", ("earthquake", "quake", "seismic", "magnitude", "tremor", "aftershock")),
    ("tsunami", ("tsunami", "tidal wave")),
    ("volcano", ("volcano", "volcanic", "eruption", "lava")),
    ("wildfire", ("wildfire", "bushfire", "forest fire", "blaze")),
    ("flood", ("flood", "flooding", "monsoon", "deluge")),
    ("storm", ("hurricane", "typhoon", "cyclone", "tornado", "storm")),
    ("drought", ("drought", "famine")),
    ("conflict", ("war", "strike", "missile", "airstrike", "shelling", "offensive", "combat")),
    ("terrorism", ("terror", "bombing", "bomb blast", "explosion", "suicide attack")),
    ("protest", ("protest", "demonstration", "rally", "riot", "unrest")),
    ("election", ("election", "vote", "ballot", "referendum", "poll")),
    ("politics", ("parliament", "minister", "president", "government", "senate", "bill")),
    ("economy", ("economy", "inflation", "gdp", "recession", "market", "stocks", "trade")),
    ("health", ("outbreak", "virus", "pandemic", "epidemic", "disease", "hospital", "vaccine")),
    ("accident", ("crash", "collision", "derail", "accident", "capsiz")),
    ("crime", ("murder", "arrest", "police", "shooting", "kidnap", "fraud")),
    ("sports", ("match", "tournament", "championship", "olympic", "league", "goal")),
    ("science", ("nasa", "spacecraft", "satellite", "launch", "rocket", "telescope")),
]


def classify_event_type(*texts: str) -> str | None:
    """Return a coarse event type from keyword evidence, or None."""
    haystack = " ".join(t for t in texts if t).casefold()
    if not haystack:
        return None
    for event_type, keywords in _EVENT_TYPE_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return event_type
    return None


# --- Numbers -------------------------------------------------------------------

# Words that map to the same measured quantity, so "12 killed" and "12 dead"
# are recognized as the same claim.
_MEASURE_ALIASES: dict[str, str] = {
    "kill": "deaths",
    "kills": "deaths",
    "killed": "deaths",
    "killing": "deaths",
    "dead": "deaths",
    "death": "deaths",
    "deaths": "deaths",
    "died": "deaths",
    "fatalities": "deaths",
    "fatal": "deaths",
    "toll": "deaths",
    "injured": "injured",
    "injury": "injured",
    "injuries": "injured",
    "wounded": "injured",
    "hurt": "injured",
    "missing": "missing",
    "evacuated": "evacuated",
    "evacuations": "evacuated",
    "displaced": "displaced",
    "homeless": "displaced",
    "magnitude": "magnitude",
    "quake": "magnitude",
    "arrested": "arrested",
    "detained": "arrested",
    "infected": "infected",
    "cases": "cases",
    "rescued": "rescued",
    "affected": "affected",
}

# Number tokens: 6.8, 1,200, 12
_NUMBER = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\b")
_WORD = re.compile(r"[a-zA-Z]+")

# Numbers that appear as part of a date/time and are not quantities.
_DATE_LIKE = re.compile(
    r"\b(?:\d{4}|\d{1,2}:\d{2}(?::\d{2})?|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec))\b",
    re.IGNORECASE,
)


def _canonical_measure(word: str) -> str | None:
    return _MEASURE_ALIASES.get(word.casefold())


@dataclass(frozen=True)
class ExtractedFact:
    fact_type: str
    fact_key: str
    statement: str
    value_numeric: float | None = None
    value_text: str | None = None


def _extract_numbers(text: str) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []
    for match in _NUMBER.finditer(text):
        raw = match.group(1)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue

        # Skip numbers that are really calendar/time references.
        window = text[max(0, match.start() - 4) : match.end() + 4]
        if _DATE_LIKE.search(window):
            continue

        # Look for a measure word just before or after the number.
        before = _WORD.findall(text[max(0, match.start() - 20) : match.start()])
        after = _WORD.findall(text[match.end() : match.end() + 20])
        measure = None
        for word in reversed(before):
            measure = _canonical_measure(word)
            if measure:
                break
        if measure is None:
            for word in after:
                measure = _canonical_measure(word)
                if measure:
                    break
        if measure is None:
            # A bare number carries no verifiable claim; skip it.
            continue

        display = f"{int(value)}" if value.is_integer() else f"{value}"
        facts.append(
            ExtractedFact(
                fact_type="number",
                fact_key=f"number:{measure}:{display}",
                statement=f"{measure} reported as {display}",
                value_numeric=value,
                value_text=measure,
            )
        )
    return facts


# --- Dates ---------------------------------------------------------------------

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
# "6 October 2025", "October 6, 2025", "6 Oct 2025"
_DATE_TEXT = re.compile(
    r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DATE_TEXT_US = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)


def _extract_dates(text: str) -> list[ExtractedFact]:
    facts: list[ExtractedFact] = []
    for match in _DATE_TEXT.finditer(text):
        day, month_name, year = match.group(1), match.group(2), match.group(3)
        facts.append(_date_fact(year, _MONTHS[month_name.casefold()], day))
    for match in _DATE_TEXT_US.finditer(text):
        month_name, day, year = match.group(1), match.group(2), match.group(3)
        facts.append(_date_fact(year, _MONTHS[month_name.casefold()], day))
    return facts


def _date_fact(year: str, month: int, day: str) -> ExtractedFact:
    iso = f"{int(year):04d}-{month:02d}-{int(day):02d}"
    return ExtractedFact(
        fact_type="date",
        fact_key=f"date:{iso}",
        statement=f"date referenced: {iso}",
        value_text=iso,
    )


# --- Named entities ------------------------------------------------------------

# Sequences of capitalized words, excluding sentence-initial noise is hard, so we
# require either two capitalized words or a known entity suffix.
_ENTITY = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+)\b")
_ENTITY_SUFFIX = re.compile(
    r"\b([A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)*\s+"
    r"(?:Agency|Ministry|Government|University|Organization|Organisation|Committee|"
    r"Association|Police|Army|Navy|Council|Department|Authority|Bank|Party|Union|"
    r"Court|Institute|Foundation))\b"
)


def _extract_entities(text: str) -> list[ExtractedFact]:
    names: set[str] = set()
    for pattern in (_ENTITY, _ENTITY_SUFFIX):
        for match in pattern.finditer(text):
            name = " ".join(match.group(1).split())
            if len(name) >= 5:
                names.add(name)
    return [
        ExtractedFact(
            fact_type="entity",
            fact_key=f"entity:{name.casefold()}",
            statement=f"entity mentioned: {name}",
            value_text=name,
        )
        for name in sorted(names)
    ]


def extract_facts(title: str, description: str = "") -> list[ExtractedFact]:
    """Extract the deterministic facts from one report.

    Duplicate keys within a single report are collapsed so a fact is stored once
    per report, however many times the text repeats it.
    """
    text = f"{title}. {description}".strip()
    facts: list[ExtractedFact] = []
    facts.extend(_extract_numbers(text))
    facts.extend(_extract_dates(text))
    facts.extend(_extract_entities(text))

    event_type = classify_event_type(title, description)
    if event_type:
        facts.append(
            ExtractedFact(
                fact_type="event_type",
                fact_key=f"event_type:{event_type}",
                statement=f"event type: {event_type}",
                value_text=event_type,
            )
        )

    unique: dict[str, ExtractedFact] = {}
    for fact in facts:
        unique.setdefault(fact.fact_key, fact)
    return list(unique.values())


def extract_event_type(title: str, description: str = "") -> str | None:
    return classify_event_type(title, description)


def now_utc() -> datetime:
    return datetime.now(UTC)
