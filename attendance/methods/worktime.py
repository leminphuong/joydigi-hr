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
