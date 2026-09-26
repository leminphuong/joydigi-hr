"""Phase NOTIFICATION B2 — the four attendance reminders.

Four moments in a working day, all of them measured from the employee's
own shift and none of them a fixed clock time:

    effective start - 5m   SHIFT_START_MINUS_5   "your shift is about to begin"
    effective start + 5m   SHIFT_START_PLUS_5    "you still have not checked in"
    effective end   - 5m   SHIFT_END_MINUS_5     "your shift is about to end"
    effective end   + 5m   SHIFT_END_PLUS_5      "you still have not checked out"

An 08:00-17:00 shift gets 07:55 / 08:05 / 16:55 / 17:05. A 09:00-18:00
shift gets 08:55 / 09:05 / 17:55 / 18:05. A 22:00-06:00 night shift gets
21:55 / 22:05 and then 05:55 / 06:05 *the next morning*. Nothing here
knows what 08:00 or 17:00 mean; the times come from
`EmployeeShiftSchedule` for the weekday in question, and approved
overtime moves the two end reminders later.

This module owns the vocabulary — the four stage names, the ±5 offsets
and the recovery windows — and implements the two *start* reminders.
`end_of_day.py` implements the two *end* ones and imports the vocabulary
from here, so there is exactly one definition of when a reminder is due
and what it is called.

## Why two passes and not one

The two halves ask different questions of the database. The end
reminders start from attendance rows that are open and ask when they
should have finished. The start reminders are about people who have done
nothing yet — there is no row to scan — so the list has to be built from
the shift schedule and only then checked against attendance. Keeping
them apart means switching one off never silently disables the other.

## The recovery windows

A scheduler tick can be late: a restart, a slow minute, a worker that
was busy. Every stage therefore has a bounded window rather than an
exact instant, and the windows never overlap, so a late run delivers the
reminder that is right *now* and never one that has been overtaken:

    SHIFT_START_MINUS_5   [start - 5m, start)          5 minutes
    SHIFT_START_PLUS_5    [start + 5m, start + 15m)   10 minutes
    SHIFT_END_MINUS_5     [end   - 5m, end)            5 minutes
    SHIFT_END_PLUS_5      [end   + 5m, end   + 15m)   10 minutes

Between the anchor and +5m nothing is due — that gap is what makes the
pre-stage and the post-stage mutually exclusive by construction rather
than by the ordering of `if` statements. Past the end of a window the
reminder is simply not sent: a "you are about to start" message
delivered twenty minutes into the shift is worse than silence.

## What counts as "already checked in"

`attendance.methods.session` decides, because it is the same layer
check-in, check-out and `my-attendance` consult. Asking a different
question here is how four screens came to disagree in the first place.

The distinction that matters: a day shift somebody forgot to close
*yesterday* must not be read as "they are at work today". The canonical
resolver separates that (`STALE_PREVIOUS`) from a night shift
legitimately still running (`NIGHT_SHIFT_OPEN`), so the first still gets
reminded to check in and the second does not.

## Read-only

This module reads attendance and writes notifications. It never creates,
closes or edits an `Attendance` or an `AttendanceActivity`, never calls
check-in, check-out, forgotten-session finalization or Auto Check Out,
never decides whether anybody is late, and takes no row lock. A
half-written historical row is left exactly as it is.
"""

import logging
from datetime import datetime, timedelta

from django.utils import timezone

from attendance.methods.session import (
    NO_SESSION,
    STALE_PREVIOUS,
    needs_check_in,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# the vocabulary — shared with end_of_day.py
# ---------------------------------------------------------------------

#: Five minutes before the shift starts.
STAGE_START_MINUS_5 = "SHIFT_START_MINUS_5"

#: Five minutes after it, and only for somebody who still has not
#: checked in. This is a reminder and nothing else: it does not mark
#: anybody late, and the lateness rule is untouched by this module.
STAGE_START_PLUS_5 = "SHIFT_START_PLUS_5"

#: Five minutes before the day's *effective* end — the shift's end,
#: pushed later by approved overtime.
STAGE_END_MINUS_5 = "SHIFT_END_MINUS_5"

#: Five minutes after it, and only for a session that is still open.
#: Nothing is ever closed automatically.
STAGE_END_PLUS_5 = "SHIFT_END_PLUS_5"

#: How long before an anchor the "about to" reminder is due.
REMINDER_LEAD = timedelta(minutes=5)

#: How long after an anchor the "you still have not" reminder is due.
REMINDER_GRACE = timedelta(minutes=5)

#: How late a run may be and still deliver a post-anchor reminder. Wide
#: enough to survive a restart or a missed tick, short enough that the
#: message is still about now. The pre-anchor window needs no separate
#: allowance: it is already `REMINDER_LEAD` wide and ends at the anchor.
RECOVERY_WINDOW = timedelta(minutes=10)

#: Session states that mean "this person has not started their day".
#: `STALE_PREVIOUS` belongs here: yesterday's forgotten day shift is not
#: today's attendance, and reading it as such is exactly the bug FIX A
#: was written to end.
NEEDS_CHECK_IN_STATES = frozenset({NO_SESSION, STALE_PREVIOUS})


def window_stage(now, anchor, minus_stage, plus_stage):
    """Which stage `now` falls in for an event happening at `anchor`.

    The two windows are disjoint and bounded — see the module docstring.
    The later one is tested first so the code reads in the order the day
    happens, though they cannot both match.
    """
    if now is None or anchor is None:
        return None
    if anchor + REMINDER_GRACE <= now < anchor + REMINDER_GRACE + RECOVERY_WINDOW:
        return plus_stage
    if anchor - REMINDER_LEAD <= now < anchor:
        return minus_stage
    return None


def marker_for(stage, work_date):
    """The stored value that makes one reminder unrepeatable.

    Carries the stage and the work date, so the four stages are
    independent of each other and tomorrow can remind again. This is the
    `employee + work_date + stage` key, written onto the notification
    itself; see `sent_markers`.
    """
    return f"{stage}:{work_date.isoformat()}"


def format_clock(moment):
    """`HH:MM` in local time, for putting a real time in a message."""
    if moment is None:
        return ""
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    return moment.strftime("%H:%M")


#: How far back the bulk deduplication lookup reads. Markers only ever
#: belong to the day being reminded about or the one either side of it,
#: so this bounds the query without being able to miss one.
SENT_LOOKBACK = timedelta(days=2)


def sent_markers(marker_key, target_model, object_ids, markers, since=None):
    """Which of these reminders have already gone out, in one query.

    Returns a set of `"<object_id>|<marker>"` strings. Looked up in the
    database rather than in process memory, because production runs
    several workers and each has its own scheduler; an in-memory guard
    would let every worker send its own copy.

    One query for everybody, not one per employee: at 07:55 every person
    on an 08:00 shift is due at the same moment, so a per-employee check
    is precisely the query-per-employee pattern these passes exist to
    avoid.

    The marker itself is compared in Python rather than in SQL. Matching
    inside a JSON column is the kind of expression that behaves
    differently on SQLite and PostgreSQL, and this project has already
    lost production once to a query that passed on SQLite and was
    rejected by PostgreSQL. The rows are bounded by recipient and by
    `SENT_LOOKBACK`, so there are few of them, and an exact string
    comparison cannot surprise anybody.
    """
    from django.contrib.contenttypes.models import ContentType
    from notifications.models import Notification

    object_ids = [str(pk) for pk in object_ids]
    wanted = set(markers)
    if not object_ids or not wanted:
        return set()

    rows = Notification.objects.filter(
        target_content_type=ContentType.objects.get_for_model(target_model),
        target_object_id__in=object_ids,
        timestamp__gte=(since or timezone.now()) - SENT_LOOKBACK,
    ).values_list("target_object_id", "data")

    found = set()
    for object_id, data in rows:
        marker = (data or {}).get(marker_key)
        if marker in wanted:
            found.add(f"{object_id}|{marker}")
    return found


# ---------------------------------------------------------------------
# the start reminders
# ---------------------------------------------------------------------

#: The key the start reminders store their marker under.
CHECK_IN_MARKER_KEY = "checkin_reminder"


def push_copy(stage, start_at):
    """Title and body for a start reminder, with the real shift time."""
    when = format_clock(start_at)
    if stage == STAGE_START_MINUS_5:
        return (
            "Nhắc chấm công vào",
            f"Ca làm việc của bạn bắt đầu lúc {when}. Đừng quên chấm công vào.",
        )
    return (
        "Bạn chưa chấm công vào",
        f"Bạn chưa chấm công vào cho ca làm việc bắt đầu lúc {when}. "
        "Vui lòng kiểm tra chấm công.",
    )


def in_app_copy(stage, start_at):
    """The same message as it appears inside the app."""
    return push_copy(stage, start_at)[1]


def shift_start_datetime(work_date, start_time):
    """`start_time` on `work_date`, as an aware instant in local time."""
    if work_date is None or start_time is None:
        return None
    return timezone.make_aware(
        datetime.combine(work_date, start_time), timezone.get_current_timezone()
    )


def stage_due(now, start_at):
    """Which start reminder `now` has reached for a shift at `start_at`."""
    return window_stage(now, start_at, STAGE_START_MINUS_5, STAGE_START_PLUS_5)


def reminder_already_sent(employee, stage, work_date):
    """Whether this one start reminder has already gone out.

    Re-checked immediately before writing: the bulk snapshot is taken
    once at the top of the pass, and this closes the gap between that
    snapshot and the moment the notification is created.

    A single equality against the stored key, which is the form this
    project has been running in production since the end-of-day
    reminders shipped.
    """
    from django.contrib.contenttypes.models import ContentType
    from notifications.models import Notification

    return Notification.objects.filter(
        target_content_type=ContentType.objects.get_for_model(employee),
        target_object_id=str(employee.pk),
        **{f"data__{CHECK_IN_MARKER_KEY}": marker_for(stage, work_date)},
    ).exists()


# ---------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------


def _schedules_for(weekdays):
    """Every shift schedule for the given weekday names, in one query.

    Returns `{(shift_id, weekday_name): schedule}`. Bulk because the
    alternative is a query per employee, and this job runs every minute
    over everybody.
    """
    from base.models import EmployeeShiftSchedule

    rows = EmployeeShiftSchedule.objects.filter(
        day__day__in=list(weekdays)
    ).select_related("day")
    return {(row.shift_id_id, row.day.day): row for row in rows}


def _holiday_dates(work_dates):
    """Company-wide holiday dates within the window, plus specific ones.

    Mirrors `attendance.methods.utils.attendance_day_checking`: a holiday
    spans `start_date`..`end_date`, and `is_specific` ones apply only to
    the employees they name. Returns
    `(general_dates, {employee_id: {dates}})`.
    """
    from base.models import Holidays

    if not work_dates:
        return set(), {}
    earliest, latest = min(work_dates), max(work_dates)
    general, specific = set(), {}
    holidays = Holidays.objects.filter(start_date__lte=latest).prefetch_related(
        "employees"
    )
    for holiday in holidays:
        end = holiday.end_date or holiday.start_date
        if end < earliest:
            continue
        covered = {day for day in work_dates if holiday.start_date <= day <= end}
        if not covered:
            continue
        if holiday.is_specific:
            for employee_id in holiday.employees.values_list("pk", flat=True):
                specific.setdefault(employee_id, set()).update(covered)
        else:
            general.update(covered)
    return general, specific


def _company_weekly_off(work_date, company_id, company_leaves):
    """Whether `work_date` is a weekly off day for this company.

    The same comparison `attendance_day_checking` uses: numeric weekday,
    and a `based_on_week` of `None` meaning every week. Week-of-month is
    computed the same way too, so the two cannot disagree about which
    Saturday is the second one.

    This is also what keeps approved weekend overtime from turning into a
    normal working-day reminder: a Saturday that is a weekly off day gets
    no start reminder, whatever overtime has been approved on it.
    """
    week_in_month = str(((work_date.day - 1) // 7 + 1) - 1)
    for leave, companies in company_leaves:
        if companies and company_id is not None and company_id not in companies:
            continue
        if str(leave.based_on_week_day) != str(work_date.weekday()):
            continue
        if leave.based_on_week is None or str(leave.based_on_week) == week_in_month:
            return True
    return False


def _company_leaves():
    """Weekly off-day rules with their company scoping, in one query."""
    from base.models import CompanyLeaves

    rows = CompanyLeaves.objects.all().prefetch_related("company_id")
    return [(row, set(row.company_id.values_list("pk", flat=True))) for row in rows]


def _employees_on_leave(employee_ids, work_dates):
    """`{employee_id: {dates}}` covered by an approved leave request."""
    from leave.models import LeaveRequest

    if not employee_ids or not work_dates:
        return {}
    earliest, latest = min(work_dates), max(work_dates)
    on_leave = {}
    requests = LeaveRequest.objects.filter(
        employee_id_id__in=list(employee_ids),
        status="approved",
        start_date__lte=latest,
    ).values_list("employee_id_id", "start_date", "end_date")
    for employee_id, start_date, end_date in requests:
        end = end_date or start_date
        if end < earliest:
            continue
        covered = {day for day in work_dates if start_date <= day <= end}
        if covered:
            on_leave.setdefault(employee_id, set()).update(covered)
    return on_leave


# ---------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------


def record_push_status(push_tally, result):
    """Count one push outcome into a per-run tally.

    Phase NOTIFY-2. The counting lives here rather than inside
    `joydigi_api.push` on purpose: a push happens once per employee per
    reminder, and with the reminder jobs on a one-minute tick, logging at
    the point of the push would write one line per employee per minute —
    a journal nobody reads, which is only marginally better than the
    silence it replaced. Counting here and logging once per run keeps the
    same information at a volume somebody will actually look at.
    """
    if push_tally is None or not isinstance(result, dict):
        return
    status = result.get("status")
    if status:
        push_tally[status] = push_tally.get(status, 0) + 1


def log_run_summary(job, tally, push_tally):
    """One line per scheduler run, and only when something happened.

    Silence when there was nothing due is the point: these jobs run every
    minute and are idle for most of the day. A line every tick saying
    "nothing to do" would bury the one that matters.
    """
    delivered = sum(
        count for key, count in tally.items() if key != "skipped"
    )
    if not delivered and not push_tally:
        return
    pushes = ", ".join(
        f"{status}={count}" for status, count in sorted(push_tally.items())
    )
    logger.info(
        "%s: REMINDER_CREATED=%s %s%s",
        job,
        delivered,
        " ".join(f"{key}={value}" for key, value in sorted(tally.items())),
        f" push[{pushes}]" if pushes else "",
    )


def _send_reminder(employee, stage, work_date, start_at, push_tally=None):
    """One reminder, at most once per employee, stage and work date.

    The in-app notification is written first and is what makes the
    reminder "sent": the stored row is the deduplication marker, so a
    rerun — or another worker — finds it and does nothing. The push is
    attempted on top, so a Firebase outage can cost somebody a push but
    never the notification, and never causes a re-send.

    A user who has turned notifications off gets neither: `notify.send`
    honours their preference and writes no row, and nothing is pushed for
    a reminder that was not created.
    """
    from notifications.signals import notify

    user = getattr(employee, "employee_user_id", None)
    if user is None:
        return False
    if reminder_already_sent(employee, stage, work_date):
        return False

    created = notify.send(
        user,
        recipient=user,
        verb=in_app_copy(stage, start_at),
        target=employee,
        icon="time-outline",
        checkin_reminder=marker_for(stage, work_date),
        checkin_shift_start=start_at.isoformat(),
    )
    if not _notification_created(created):
        return False

    record_push_status(
        push_tally, _push_reminder(user, stage, work_date, start_at)
    )
    return True


def _notification_created(send_result):
    """Whether `notify.send` actually wrote a notification."""
    for _receiver, result in send_result or []:
        if result:
            return True
    return False


def _push_reminder(user, stage, work_date, start_at):
    """Push to the user's devices, best effort and deliberately last.

    Every failure mode — no device registered, Firebase not configured,
    network down, a dead token — must leave the reminder itself intact
    and the loop running for everybody else. No token is ever logged.
    """
    from joydigi_api.push import send_to_user

    title, body = push_copy(stage, start_at)
    try:
        return send_to_user(
            user,
            title,
            body,
            data={
                "type": "checkin_reminder",
                "stage": stage,
                "work_date": work_date.isoformat(),
                "shift_start": start_at.isoformat(),
            },
        )
    except Exception as error:
        logger.warning("push check-in reminder failed for user %s: %s", user.pk, error)
        return None


# ---------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------


def _candidate_dates(now):
    """The work dates a start reminder could currently be due for.

    Three, because the windows straddle midnight in both directions: the
    pre-start reminder for a shift beginning at 00:03 falls on the
    previous calendar day, and the recovery window for one beginning at
    23:50 runs past midnight into the next. Asking about one date only
    would silently never remind those shifts.
    """
    today = timezone.localdate(now)
    return [today - timedelta(days=1), today, today + timedelta(days=1)]


def process_check_in_reminders(now=None):
    """One pass: remind whoever is due a start reminder.

    Returns a tally so the job's effect is visible in logs and assertable
    in tests. Safe to run as often as the scheduler likes — reminders are
    deduplicated through stored notifications.

    Queries are bulk and do not grow with headcount: one for the
    schedules, one for the employees, one per candidate date for
    attendance state, one each for holidays, weekly off days and approved
    leave, and one for every reminder already sent. The per-employee work
    is then arithmetic over maps.
    """
    from employee.models import Employee

    now = now or timezone.localtime()
    tally = {"start_minus_5": 0, "start_plus_5": 0, "skipped": 0}
    push_tally = {}

    work_dates = _candidate_dates(now)
    weekdays = {day.strftime("%A").lower() for day in work_dates}
    schedules = _schedules_for(weekdays)
    if not schedules:
        return tally

    shift_ids = {shift_id for shift_id, _weekday in schedules}
    employees = list(
        Employee.objects.filter(
            is_active=True,
            employee_work_info__shift_id_id__in=shift_ids,
        ).select_related(
            "employee_user_id",
            "employee_work_info__shift_id",
            "employee_work_info__company_id",
        )
    )
    if not employees:
        return tally

    employee_ids = [employee.pk for employee in employees]
    general_holidays, specific_holidays = _holiday_dates(work_dates)
    company_leaves = _company_leaves()
    on_leave = _employees_on_leave(employee_ids, work_dates)

    # Canonical attendance state, in one query per date rather than one
    # per employee. `needs_check_in` lives in `attendance.methods.session`
    # so this job shares the definition of "checked in" with check-in,
    # check-out and `my-attendance`. It is read at the top of the pass
    # that evaluates the stage, which is what makes the +5 reminder a
    # genuine re-read of authoritative state rather than a decision taken
    # five minutes earlier and remembered.
    needing = {
        work_date: needs_check_in(employee_ids, work_date) for work_date in work_dates
    }

    # Everything already sent for these people and these dates, in one
    # query. Without it, a busy minute is one notification lookup per
    # employee.
    already = sent_markers(
        CHECK_IN_MARKER_KEY,
        Employee,
        employee_ids,
        [
            marker_for(stage, work_date)
            for work_date in work_dates
            for stage in (STAGE_START_MINUS_5, STAGE_START_PLUS_5)
        ],
        since=now,
    )

    for employee in employees:
        work_info = getattr(employee, "employee_work_info", None)
        shift = getattr(work_info, "shift_id", None)
        if shift is None:
            tally["skipped"] += 1
            continue
        company = getattr(work_info, "company_id", None)
        company_id = getattr(company, "pk", None)

        for work_date in work_dates:
            schedule = schedules.get((shift.pk, work_date.strftime("%A").lower()))
            if schedule is None or schedule.start_time is None:
                # No schedule for this shift on this weekday: not a
                # working day, and nothing to remind about.
                continue

            start_at = shift_start_datetime(work_date, schedule.start_time)
            stage = stage_due(now, start_at)
            if stage is None:
                continue

            if f"{employee.pk}|{marker_for(stage, work_date)}" in already:
                continue

            if work_date in general_holidays or work_date in specific_holidays.get(
                employee.pk, ()
            ):
                tally["skipped"] += 1
                continue
            if _company_weekly_off(work_date, company_id, company_leaves):
                tally["skipped"] += 1
                continue
            if work_date in on_leave.get(employee.pk, ()):
                tally["skipped"] += 1
                continue

            if employee.pk not in needing.get(work_date, set()):
                # Already checked in for this session — or genuinely mid
                # night shift. Either way there is nothing to remind.
                continue

            try:
                if _send_reminder(
                    employee, stage, work_date, start_at, push_tally=push_tally
                ):
                    key = (
                        "start_minus_5"
                        if stage == STAGE_START_MINUS_5
                        else "start_plus_5"
                    )
                    tally[key] += 1
            except Exception as error:  # one employee must not stop the rest
                logger.error(
                    "check-in reminder failed for employee %s on %s: %s",
                    employee.pk,
                    work_date,
                    error,
                )

    log_run_summary("attendance_reminders", tally, push_tally)
    return tally
