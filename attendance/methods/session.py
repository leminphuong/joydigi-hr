"""Phase FIX A — which attendance session a person is in, right now.

Four parts of this system used to answer that question separately, and
they answered it differently. `check_online()` swept yesterday and today
together and then decided by a rule applied afterwards; check-out picked
an open activity by one rule and an attendance row by another rule that
never consulted the first; the admin looked at a different column
entirely. Nothing tied any of those answers to a particular day.

The cost was concrete. An employee who forgot to check out on Tuesday
arrived on Wednesday with Tuesday's row still open. Their phone was told
they were checked in — from Tuesday — offered them check-out, and the
server refused it, because by Wednesday Tuesday's day shift no longer
counted. Nothing was corrupt. The day simply was not standing on its
own.

This module makes "which session" a single question with a single
answer, scoped to a date:

    TODAY_OPEN                            today's row, open
    TODAY_CLOSED                          today's row, finished
    TODAY_MALFORMED                       today's row, half-written
    LEGITIMATE_PREVIOUS_NIGHT_SHIFT_OPEN  yesterday's night shift, still running
    STALE_PREVIOUS_NORMAL_SHIFT_OPEN      yesterday's day shift, forgotten
    NO_TODAY_SESSION                      nothing

The distinction that matters is the last two. A night shift that began
at 22:00 and has not ended is genuinely the current session and must
keep working exactly as it always has. A day shift left open from
yesterday is not the current session — it is a record somebody needs to
fix — and today must proceed as though it were not there.

## What this module will not do

It never writes. A stale row stays exactly as it is: not closed, not
guessed at, not normalised. Inventing yesterday's check-out time would
put a number in the database that nobody observed, and the honest state
of "this needs a human" is more useful than a plausible fiction.

Half-written rows — one of the two check-out columns set and not the
other — are treated as *present but not resolvable*. They report as
online, so nothing offers to check in on top of them, and check-out
refuses with a controlled conflict rather than choosing which of the two
columns to believe. That keeps the row untouched and puts it in front of
an administrator, which is where it belongs.

## Dates

`timezone.localdate()`, always. The business day is Vietnamese local
time; `date.today()` follows whatever timezone the server process was
started in, and the two disagree for seven hours a day when that is UTC.
"""

import logging
from datetime import date as date_cls
from datetime import datetime, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

#: Today's row exists and is open. The ordinary working state.
TODAY_OPEN = "TODAY_OPEN"

#: Today's row exists and is finished.
TODAY_CLOSED = "TODAY_CLOSED"

#: Today's row has exactly one of its two check-out columns set. Not
#: repairable from here and not guessed at; see the module docstring.
TODAY_MALFORMED = "TODAY_MALFORMED"

#: Yesterday's night shift is still running. Genuinely current.
NIGHT_SHIFT_OPEN = "LEGITIMATE_PREVIOUS_NIGHT_SHIFT_OPEN"

#: Yesterday's day shift was never closed. Not current; today ignores it.
STALE_PREVIOUS = "STALE_PREVIOUS_NORMAL_SHIFT_OPEN"

#: Nothing at all for this day.
NO_SESSION = "NO_TODAY_SESSION"

#: States in which the employee is working right now. `TODAY_MALFORMED`
#: is included deliberately: a half-written row must not invite a second
#: check-in on top of itself, and check-out will refuse it with a
#: conflict rather than write to it.
ONLINE_STATES = frozenset({TODAY_OPEN, NIGHT_SHIFT_OPEN, TODAY_MALFORMED})

#: States that may be checked out of. `TODAY_MALFORMED` is not one: it
#: is ambiguous, and picking a column to believe would be a repair.
CHECKOUT_STATES = frozenset({TODAY_OPEN, NIGHT_SHIFT_OPEN})


class AttendanceSession:
    """One answer to "which session is this, on this date"."""

    __slots__ = ("state", "attendance", "session_date", "audit_date")

    def __init__(self, state, attendance=None, session_date=None, audit_date=None):
        self.state = state
        #: The `Attendance` row the state refers to, when there is one.
        #: For `STALE_PREVIOUS` this is yesterday's forgotten row — held
        #: so a caller can report it, never so a caller can write to it.
        self.attendance = attendance
        #: The date the session belongs to, which for a night shift is
        #: the date it began, not today.
        self.session_date = session_date
        #: The date the question was asked about.
        self.audit_date = audit_date

    @property
    def is_online(self):
        return self.state in ONLINE_STATES

    @property
    def can_check_out(self):
        return self.state in CHECKOUT_STATES

    @property
    def can_check_in(self):
        """Whether a fresh check-in is the right action.

        A stale previous day does not block it — that is the whole
        point — and neither does a finished day, because the existing
        check-in path deliberately re-opens today's row when somebody
        returns after checking out.
        """
        return self.state in {NO_SESSION, STALE_PREVIOUS, TODAY_CLOSED}

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"<AttendanceSession {self.state} "
            f"attendance={getattr(self.attendance, 'pk', None)} "
            f"session_date={self.session_date}>"
        )


def today():
    """The current business date. One source, used everywhere here."""
    return timezone.localdate()


def attendance_is_open(attendance):
    """Whether a row is open: `True`, `False`, or `None` for malformed.

    Open means both check-out columns are empty; closed means both are
    filled. Exactly one filled is neither, and saying so is the point —
    a boolean would force this function to pick a column to believe, and
    picking is how a diagnosis turns into a silent repair.
    """
    if attendance is None:
        return None
    has_time = attendance.attendance_clock_out is not None
    has_date = attendance.attendance_clock_out_date is not None
    if not has_time and not has_date:
        return True
    if has_time and has_date:
        return False
    return None


def _row_for(employee, on_date):
    """The single attendance row for this employee and date, or None.

    `Attendance.Meta.unique_together = ("employee_id", "attendance_date")`
    guarantees there is at most one, so this cannot be ambiguous.
    """
    from attendance.models import Attendance

    return (
        Attendance.objects.filter(employee_id=employee, attendance_date=on_date)
        .select_related("attendance_day", "shift_id")
        .first()
    )


def resolve_session(employee, on_date=None):
    """The employee's session as of `on_date`. Reads only.

    Resolved in an order that keeps night shifts behaving exactly as
    they did before this module existed: today's own open row is the
    session if there is one, and only when there is not does yesterday's
    night shift become the answer.
    """
    audit_date = on_date or today()
    yesterday = audit_date - timedelta(days=1)

    current = _row_for(employee, audit_date)
    openness = attendance_is_open(current)

    if current is not None and openness is True:
        return AttendanceSession(TODAY_OPEN, current, audit_date, audit_date)
    if current is not None and openness is None:
        return AttendanceSession(TODAY_MALFORMED, current, audit_date, audit_date)

    previous = _row_for(employee, yesterday)
    if previous is not None and attendance_is_open(previous) is True:
        if previous.is_night_shift():
            return AttendanceSession(NIGHT_SHIFT_OPEN, previous, yesterday, audit_date)
        if current is None:
            # A day shift somebody forgot to close. Today does not
            # belong to it, and today must not be held hostage by it.
            return AttendanceSession(STALE_PREVIOUS, previous, yesterday, audit_date)

    if current is not None:
        return AttendanceSession(TODAY_CLOSED, current, audit_date, audit_date)
    return AttendanceSession(NO_SESSION, None, None, audit_date)


def open_activities_for(employee, session_date):
    """Open activities belonging to one employee on one session date.

    The pairing that check-out used to lack. The activity closed must
    belong to the same day as the attendance row being closed, and the
    only way to guarantee that with this schema is to ask for it by
    date.
    """
    from attendance.models import AttendanceActivity

    return list(
        AttendanceActivity.objects.filter(
            employee_id=employee,
            attendance_date=session_date,
            clock_out__isnull=True,
        ).order_by("id")
    )


# ---------------------------------------------------------------------
# Phase FIX A.1 — has this session's shift actually ended?
#
# "Yesterday" is not the question. A night shift that started at 22:00
# on Wednesday is still running at 03:00 on Thursday, and a day shift
# that ended at 17:00 on Wednesday is over whatever the calendar says.
# So expiry is decided by the shift's own configured end time, resolved
# to a real instant, never by comparing dates.
#
# Nothing here writes, and nothing here invents a shift definition: the
# end time and the midnight crossing both come from the existing
# `EmployeeShiftSchedule` fields the rest of the system already uses.
# ---------------------------------------------------------------------

#: An open day-shift session whose configured end has not passed.
CURRENT_NORMAL = "CURRENT_NORMAL_SESSION"

#: An open day-shift session whose configured end has passed. Somebody
#: forgot to check out.
EXPIRED_NORMAL = "EXPIRED_NORMAL_SESSION"

#: An open night shift still inside its configured window — legitimately
#: running, even though the calendar date has moved on.
CURRENT_NIGHT = "CURRENT_LEGITIMATE_NIGHT_SHIFT_SESSION"

#: An open night shift past its configured end.
EXPIRED_NIGHT = "EXPIRED_NIGHT_SHIFT_SESSION"

#: The session cannot be reasoned about at all: its two check-out
#: columns disagree, its shift schedule is missing, its end time is
#: unset, or its activity cannot be paired one-to-one. Never finalized,
#: never repaired — reported.
MALFORMED_OR_AMBIGUOUS = "MALFORMED_OR_AMBIGUOUS_SESSION"

#: The session predates automatic finalization. Perfectly valid, and
#: still never touched by it: nobody was told the system would close
#: these days on their behalf, and some of them are the records the
#: original incident left behind. An administrator reviews them.
HISTORICAL_PROTECTED = "HISTORICAL_PROTECTED_SESSION"


def finalization_cutoff():
    """The first attendance date automatic finalization may act on.

    `None` means "act on nothing", which is both the default and the
    behaviour for a value that will not parse. That direction is chosen
    deliberately: a deployment that forgets this setting, or fat-fingers
    it, should quietly do less rather than quietly start rewriting
    history.

    Only the setting's name is logged, never its value — an unparseable
    string came from the environment and may be anything at all.
    """
    from django.conf import settings

    raw = getattr(settings, "ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF", "")
    if isinstance(raw, date_cls):
        return raw
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date_cls.fromisoformat(text)
    except ValueError:
        logger.warning(
            "ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF is not a valid "
            "YYYY-MM-DD date; automatic forgotten-session finalization "
            "stays disabled."
        )
        return None


def shift_schedule_for(attendance):
    """The schedule row governing this attendance's day, or None.

    `(shift, weekday)` is the key the rest of the system uses —
    `Attendance.is_night_shift()` resolves the same pair — so this asks
    the same question rather than a new one.
    """
    from base.models import EmployeeShiftSchedule

    if attendance.attendance_day_id is None or attendance.shift_id_id is None:
        return None
    return EmployeeShiftSchedule.objects.filter(
        shift_id=attendance.shift_id_id, day_id=attendance.attendance_day_id
    ).first()


def session_end_datetime(attendance, schedule=None):
    """When this session was configured to end, as an aware instant.

    For an ordinary day shift that is the session's own date at the
    shift's `end_time`. For a shift whose `end_time` is earlier than its
    `start_time` and which is marked as a night shift, the end falls on
    the following calendar day — the same crossing rule
    `attendance.scheduler.auto_punch_out` already applies, so the two
    cannot disagree about when a night ends.

    `None` when it cannot be established: no schedule, or no `end_time`.
    A caller must treat that as "do not touch", never as "ended long
    ago".
    """
    if schedule is None:
        schedule = shift_schedule_for(attendance)
    if schedule is None or schedule.end_time is None:
        return None

    end_date = attendance.attendance_date
    if end_date is None:
        return None
    if (
        schedule.is_night_shift
        and schedule.start_time is not None
        and schedule.start_time > schedule.end_time
    ):
        end_date = end_date + timedelta(days=1)

    naive = datetime.combine(end_date, schedule.end_time)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def classify_open_session(attendance, now=None, schedule=None):
    """What kind of open session this row is, as of `now`.

    Returns one of the five `*_NORMAL` / `*_NIGHT` /
    `MALFORMED_OR_AMBIGUOUS` constants, or `None` when the row is simply
    closed and there is no session to classify.

    Only the row is examined here. Whether its activity can be paired
    one-to-one is a separate question, asked by
    `expired_sessions_for` — kept apart so a row can be reported as
    "expired but unpairable" rather than collapsing two different
    problems into one answer.
    """
    openness = attendance_is_open(attendance)
    if openness is False:
        return None
    if openness is None:
        return MALFORMED_OR_AMBIGUOUS

    if schedule is None:
        schedule = shift_schedule_for(attendance)
    ends_at = session_end_datetime(attendance, schedule)
    if ends_at is None:
        # No usable end time: nothing may be concluded about expiry, so
        # nothing may be done to it.
        return MALFORMED_OR_AMBIGUOUS

    is_night = bool(schedule.is_night_shift)
    expired = (now or timezone.now()) > ends_at
    if is_night:
        return EXPIRED_NIGHT if expired else CURRENT_NIGHT
    return EXPIRED_NORMAL if expired else CURRENT_NORMAL


class ExpiredSession:
    """An expired session and the single activity it pairs with."""

    __slots__ = ("attendance", "activity", "kind", "ends_at")

    def __init__(self, attendance, activity, kind, ends_at):
        self.attendance = attendance
        self.activity = activity
        self.kind = kind
        self.ends_at = ends_at

    @property
    def session_date(self):
        return self.attendance.attendance_date


def expired_sessions_for(employee, now=None, before_date=None):
    """Expired day-shift sessions safe to finalize, and those that are not.

    Returns `(ready, blocked)`.

    `ready` holds sessions that are unambiguously one expired day-shift
    attendance row paired with exactly one open activity for the same
    employee and the same date. `blocked` holds `(attendance, reason)`
    for everything that is expired or unreasonable but cannot be
    finalized safely — a malformed row, a missing schedule, no open
    activity, or more than one.

    Night shifts are never returned as `ready`, even once expired: the
    business rule being implemented is about forgotten *day* shifts, and
    a night worker who stays past their configured end is the case most
    likely to be genuinely still working. They appear in `blocked` so
    they are visible rather than silently ignored.

    `before_date` restricts the search to sessions dated before it, which
    is how the caller expresses "only days that have already rolled
    over". Two queries regardless of how many sessions there are.

    Phase FIX A.1B: sessions dated before
    `ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF` are `HISTORICAL_PROTECTED`
    and never returned as `ready`, however clean they are. They predate
    the policy — nobody was told the system would close their day for
    them — and some of them are the records the original incident left
    behind. With no cutoff configured, nothing is eligible at all.
    """
    from attendance.models import Attendance, AttendanceActivity

    now = now or timezone.now()
    cutoff = finalization_cutoff()

    rows = list(
        Attendance.objects.filter(
            employee_id=employee,
            attendance_clock_out__isnull=True,
        ).select_related("attendance_day", "shift_id")
    )
    # A row with only one of the two columns filled is malformed rather
    # than open; it is fetched separately so it can be reported instead
    # of quietly missed by the filter above.
    rows += [
        row
        for row in Attendance.objects.filter(
            employee_id=employee,
            attendance_clock_out__isnull=False,
            attendance_clock_out_date__isnull=True,
        ).select_related("attendance_day", "shift_id")
        if row.pk not in {existing.pk for existing in rows}
    ]
    if before_date is not None:
        rows = [row for row in rows if row.attendance_date < before_date]
    if not rows:
        return [], []

    activities = AttendanceActivity.objects.filter(
        employee_id=employee,
        attendance_date__in=[row.attendance_date for row in rows],
        clock_out__isnull=True,
    ).order_by("id")
    open_by_date = {}
    for activity in activities:
        open_by_date.setdefault(activity.attendance_date, []).append(activity)

    ready, blocked = [], []
    for row in sorted(rows, key=lambda r: r.attendance_date):
        schedule = shift_schedule_for(row)
        kind = classify_open_session(row, now, schedule)
        if kind is None:
            continue
        if cutoff is None or row.attendance_date < cutoff:
            # Before the policy began, or no policy configured. Reported
            # so it is visible to an administrator, never acted on — and
            # checked before anything else, so no later branch can reach
            # a historical row by accident.
            blocked.append((row, HISTORICAL_PROTECTED))
            continue
        if kind == MALFORMED_OR_AMBIGUOUS:
            blocked.append((row, MALFORMED_OR_AMBIGUOUS))
            continue
        if kind in (CURRENT_NORMAL, CURRENT_NIGHT):
            continue
        if kind == EXPIRED_NIGHT:
            blocked.append((row, EXPIRED_NIGHT))
            continue

        candidates = open_by_date.get(row.attendance_date, [])
        if len(candidates) != 1:
            # No activity to close, or more than one and no way to know
            # which. Either way, not a unique pair.
            blocked.append((row, MALFORMED_OR_AMBIGUOUS))
            continue
        ready.append(
            ExpiredSession(
                row, candidates[0], kind, session_end_datetime(row, schedule)
            )
        )
    return ready, blocked


def needs_check_in(employee_ids, work_date):
    """Which of these employees have not started `work_date` yet.

    The bulk counterpart of asking `resolve_session(employee, work_date)`
    and testing for `NO_SESSION` or `STALE_PREVIOUS`. It exists so the
    check-in reminder pass can ask about everybody in one query instead of
    one query per person, while still sharing this module's single
    definition of what "checked in" means — a second definition is how
    four screens came to disagree in the first place. There is a test
    asserting the two agree, scenario by scenario.

    `STALE_PREVIOUS` counts as needing a check-in: a day shift left open
    yesterday is not today's attendance. `NIGHT_SHIFT_OPEN` does not, and
    neither does a half-written row for the day itself — somebody with one
    of those has started, whatever else is wrong with the record.

    Reads only. The sibling of `employees_online` below, built from the
    same two primitives (`attendance_is_open` and the row's own
    `is_night_shift()`) so the two cannot drift apart.
    """
    from attendance.models import Attendance

    employee_ids = list(employee_ids)
    if not employee_ids:
        return set()
    yesterday = work_date - timedelta(days=1)

    rows = list(
        Attendance.objects.filter(
            employee_id_id__in=employee_ids,
            attendance_date__gte=yesterday,
            attendance_date__lte=work_date,
        ).select_related("attendance_day", "shift_id")
    )
    by_employee = {}
    for row in rows:
        by_employee.setdefault(row.employee_id_id, {})[row.attendance_date] = row

    needing = set()
    for employee_id in employee_ids:
        dated = by_employee.get(employee_id, {})
        current = dated.get(work_date)
        if current is not None:
            # Open, closed or half-written — the day has been started.
            continue
        previous = dated.get(yesterday)
        if (
            previous is not None
            and attendance_is_open(previous) is True
            and previous.is_night_shift()
        ):
            # Legitimately still at work from last night.
            continue
        needing.add(employee_id)
    return needing


def employees_online(employee_ids, on_date=None):
    """Which of these employees are working, in one pass.

    For callers that would otherwise ask `resolve_session` per person
    and turn a list into N queries. Same rules, two queries.
    """
    from attendance.models import Attendance

    audit_date = on_date or today()
    yesterday = audit_date - timedelta(days=1)

    rows = list(
        Attendance.objects.filter(
            employee_id_id__in=list(employee_ids),
            attendance_date__gte=yesterday,
            attendance_date__lte=audit_date,
        ).select_related("attendance_day", "shift_id")
    )
    by_employee = {}
    for row in rows:
        by_employee.setdefault(row.employee_id_id, {})[row.attendance_date] = row

    online = set()
    for employee_id, dated in by_employee.items():
        current = dated.get(audit_date)
        openness = attendance_is_open(current)
        if current is not None and openness in (True, None):
            online.add(employee_id)
            continue
        previous = dated.get(yesterday)
        if (
            current is None
            and previous is not None
            and attendance_is_open(previous) is True
            and previous.is_night_shift()
        ):
            online.add(employee_id)
    return online
