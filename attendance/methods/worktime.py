"""
worktime.py

Worked-duration arithmetic for the check-out path.

Phase ATTENDANCE-CHECKOUT-FINAL-WORKTIME-2. The company's working day is
08:00-12:00 and 13:00-17:00: the 12:00-13:00 lunch hour is unpaid and must
not count as worked time. Before this module, `clock_out_attendance_and_
activity()` summed each activity's raw wall-clock span, so a standard
08:30-17:30 day was recorded as 9h against an 8h `minimum_working_hour` and
produced a phantom hour of overtime every single day.

Two deliberate choices:

1. The lunch window is a fixed business constant, not configuration. There
   is no break/rest/lunch field anywhere in `EmployeeShiftSchedule` (or any
   other model) to read it from, so inventing a configurable one would mean
   a second migration this phase was not authorised to make.

2. Nothing here is called from `Attendance.save()`. `save()` runs on every
   edit of every row, including years-old attendance an HR user merely
   opens and re-saves; recomputing worked hours there would silently
   rewrite historical records (and, through `save()`'s delta accounting,
   the monthly `AttendanceOverTime` totals derived from them). Lunch
   exclusion is applied only where a new check-out is being recorded.
"""

from datetime import datetime, time, timedelta

from attendance.methods.utils import activity_datetime

# Unpaid lunch break, applied to every day the shift spans.
LUNCH_START = time(12, 0)
LUNCH_END = time(13, 0)


def lunch_overlap_seconds(start, end):
    """
    Seconds of `[start, end)` that fall inside the unpaid lunch window.

    Deliberately the *actual* overlap rather than a flat "subtract an hour
    if the span crosses midday": a 11:30-12:30 span owes 30 minutes, not
    60, and an 08:00-11:00 span owes nothing at all.

    Iterates per calendar day so a night shift spanning midnight has each
    day's lunch window subtracted, instead of only the first.
    """
    if end <= start:
        return 0

    total = 0
    day = start.date()
    last_day = end.date()
    while day <= last_day:
        window_start = datetime.combine(day, LUNCH_START)
        window_end = datetime.combine(day, LUNCH_END)
        overlap = min(end, window_end) - max(start, window_start)
        seconds = overlap.total_seconds()
        if seconds > 0:
            total += seconds
        day += timedelta(days=1)
    return int(total)


def worked_seconds(start, end):
    """
    Paid seconds between two naive datetimes: the raw span minus lunch.

    A reversed or zero-length span is 0, never negative — a negative
    contribution here would silently subtract from the day's total.
    """
    if end <= start:
        return 0
    raw = int((end - start).total_seconds())
    return max(0, raw - lunch_overlap_seconds(start, end))


def _time_to_seconds(value):
    return value.hour * 3600 + value.minute * 60 + value.second


def _lunch_overlap_in_day(start_secs, end_secs):
    """Lunch overlap for a window expressed as seconds-since-midnight."""
    return max(
        0,
        min(end_secs, _time_to_seconds(LUNCH_END))
        - max(start_secs, _time_to_seconds(LUNCH_START)),
    )


def overtime_request_seconds(start_time, end_time):
    """
    Paid seconds for one approved overtime window, lunch excluded.

    Phase ATTENDANCE-WEEKEND-OT-REQUEST-IMPLEMENT-1. Takes two
    `datetime.time` values on a single day (`OvertimeRequest` is
    explicitly scoped to one calendar day) and applies the same unpaid
    12:00-13:00 rule the attendance path uses — so a request written as
    09:00-17:00 is worth 7h, not 8h, exactly like a worked day of the
    same span.

    Deliberately independent of `perform_clock_in`/`perform_clock_out`:
    an approved request is a granted permission, not a record of someone
    physically arriving, and must never manufacture an
    `AttendanceActivity`.
    """
    start = _time_to_seconds(start_time)
    end = _time_to_seconds(end_time)
    if end <= start:
        return 0
    return max(0, (end - start) - _lunch_overlap_in_day(start, end))


def merge_time_windows(windows):
    """
    Overlapping/touching `(start_time, end_time)` pairs merged into the
    fewest disjoint spans, as `(start_secs, end_secs)` sorted ascending.

    The day's total is derived from *all* of an employee's approved
    requests, so two requests covering the same hour must contribute
    that hour once. Creation-time validation already rejects overlaps
    (see `OvertimeRequestSerializer`), but data written before that
    validation existed — or through the ORM, an import, or the admin —
    can still overlap. Merging first makes the total correct by
    construction rather than trusting the input.
    """
    spans = sorted(
        (_time_to_seconds(start), _time_to_seconds(end))
        for start, end in windows
        if _time_to_seconds(end) > _time_to_seconds(start)
    )
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def approved_overtime_seconds(windows):
    """
    One day's authoritative approved-overtime seconds.

    Derived from the full set of that day's approved windows every time
    it is asked for — never accumulated onto a previous value. That is
    what makes approving the same request twice a no-op, and what makes
    a cancellation fall out of the total on its own.
    """
    return sum(
        max(0, (end - start) - _lunch_overlap_in_day(start, end))
        for start, end in merge_time_windows(windows)
    )


def activities_worked_seconds(activities):
    """
    Total paid seconds across a day's `AttendanceActivity` rows.

    Still-open activities (no clock-out yet) contribute nothing rather
    than being counted up to "now" — this runs while recording a
    check-out, where every span that counts has both ends recorded.
    """
    total = 0
    for activity in activities:
        if activity.clock_out is None or activity.clock_out_date is None:
            continue
        if activity.clock_in is None or activity.clock_in_date is None:
            continue
        start, end = activity_datetime(activity)
        total += worked_seconds(start, end)
    return total
