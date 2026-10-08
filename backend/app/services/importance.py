"""Importance scoring (spec sections 13-14).

Importance answers a different question from confidence: *how much does this
story matter?* The two are deliberately independent. A fully confirmed minor
event can be ``confidence=98, importance=20``; a still-developing catastrophe
can be ``confidence=82, importance=98``. The second needs more verification
before confident publication, not a lower importance score.

The score is a transparent weighted blend of the factors the spec lists. It is
never optimized for clicks, virality or social popularity — those are not
inputs at all. Everything here is derived from facts already extracted during
verification, so importance cannot be inflated by an LLM.

Scale (0-100):

* 80-100  major: mass-casualty, international, or fast-developing
* 60-79   significant: regional impact, notable casualties or scale
* 40-59   moderate: real but contained
* 0-39    minor: routine, local, low impact
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.models.enums import EventStatus

# Event types by intrinsic severity. This is the "public safety" and "severity"
# factor; it is a prior, adjusted upward by casualties and scale below.
_TYPE_SEVERITY: dict[str, float] = {
    "earthquake": 78.0,
    "tsunami": 82.0,
    "flood": 72.0,
    "wildfire": 70.0,
    "storm": 62.0,
    "disaster": 75.0,
    "conflict": 74.0,
    "war": 80.0,
    "attack": 76.0,
    "explosion": 74.0,
    "shooting": 76.0,
    "crime": 45.0,
    "protest": 48.0,
    "epidemic": 72.0,
    "disease": 60.0,
    "accident": 55.0,
    "economy": 52.0,
    "politics": 55.0,
    "election": 58.0,
    "science": 40.0,
    "health": 50.0,
    "sports": 25.0,
    "entertainment": 22.0,
    "technology": 40.0,
    "education": 35.0,
    "environment": 55.0,
}
_DEFAULT_SEVERITY = 40.0

# Weights sum to 1.0.
W_SEVERITY = 0.34
W_CASUALTIES = 0.24
W_SCALE = 0.14
W_SCOPE = 0.14
W_DEVELOPING = 0.14

# Measures that represent harm to people, and how much each counts. Casualty
# counts dominate; disruption counts less.
_HARM_MEASURES: dict[str, float] = {
    "deaths": 1.0,
    "killed": 1.0,
    "fatalities": 1.0,
    "injured": 0.55,
    "wounded": 0.55,
    "missing": 0.5,
    "displaced": 0.45,
    "affected": 0.4,
    "evacuated": 0.4,
    "homeless": 0.45,
    "infected": 0.5,
}
_SCALE_MEASURES: dict[str, float] = {
    "magnitude": 1.0,
    "richter": 1.0,
    "intensity": 0.7,
}

# Counts below this contribute nothing; the curve is logarithmic so a jump from
# 0 to 10 deaths matters far more than 100 to 110.
_HARM_FLOOR = 1.0
# A count at/above this saturates the harm term.
_HARM_CEILING = 1000.0

# Events still moving in this window count as "developing" (spec section 14).
_DEVELOPING_WINDOW_HOURS = 6.0


@dataclass(frozen=True)
class ImportanceInputs:
    event_type: str | None
    # measure -> largest value reported for that measure
    measures: dict[str, float]
    # distinct countries/cities named, for geographic scope
    location_count: int
    # distinct non-location entities named, for breadth of impact
    entity_count: int
    update_count: int
    hours_since_first_seen: float
    official_action: bool

    def __post_init__(self) -> None:
        if self.location_count < 0 or self.entity_count < 0 or self.update_count < 0:
            raise ValueError("counts must be >= 0")


def _log_scale(value: float, ceiling: float = _HARM_CEILING, floor: float = _HARM_FLOOR) -> float:
    """Map a count onto 0-100 logarithmically, saturating at ``ceiling``."""
    if value < floor:
        return 0.0
    span = math.log10(ceiling) - math.log10(floor)
    if span <= 0:
        return 100.0
    return min(100.0, 100.0 * (math.log10(value) - math.log10(floor)) / span)


def _harm_score(measures: dict[str, float]) -> float:
    """Weighted casualty/impact term. The worst measure leads, others add."""
    contributions = sorted(
        (
            _log_scale(value) * weight
            for measure, weight in _HARM_MEASURES.items()
            if (value := measures.get(measure)) is not None
        ),
        reverse=True,
    )
    if not contributions:
        return 0.0
    # Full weight for the worst measure, a third for each additional one: an
    # event with deaths and injuries is worse than deaths alone, but not double.
    total = contributions[0]
    for extra in contributions[1:]:
        total += extra / 3.0
    return min(100.0, total)


def _scale_score(measures: dict[str, float]) -> float:
    """Physical scale (earthquake magnitude, storm intensity, ...)."""
    values = [
        _log_scale(value, ceiling=10.0, floor=1.0)
        for measure, weight in _SCALE_MEASURES.items()
        if weight and (value := measures.get(measure)) is not None
    ]
    return max(values) if values else 0.0


def _scope_score(inputs: ImportanceInputs) -> float:
    """Geographic and entity breadth: international significance, reach."""
    location = min(100.0, 40.0 * inputs.location_count)
    breadth = min(100.0, 12.0 * inputs.entity_count)
    return max(location, breadth)


def _developing_score(inputs: ImportanceInputs) -> float:
    """A fast-moving story matters more now; a settled one matters less.

    Updates are only meaningful while the event is fresh, so the signal fades
    with age rather than rewarding long-lived events.
    """
    if inputs.hours_since_first_seen > _DEVELOPING_WINDOW_HOURS:
        return 0.0
    momentum = min(100.0, 25.0 * max(0, inputs.update_count - 1))
    if inputs.official_action:
        momentum = min(100.0, momentum + 25.0)
    return momentum


def score_importance(inputs: ImportanceInputs) -> float:
    """Blend the spec's importance factors into a single 0-100 score."""
    severity = _TYPE_SEVERITY.get((inputs.event_type or "").casefold(), _DEFAULT_SEVERITY)
    harm = _harm_score(inputs.measures)
    scale = _scale_score(inputs.measures)
    scope = _scope_score(inputs)
    developing = _developing_score(inputs)

    score = (
        W_SEVERITY * severity
        + W_CASUALTIES * harm
        + W_SCALE * scale
        + W_SCOPE * scope
        + W_DEVELOPING * developing
    )
    return round(max(0.0, min(100.0, score)), 2)


def importance_band(score: float) -> str:
    """Coarse label for the editorial UI."""
    if score >= 80:
        return "major"
    if score >= 60:
        return "significant"
    if score >= 40:
        return "moderate"
    return "minor"


# A pending event still moving through the pipeline; used to decide whether an
# update should trigger a re-score.
_ACTIVE_STATUSES = (
    EventStatus.DETECTED,
    EventStatus.CLUSTERING,
    EventStatus.VERIFYING,
    EventStatus.VERIFIED,
    EventStatus.PUBLISHED,
    EventStatus.UPDATING,
)


def is_active(status: EventStatus | str) -> bool:
    """Whether an event is still evolving (and so worth re-scoring)."""
    return str(status) in {s.value for s in _ACTIVE_STATUSES}
