"""Phase NOTIFICATION B — reminding people to check *in*.

`end_of_day.py` reminds whoever already has an open session that they
should close it. This is the other half, and it is a harder question,
because it is about people who have done nothing yet: there is no
attendance row to scan. The list has to be built from the shift schedule
instead — who is meant to be working today, and at what time — and only
then checked against attendance.

Two reminders, both measured from the shift's own configured start:

    start - 10m   "Sắp đến giờ chấm công"
    start         "Bạn chưa chấm công vào"   (only if they still have not)

Nothing here is a fixed clock time. An 08:00-17:00 shift gets 07:50 and
08:00; a 14:00-22:00 shift gets 13:50 and 14:00; a 22:00-06:00 night
shift gets 21:50 and 22:00. The times come from
`EmployeeShiftSchedule.start_time` for the weekday in question, and a day
with no schedule is not a working day and gets nothing.

## The recovery window

The second reminder is due from the shift start until thirty minutes
after it. That window exists so a scheduler tick that was missed — a
restart, a slow minute — can still deliver it, **not** so the reminder
repeats every minute. Sending is gated on a stored notification, so the
window can be wide and the reminder still goes out exactly once.

## What counts as "already checked in"

`attendance.methods.session` decides, because it is the same layer
check-in, check-out and `my-attendance` consult. Asking a different
question here is how four screens came to disagree in the first place.

The distinction that matters: a day shift somebody forgot to close
*yesterday* must not be read as "they are at work today". The canonical
resolver already separates that (`STALE_PREVIOUS`) from a night shift
legitimately still running (`NIGHT_SHIFT_OPEN`), so the first still gets
reminded to check in and the second does not.

## Read-only

This module reads attendance and writes notifications. It never creates,
closes or edits an `Attendance` or an `AttendanceActivity`, never calls
check-in, check-out, forgotten-session finalization or Auto Check Out,
and takes no row lock. A half-written historical row is left exactly as
it is.
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

#: How long before the shift starts the first reminder goes out.
PRE_START_LEAD = timedelta(minutes=10)

#: How long after the start the missing-check-in reminder stays due. A
#: window, not a repeat: the stored notification is what stops it being
#: sent twice, so this only has to be wide enough to survive a missed
#: tick.
MISSING_CHECK_IN_WINDOW = timedelta(minutes=30)

#: Stage names, also the marker stored on the notification.
STAGE_PRE_START = "before_start"
STAGE_MISSING_CHECK_IN = "missing_check_in"

#: Session states that mean "this person has not started their day".
#: `STALE_PREVIOUS` belongs here: yesterday's forgotten day shift is not
#: today's attendance, and reading it as such is exactly the bug FIX A
#: was written to end.
NEEDS_CHECK_IN_STATES = frozenset({NO_SESSION, STALE_PREVIOUS})

PUSH_COPY = {
    STAGE_PRE_START: (
        "Sắp đến giờ chấm công",
        "Còn 10 phút nữa đến giờ làm việc. Đừng quên chấm công vào.",
    ),
    STAGE_MISSING_CHECK_IN: (
        "Bạn chưa chấm công vào",
        "Đã đến giờ làm việc mà bạn chưa chấm công vào.",
    ),
}

IN_APP_COPY = {
    STAGE_PRE_START: "Còn 10 phút nữa đến giờ làm việc. Đừng quên chấm công vào.",
    STAGE_MISSING_CHECK_IN: "Đã đến giờ làm việc mà bạn chưa chấm công vào.",
}


def shift_start_datetime(work_date, start_time):
    """`start_time` on `work_date`, as an aware instant in local time."""
    if work_date is None or start_time is None:
        return None
    return timezone.make_aware(
        datetime.combine(work_date, start_time), timezone.get_current_timezone()
    )


def stage_due(now, start_at):
    """Which reminder `now` has reached for a shift starting `start_at`.

    The later one wins: a run at 08:05 owes the missing-check-in
    reminder, not the one that was due at 07:50.
    """
    if now is None or start_at is None:
        return None
    if start_at <= now < start_at + MISSING_CHECK_IN_WINDOW:
        return STAGE_MISSING_CHECK_IN
    if start_at - PRE_START_LEAD <= now < start_at:
        return STAGE_PRE_START
    return None


def marker_for(stage, work_date):
    """The stored value that makes a reminder unrepeatable.

    Carries the stage and the date, so the two reminders are independent
    of each other and tomorrow can remind again.
    """
    return f"{stage}:{work_date.isoformat()}"


def reminder_already_sent(employee, stage, work_date):
    """Whether this reminder has already gone out.

    Looked up in the database, not in process memory: production runs
    several workers, each with its own scheduler, so an in-memory guard
    would let every worker send its own copy.
    """
    from django.contrib.contenttypes.models import ContentType
    from notifications.models import Notification

    return Notification.objects.filter(
        target_content_type=ContentType.objects.get_for_model(employee),
        target_object_id=str(employee.pk),
        data__checkin_reminder=marker_for(stage, work_date),
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
        covered = {
            day
            for day in work_dates
            if holiday.start_date <= day <= end
        }
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
    return [
        (row, set(row.company_id.values_list("pk", flat=True))) for row in rows
    ]


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


def _send_reminder(employee, stage, work_date, start_at):
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
        verb=IN_APP_COPY[stage],
        target=employee,
        icon="time-outline",
        checkin_reminder=marker_for(stage, work_date),
        checkin_shift_start=start_at.isoformat(),
    )
    if not _notification_created(created):
        return False

    _push_reminder(user, stage, work_date, start_at)
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
    and the loop running for everybody else.
    """
    from joydigi_api.push import send_to_user

    title, body = PUSH_COPY[stage]
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

    Today, plus tomorrow — the second only matters for a shift starting
    just after midnight, whose ten-minute warning falls on the previous
    calendar day. Two dates rather than one because the alternative is
    silently never reminding those shifts.
    """
    today = timezone.localdate(now)
    return [today, today + timedelta(days=1)]


def process_check_in_reminders(now=None):
    """One pass: remind whoever is due a check-in reminder.

    Returns a tally so the job's effect is visible in logs and assertable
    in tests. Safe to run as often as the scheduler likes — reminders are
    deduplicated through stored notifications.

    Queries are bulk: one for the schedules, one for the employees, one
    for attendance state across everybody, and one each for holidays,
    weekly off days and approved leave. The per-employee work is then
    arithmetic over maps, so the cost does not grow with headcount the
    way a query-per-employee loop would.
    """
    from employee.models import Employee

    now = now or timezone.localtime()
    tally = {"pre_start": 0, "missing_check_in": 0, "skipped": 0}

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
    # check-out and `my-attendance`.
    needing = {
        work_date: needs_check_in(employee_ids, work_date)
        for work_date in work_dates
    }

    for employee in employees:
        work_info = getattr(employee, "employee_work_info", None)
        shift = getattr(work_info, "shift_id", None)
        if shift is None:
            tally["skipped"] += 1
            continue
        company = getattr(work_info, "company_id", None)
        company_id = getattr(company, "pk", None)

        for work_date in work_dates:
            schedule = schedules.get(
                (shift.pk, work_date.strftime("%A").lower())
            )
            if schedule is None or schedule.start_time is None:
                # No schedule for this shift on this weekday: not a
                # working day, and nothing to remind about.
                continue

            start_at = shift_start_datetime(work_date, schedule.start_time)
            stage = stage_due(now, start_at)
            if stage is None:
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
                if _send_reminder(employee, stage, work_date, start_at):
                    key = (
                        "pre_start"
                        if stage == STAGE_PRE_START
                        else "missing_check_in"
                    )
                    tally[key] += 1
            except Exception as error:  # one employee must not stop the rest
                logger.error(
                    "check-in reminder failed for employee %s on %s: %s",
                    employee.pk,
                    work_date,
                    error,
                )

    return tally
