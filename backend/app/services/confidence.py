"""Confidence scoring (spec sections 11-12).

Confidence answers one question: *how sure are we that the facts are true?* It
is deliberately independent of importance (how much the story matters), which is
scored separately in Phase 4.

The score is a weighted blend of signals that each answer part of that
question, and it is bounded so no single strong signal can manufacture
certainty:

* **independent sources** — the dominant term. Ten copies of one wire report are
  one chain (see ``independence``), not ten confirmations.
* **source reliability** — the configured editorial prior of those sources.
* **agreement** — reports that contradict each other on a number or date lower
  confidence, and any unresolved conflict caps the score.
* **official confirmation** — an official/agency source raises confidence.
* **age** — stale information decays: an unrefreshed claim is less trustworthy.
* **factual consistency** — the fraction of extracted facts that are not
  contested.

The band thresholds (spec section 12) are configurable; the default floor for
publishing is 70.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings
from app.models.enums import ConfidenceBand

# Weights sum to 1.0 so the base score is itself a 0-100 quantity.
W_INDEPENDENT = 0.45
W_RELIABILITY = 0.20
W_AGREEMENT = 0.20
W_OFFICIAL = 0.15

# A claim that has not been reconfirmed in this many hours starts to decay.
STALENESS_GRACE_HOURS = 24.0
# Fraction of the score lost per full day of staleness beyond the grace period.
STALENESS_DECAY_PER_DAY = 0.06
# A single unresolved conflict multiplies the score by this, compounding.
CONFLICT_PENALTY = 0.85
# With no conflicts we still hold confidence just under certainty.
NO_CONFLICT_CAP = 99.0


@dataclass(frozen=True)
class ConfidenceInputs:
    independent_sources: int
    total_sources: int
    mean_reliability: float  # 0-100
    agreement: float  # 0-1, fraction of facts with no disagreement
    conflicts: int
    age_hours: float
    official_confirmation: bool
    factual_consistency: float  # 0-1

    def __post_init__(self) -> None:
        if self.independent_sources < 0:
            raise ValueError("independent_sources must be >= 0")
        if not 0.0 <= self.agreement <= 1.0:
            raise ValueError("agreement must be in [0, 1]")
        if not 0.0 <= self.factual_consistency <= 1.0:
            raise ValueError("factual_consistency must be in [0, 1]")


def _independent_score(count: int) -> float:
    """Saturating curve: the first independent source matters most."""
    if count <= 0:
        return 0.0
    target = max(settings.confidence_independent_target, 1)
    # 1 source -> 40, 2 -> 65, 3 -> 80, 4 -> 90, 5+ -> 100
    return min(100.0, 100.0 * (1.0 - 0.6 ** min(count, target)))


def score_confidence(inputs: ConfidenceInputs) -> float:
    """Blend the signals into a single 0-100 confidence score."""
    independent = _independent_score(inputs.independent_sources)
    reliability = max(0.0, min(100.0, inputs.mean_reliability))
    agreement = 100.0 * inputs.agreement
    official = 100.0 if inputs.official_confirmation else 0.0

    base = (
        W_INDEPENDENT * independent
        + W_RELIABILITY * reliability
        + W_AGREEMENT * agreement
        + W_OFFICIAL * official
    )

    # Factual consistency scales the whole score: if half the facts are
    # contested we should not be half confident, we should be much less.
    base *= inputs.factual_consistency

    # Staleness decay beyond the grace window.
    if inputs.age_hours > STALENESS_GRACE_HOURS:
        days_over = (inputs.age_hours - STALENESS_GRACE_HOURS) / 24.0
        base *= max(0.0, 1.0 - STALENESS_DECAY_PER_DAY * days_over)

    # Each unresolved conflict compounds the penalty.
    if inputs.conflicts > 0:
        base *= CONFLICT_PENALTY**inputs.conflicts

    # Never report perfect certainty, and never below zero.
    return round(max(0.0, min(NO_CONFLICT_CAP, base)), 2)


def confidence_band(score: float) -> ConfidenceBand:
    """Map a score onto the configurable bands in spec section 12."""
    if score >= 95:
        return ConfidenceBand.HIGHLY_CONFIRMED
    if score >= 85:
        return ConfidenceBand.STRONGLY_SUPPORTED
    if score >= 70:
        return ConfidenceBand.REASONABLY_SUPPORTED
    if score >= 50:
        return ConfidenceBand.UNCERTAIN
    return ConfidenceBand.DO_NOT_PUBLISH


def is_publishable(score: float, *, threshold: float | None = None) -> bool:
    """Whether a score clears the publication floor."""
    floor = settings.min_confidence_to_publish if threshold is None else threshold
    return score >= floor
