"""
workday_rules.py

The three clock-time rules the company applies to an ordinary working day.

Phase ATTENDANCE-WORKDAY-RULES-SAFE-IMPLEMENT-1. These are absolute times,
not offsets from whatever a shift row happens to say: arriving by 08:30 is
on time, leaving from 16:30 is a full departure, and a day that ends before
noon is worth half a day. The official shift remains 08:00-17:00 — the
allowances are the rule, and the shift data is deliberately left alone
rather than edited to fake them.

Pure functions on purpose: no database, no manager, no request, no lock.
They can be reasoned about and tested directly, which is what the check-out
path needs after a row-locking attempt took production down.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone

#: Arrivals up to and including 08:30:59 are on time; 08:31:00 is late.
LATE_FROM = time(8, 31)

#: Departures from 16:30:00 onwards are not early; 16:29:59 is.
EARLY_OUT_BEFORE = time(16, 30)

#: A day whose check-out falls before noon is worth half a day.
HALF_DAY_BEFORE = time(12, 0)

#: What a half day is worth, and what a whole one is worth.
HALF_DAY_VALUE = 0.5
FULL_DAY_VALUE = 1.0

#: How long someone must stay checked in before they may check out.
MIN_CHECKOUT_MINUTES = 30
MIN_CHECKOUT_DELTA = timedelta(minutes=MIN_CHECKOUT_MINUTES)


def _as_time(value):
    """
    A `datetime.time` from a time, a datetime, or an "HH:MM[:SS]" string.

    Attendance stores clock-in/clock-out as `TimeField`, but the same values
    arrive as datetimes from the clock-in/out path and occasionally as
    strings from older code, so every caller is accepted rather than each
    one having to convert first. Anything unrecognised yields None, and each
    rule below treats that as "no opinion" instead of guessing.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(value, fmt).time()
            except ValueError:
                continue
    return None


def is_late(check_in):
    """
    Whether an arrival counts as late.

    On time through 08:30:59 — the whole of the 08:30 minute is still
    within the allowance — and late from 08:31:00.
    """
    moment = _as_time(check_in)
    if moment is None:
        return False
    return moment >= LATE_FROM


def is_early_out(check_out):
    """
    Whether a departure counts as leaving early.

    Early before 16:30:00; from 16:30:00 onwards it is not, even though the
    shift officially ends at 17:00.
    """
    moment = _as_time(check_out)
    if moment is None:
        return False
    return moment < EARLY_OUT_BEFORE


def is_half_day(check_out):
    """Whether a day ended before noon and is therefore worth half."""
    moment = _as_time(check_out)
    if moment is None:
        return False
    return moment < HALF_DAY_BEFORE


def _comparable(moment, reference):
    """
    `moment` made safe to subtract from `reference`.

    The project runs with `USE_TZ = True`, so both sides are normally aware
    and this returns them untouched. A legacy naive value is interpreted in
    the current timezone — the same reading Django itself gives a naive
    datetime on the way into the database — rather than having its tzinfo
    stripped off the aware side, which would silently shift the comparison
    by the UTC offset. Returns None when there is nothing to compare.
    """
    if not isinstance(moment, datetime) or not isinstance(reference, datetime):
        return None
    moment_aware = timezone.is_aware(moment)
    if moment_aware == timezone.is_aware(reference):
        return moment
    if moment_aware:
        return timezone.make_naive(moment, timezone.get_current_timezone())
    return timezone.make_aware(moment, timezone.get_current_timezone())


def seconds_since_check_in(check_in, now):
    """
    Real elapsed seconds between a check-in and a later instant.

    A true duration, not a clock-face subtraction: a check-in at 08:00:30
    is 1799 seconds old at 08:30:29, so the seconds matter. None when
    either side is missing or unusable, and each caller decides what to do
    with that rather than this function inventing an answer.
    """
    moment = _comparable(check_in, now)
    if moment is None:
        return None
    return (now - moment).total_seconds()


def can_check_out_yet(check_in, now):
    """
    Whether someone who checked in at `check_in` may check out at `now`.

    They may once a full 30 minutes has elapsed — 08:00:00 to 08:29:59 is
    refused, 08:30:00 exactly is allowed. An unreadable or missing check-in
    yields True: this rule exists to stop a check-out seconds after
    arriving, not to become a new way for a check-out to fail when the
    stored data is odd.
    """
    elapsed = seconds_since_check_in(check_in, now)
    if elapsed is None:
        return True
    return elapsed >= MIN_CHECKOUT_DELTA.total_seconds()


def day_credit(check_out, otherwise=FULL_DAY_VALUE):
    """
    How much of a working day a check-out time is worth.

    Half a day when it falls before noon; `otherwise` — whatever the
    worked-time rules already decided — for anything from 12:00:00 on. This
    only ever answers the *credit* question: worked hours stay the real
    elapsed time less the unpaid lunch hour, and are never rewritten to
    match the credit.
    """
    return HALF_DAY_VALUE if is_half_day(check_out) else otherwise
