"""Phase ATT-TIME-3A — READ-ONLY audit of historical attendance times.

While production ran `Asia/Kolkata` (UTC+05:30) instead of
`Asia/Ho_Chi_Minh` (UTC+07:00), every server-stamped attendance
wall-clock was written 1h30m early. ATT-TIME-2I fixed the cause; this
module works out which historical rows are wrong and what they *should*
say — without touching anything.

### Why reconstruction is possible at all
`Attendance.attendance_clock_in`/`attendance_clock_out` are naive
`TimeField`s: they store digits with no offset, so the original instant
cannot be recovered from them. `AttendanceActivity.in_datetime` and
`out_datetime` are aware `DateTimeField`s — Django stored those as real
UTC instants, which were correct all along. Re-reading them through the
now-correct timezone yields the true Vietnam wall clock.

Nothing here adds or subtracts a fixed offset. A row is *classified* as
a 90-minute error by comparison, never repaired by arithmetic.

### How the two tables line up (audited, not assumed)
`clock_in_attendance_and_activity` creates the `Attendance` row and an
`AttendanceActivity` in the same call with the same `employee_id` and
the same `attendance_date` — including for night shifts, where both get
the shifted business date while `clock_in_date` keeps the real calendar
day. Every later clock-in adds another activity for that same key, and
`Attendance.attendance_clock_in` is never overwritten (model help text:
"First Check-In Time"). So:

    expected clock-in  = earliest activity `in_datetime`
    expected clock-out = latest activity `out_datetime`
                         (model help text: "Last Check-Out Time")

One caveat is deliberately treated as unsafe rather than guessed:
`clock_out_attendance_and_activity` writes the clock-out onto
`Attendance.objects.filter(employee_id=...).order_by("-attendance_date",
"-id")[0]` — the employee's newest row — while closing an activity that
may belong to an *older* `attendance_date`. When the two disagree the
pairing is genuinely ambiguous, so such rows are reported, never
assumed.
"""

from django.utils import timezone as django_timezone

from attendance.models import Attendance, AttendanceActivity

#: Row-level pairing verdicts.
SAFE = "SAFE"
AMBIGUOUS = "AMBIGUOUS"
NO_ACTIVITY = "NO_ACTIVITY"

#: Per-field comparison verdicts.
ALREADY_CORRECT = "ALREADY_CORRECT"
EXACT_90_MIN_ERROR = "EXACT_90_MIN_ERROR"
OTHER_DIFFERENCE = "OTHER_DIFFERENCE"
MISSING_VALUE = "MISSING_VALUE"
NOT_APPLICABLE = "NOT_APPLICABLE"

#: The Kolkata/Vietnam gap, used only to *label* a difference.
KOLKATA_GAP_MINUTES = 90


def _minutes_of_day(value):
    return value.hour * 60 + value.minute


def _difference_minutes(current, expected):
    """`expected - current`, in minutes, normalised across midnight.

    Stored times carry no date, so a raw subtraction could read a 90
    minute gap as -1350. Normalising into (-720, 720] keeps a
    late-evening/early-morning pair comparable.
    """
    delta = (_minutes_of_day(expected) - _minutes_of_day(current)) % (24 * 60)
    if delta > 12 * 60:
        delta -= 24 * 60
    return delta


def _classify_field(current, expected):
    """Compares one stored time against its reconstruction."""
    if current is None and expected is None:
        return NOT_APPLICABLE, None
    if current is None or expected is None:
        return MISSING_VALUE, None

    difference = _difference_minutes(current, expected)
    if difference == 0:
        return ALREADY_CORRECT, 0
    if difference == KOLKATA_GAP_MINUTES:
        return EXACT_90_MIN_ERROR, difference
    return OTHER_DIFFERENCE, difference


def _local_time(value):
    """The Vietnam wall clock of an aware instant, to minute resolution.

    Minute resolution on purpose: the write path stores
    `strftime("%H:%M")`, so seconds were never recorded and comparing
    them would manufacture differences that do not exist.
    """
    if value is None:
        return None
    local = django_timezone.localtime(value)
    return local.time().replace(second=0, microsecond=0)


def audit_attendance(attendance, activities=None):
    """Audits one `Attendance` row. Pure: reads, never writes.

    `activities` may be passed in by a caller that already fetched them
    (see `audit_queryset`) to avoid a query per row.
    """
    if activities is None:
        activities = list(
            AttendanceActivity.objects.filter(
                employee_id=attendance.employee_id,
                attendance_date=attendance.attendance_date,
            )
        )

    result = {
        "attendance_id": attendance.id,
        "employee_id": getattr(attendance.employee_id, "id", None),
        "attendance_date": attendance.attendance_date,
        "current_clock_in": attendance.attendance_clock_in,
        "current_clock_out": attendance.attendance_clock_out,
        "expected_clock_in": None,
        "expected_clock_out": None,
        "clock_in_status": NOT_APPLICABLE,
        "clock_out_status": NOT_APPLICABLE,
        "clock_in_difference_minutes": None,
        "clock_out_difference_minutes": None,
        "match": NO_ACTIVITY,
        "notes": [],
    }

    if not activities:
        return result

    with_in = [a for a in activities if a.in_datetime is not None]
    if not with_in:
        # Rows predating the aware `in_datetime` field, or written by an
        # import: there is no trustworthy instant to rebuild from.
        result["match"] = AMBIGUOUS
        result["notes"].append("no activity carries an aware in_datetime")
        return result

    earliest = min(with_in, key=lambda a: a.in_datetime)
    result["expected_clock_in"] = _local_time(earliest.in_datetime)

    with_out = [a for a in activities if a.out_datetime is not None]
    latest_out = max(with_out, key=lambda a: a.out_datetime) if with_out else None
    result["expected_clock_out"] = (
        _local_time(latest_out.out_datetime) if latest_out else None
    )

    result["clock_in_status"], result["clock_in_difference_minutes"] = _classify_field(
        attendance.attendance_clock_in, result["expected_clock_in"]
    )
    result["clock_out_status"], result["clock_out_difference_minutes"] = (
        _classify_field(attendance.attendance_clock_out, result["expected_clock_out"])
    )

    ambiguous_reasons = []

    # Two activities starting at the very same instant make "which one is
    # first" a coin toss.
    starts = [a.in_datetime for a in with_in]
    if len(starts) != len(set(starts)):
        ambiguous_reasons.append("duplicate in_datetime among activities")

    # A stored clock-out with nothing to rebuild it from — see the module
    # docstring on where clock-outs can land.
    if attendance.attendance_clock_out is not None and latest_out is None:
        ambiguous_reasons.append("clock_out stored but no activity has out_datetime")

    # A reconstruction exists for a clock-out the row never recorded.
    if attendance.attendance_clock_out is None and latest_out is not None:
        ambiguous_reasons.append("activity has out_datetime but row has no clock_out")

    if ambiguous_reasons:
        result["match"] = AMBIGUOUS
        result["notes"].extend(ambiguous_reasons)
    else:
        result["match"] = SAFE

    return result


def audit_queryset(queryset=None, limit=None):
    """Audits many rows with two queries in total, never per row.

    Returns `(results, summary)`. Read-only throughout: this function
    performs no `save`, `update`, `bulk_update` or raw SQL.
    """
    if queryset is None:
        queryset = Attendance.objects.all().order_by("-attendance_date", "-id")
    if limit is not None:
        queryset = queryset[:limit]

    rows = list(queryset.select_related("employee_id"))

    by_key = {}
    if rows:
        activities = AttendanceActivity.objects.filter(
            employee_id__in={r.employee_id_id for r in rows},
            attendance_date__in={r.attendance_date for r in rows},
        )
        for activity in activities:
            by_key.setdefault(
                (activity.employee_id_id, activity.attendance_date), []
            ).append(activity)

    results = [
        audit_attendance(
            row, by_key.get((row.employee_id_id, row.attendance_date), [])
        )
        for row in rows
    ]
    return results, summarise(results)


def summarise(results):
    """Counts for the dry-run report."""
    summary = {
        "total": len(results),
        "safe": 0,
        "ambiguous": 0,
        "no_activity": 0,
        "already_correct": 0,
        "exact_90_min_error": 0,
        "other_difference": 0,
        "missing_value": 0,
    }
    for result in results:
        if result["match"] == SAFE:
            summary["safe"] += 1
        elif result["match"] == AMBIGUOUS:
            summary["ambiguous"] += 1
        else:
            summary["no_activity"] += 1

        statuses = {result["clock_in_status"], result["clock_out_status"]}
        statuses.discard(NOT_APPLICABLE)
        if EXACT_90_MIN_ERROR in statuses:
            summary["exact_90_min_error"] += 1
        elif OTHER_DIFFERENCE in statuses:
            summary["other_difference"] += 1
        elif MISSING_VALUE in statuses:
            summary["missing_value"] += 1
        elif statuses == {ALREADY_CORRECT}:
            summary["already_correct"] += 1

    return summary
