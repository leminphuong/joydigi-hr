"""Pure OKR calculations, ported from JOYOKR v1.6.45 ``JoyOKR_Calculator``.

JOYOKR computes in binary floating point and rounds with PHP's
``round($x, 2)``. Doing the same here keeps legacy numbers identical after
the import: the arithmetic runs on Python floats in the same order as the
PHP code, and :func:`round2` reproduces PHP's rounding (the result is the
decimal the float prints as, rounded half away from zero). Results come
back as two-place ``Decimal`` values ready for ``DecimalField(7, 2)``.

``okr/tests/golden/joyokr_calculator.json`` holds the PHP results these
functions are tested against.
"""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional, Sequence

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100.00")

# Health identifiers (JOYOKR ``status()``).
COMPLETED = "completed"
ON_TRACK = "on_track"
AT_RISK = "at_risk"
OFF_TRACK = "off_track"


def round2(value: float) -> Decimal:
    """PHP ``round($value, 2)`` as a ``Decimal``."""
    rounded = Decimal(repr(float(value))).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    # PHP prints -0.0 as "-0"; a progress value should never show that.
    return ZERO if rounded == 0 else rounded


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def progress(start, target, current) -> Decimal:
    """Key result progress between 0 and 100.

    When start equals target there is no range to measure: the key result
    is 100 % only if the current value sits exactly on the target.
    """
    start, target, current = float(start), float(target), float(current)
    if start == target:
        return HUNDRED if current == target else ZERO
    return round2(_clamp(((current - start) / (target - start)) * 100.0, 0.0, 100.0))


def expected_progress(
    start_date: datetime.date, end_date: datetime.date, as_of: Optional[datetime.date] = None
) -> Decimal:
    """Share of the cycle elapsed on ``as_of`` (both ends inclusive).

    JOYOKR quirk kept on purpose: the cycle's last day already counts as
    100 %, and a cycle whose end precedes its start reports 0.
    """
    if as_of is None:
        from django.utils import timezone

        as_of = timezone.localdate()
    if end_date < start_date or as_of < start_date:
        return ZERO
    if as_of >= end_date:
        return HUNDRED
    total_days = max(1, (end_date - start_date).days + 1)
    elapsed_days = (as_of - start_date).days + 1
    return round2(_clamp((elapsed_days / total_days) * 100.0, 0.0, 100.0))


def status(actual, expected, warning_threshold) -> str:
    """Schedule health: completed, on_track, at_risk or off_track.

    A gap exactly equal to the threshold is still ``at_risk``.
    """
    actual = _clamp(float(actual), 0.0, 100.0)
    expected = _clamp(float(expected), 0.0, 100.0)
    warning_threshold = _clamp(float(warning_threshold), 0.0, 100.0)
    if actual >= 100.0:
        return COMPLETED
    if actual >= expected:
        return ON_TRACK
    return OFF_TRACK if (expected - actual) > warning_threshold else AT_RISK


def weighted_progress(items: Iterable[tuple]) -> Decimal:
    """Weighted average of ``(progress, weight)`` pairs.

    Negative weights count as zero and each progress is clamped to 0..100;
    with no positive weight the result is 0.
    """
    total_weight = 0.0
    total_value = 0.0
    for item_progress, item_weight in items:
        weight = max(0.0, float(item_weight or 0))
        total_weight += weight
        total_value += _clamp(float(item_progress or 0), 0.0, 100.0) * weight
    return round2(total_value / total_weight) if total_weight > 0.0 else ZERO


def objective_projection(
    approval_status: str,
    stored_progress,
    key_results: Optional[Sequence[tuple]],
    expected,
    warning_threshold,
) -> dict:
    """Progress and health of an objective as the API shows them.

    An unapproved objective shows provisional progress from its current key
    results and never gets a health value. An approved one shows its stored
    official progress, which only approved key results feed.
    """
    is_approved = approval_status == "approved"
    value = _clamp(float(stored_progress or 0), 0.0, 100.0)
    if not is_approved and key_results is not None:
        value = float(weighted_progress(key_results))
    return {
        "progress": round2(value),
        "provisional": not is_approved,
        "health": status(value, expected, warning_threshold) if is_approved else None,
    }


def rating(score, ranges: Sequence[dict]) -> str:
    """Label of the band ``score`` (0..1) falls in; the last band includes its max."""
    score = _clamp(float(score), 0.0, 1.0)
    last = len(ranges) - 1
    for index, band in enumerate(ranges):
        minimum, maximum = float(band["min"]), float(band["max"])
        if score >= minimum and (score < maximum or (index == last and score <= maximum)):
            return str(band["label"])
    return ""


def weight_total_ok(total) -> bool:
    """JOYOKR's submit/approve check: key result weights sum to 100 within
    0.01, compared as floats. Float error makes 99.99 and 100.01 fail, so
    with two-decimal weights only an exact 100.00 passes."""
    return not abs(float(total or 0) - 100.0) > 0.01
