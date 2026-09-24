"""
end_of_day.py

Closing the working day for people who forget to check out.

Two reminders, both measured from the day's *effective* end — the shift's
end time, pushed later when the employee has approved overtime:

    effective_end - 5m   SHIFT_END_MINUS_5, their day is nearly over
    effective_end + 5m   SHIFT_END_PLUS_5, they still have not checked out

Phase NOTIFICATION B2 moved the second reminder from +10m to +5m and gave
both stages their names, offsets and bounded recovery windows from
`attendance.methods.reminders` — the one place the four reminder stages
of a working day are defined, so the start pair and the end pair cannot
drift apart.

Night shifts are reminded on their own terms: a 22:00-06:00 session is
reminded at 05:55 and 06:05 the following morning, because the end
instant comes from `session.session_end_datetime`, which knows the shift
crosses midnight. Approved overtime *on a night shift* is a separate
question this module deliberately does not answer — see
`effective_end_instant`.

Nothing is ever closed automatically. An employee who never checks out
keeps an open session for the rest of that day, and the next day starts a
new one that is entirely independent of it — the check-in gate is scoped
by date so yesterday's forgotten session cannot block today. Inventing a
check-out time for somebody would be worse than leaving the row honest
about what is known.

This module only reads attendance and writes notifications. It takes no
row lock: `select_for_update` on the check-out path is what took
production down, because PostgreSQL refuses FOR UPDATE alongside the
manager's DISTINCT and `Meta.ordering`'s outer join. Reminders are
deduplicated through stored notifications rather than any lock or any
process-local state, because production runs several workers.
"""

import datetime
import logging

from django.utils import timezone

from attendance.methods.reminders import (
    REMINDER_GRACE,
    REMINDER_LEAD,
    STAGE_END_MINUS_5,
    STAGE_END_PLUS_5,
    format_clock,
    marker_for,
    sent_markers,
    window_stage,
)
from attendance.methods.worktime import merge_time_windows

logger = logging.getLogger(__name__)

#: How long before the effective end the first reminder goes out, and how
#: long after it the second does. Both come from `reminders`, which owns
#: the ±5 policy for all four stages of a working day.
REMINDER_BEFORE_END = REMINDER_LEAD
SECOND_REMINDER_AFTER_END = REMINDER_GRACE

#: The two end stages, re-exported under the names this module's callers
#: and tests already use.
STAGE_FIRST_REMINDER = STAGE_END_MINUS_5
STAGE_SECOND_REMINDER = STAGE_END_PLUS_5

#: The key the end reminders store their marker under.
CHECK_OUT_MARKER_KEY = "checkout_reminder"

#: How far back a run will look for sessions to remind about. Reminders
#: are only ever about the day they belong to, so this stays deliberately
#: small: a run just after midnight may still owe a reminder for a session
#: whose approved overtime ran late, and nothing older than that matters.
MAX_RECOVERY_DAYS = 1


def effective_end_seconds(shift_end, approved_windows):
    """
    The day's effective end, as seconds from midnight.

    The shift's own end time, unless approved overtime runs later — in
    which case the end of the last approved window. Windows are merged
    first, using the same helper the monthly summary uses, so two requests
    covering the same hour cannot disagree with it. A gap between windows
    is not bridged: 17:00-18:00 plus 18:30-19:30 ends at 19:30, and the
    18:00-18:30 gap remains uncovered for overtime purposes.

    Returns None when there is no shift end and no approved overtime, i.e.
    when this module has no idea when the day is supposed to finish.
    """
    from attendance.methods.worktime import _time_to_seconds

    base = _time_to_seconds(shift_end) if shift_end else None
    merged = merge_time_windows(approved_windows or [])
    latest_ot = merged[-1][1] if merged else None

    if base is None:
        return latest_ot
    if latest_ot is None:
        return base
    return max(base, latest_ot)


def approved_overtime_windows(employee, date, cache=None):
    """Approved, uncancelled, live overtime windows for one employee and day.

    Phase NOTIFICATION B2 aligned this filter with the one the monthly
    summary already uses (`attendance/period.py`, `attendance/views/
    summary.py`): `approved=True, canceled=False, is_active=True`. The
    `is_active` term was missing here, so a soft-deleted request could
    still move a reminder while contributing nothing to anybody's hours.

    `canceled` is also how this model records a rejection —
    `OvertimeRequest.request_status()` reads it as "Rejected" — so a
    rejected request and a cancelled one are excluded by the same term.
    A request that is merely pending has `approved=False` and never
    reaches here.

    `cache` is the bulk map built by `_overtime_windows_by_key` for a
    whole pass. When it is given no query is made at all, which is what
    keeps the pass's query count independent of headcount.
    """
    from attendance.models import OvertimeRequest

    if cache is not None:
        return list(cache.get((getattr(employee, "pk", employee), date), []))

    return list(
        OvertimeRequest.objects.filter(
            employee_id=employee,
            request_date=date,
            approved=True,
            canceled=False,
            is_active=True,
        ).values_list("start_time", "end_time")
    )


def _overtime_windows_by_key(rows):
    """Every approved overtime window these sessions could use, in one query.

    Keyed `(employee_id, date)`, and only each session's own date —
    overtime is read from the date the session is filed under and nowhere
    else. See `effective_end_instant` for why a night shift's following
    day is deliberately not consulted. Same filter as
    `approved_overtime_windows`, by construction.
    """
    from attendance.models import OvertimeRequest

    if not rows:
        return {}
    employee_ids = {row.employee_id_id for row in rows}
    dates = {row.attendance_date for row in rows}

    by_key = {}
    for employee_id, date, start, end in OvertimeRequest.objects.filter(
        employee_id_id__in=employee_ids,
        request_date__in=dates,
        approved=True,
        canceled=False,
        is_active=True,
    ).values_list("employee_id_id", "request_date", "start_time", "end_time"):
        by_key.setdefault((employee_id, date), []).append((start, end))
    return by_key


def _schedules_by_key(rows):
    """`{(shift_id, day_id): schedule}` for every session, in one query."""
    from base.models import EmployeeShiftSchedule

    keys = {
        (row.shift_id_id, row.attendance_day_id)
        for row in rows
        if row.shift_id_id is not None and row.attendance_day_id is not None
    }
    if not keys:
        return {}
    return {
        (schedule.shift_id_id, schedule.day_id): schedule
        for schedule in EmployeeShiftSchedule.objects.filter(
            shift_id_id__in={shift_id for shift_id, _day in keys},
            day_id__in={day_id for _shift, day_id in keys},
        )
        if (schedule.shift_id_id, schedule.day_id) in keys
    }


def effective_end_datetime(date, end_secs):
    """
    `end_secs` on `date`, as an aware datetime in the current timezone.

    Anchored to the attendance date on purpose: this is what gets recorded
    as the check-out, so it must belong to the day being closed and never
    to the day the job happens to be running.
    """
    if end_secs is None:
        return None
    midnight = datetime.datetime.combine(date, datetime.time.min)
    return timezone.make_aware(
        midnight + datetime.timedelta(seconds=int(end_secs)),
        timezone.get_current_timezone(),
    )


def stage_due(now, effective_end):
    """
    Which end reminder `now` has reached, if any.

    Phase NOTIFICATION B2: two bounded, non-overlapping windows rather
    than "the latest one that is due, for ever". `[end-5m, end)` and
    `[end+5m, end+15m)`, defined once in `attendance.methods.reminders`
    and shared with the two start stages. Nothing is due between them and
    nothing is due after them — there is no third moment, and in
    particular no automatic close.
    """
    return window_stage(now, effective_end, STAGE_END_MINUS_5, STAGE_END_PLUS_5)


def checkout_marker(attendance, stage):
    """The `employee + work_date + stage` key for one end reminder.

    The notification already targets the attendance row, and
    `Attendance.Meta.unique_together` makes that row unique per employee
    and date — so the date in the marker is not what scopes it. It is
    there so the stored value says what it means on its own, and so the
    four stages of a day share one marker format.
    """
    return marker_for(stage, attendance.attendance_date)


def reminder_already_sent(attendance, stage):
    """
    Whether this reminder has already gone out for this attendance.

    The check is a stored notification, not process memory: production
    runs several workers, each with its own scheduler, so an in-memory
    guard would let every worker send its own copy. `notify.send` writes
    the marker onto the notification itself.
    """
    from django.contrib.contenttypes.models import ContentType
    from notifications.models import Notification

    return Notification.objects.filter(
        target_content_type=ContentType.objects.get_for_model(attendance),
        target_object_id=str(attendance.pk),
        **{f"data__{CHECK_OUT_MARKER_KEY}": checkout_marker(attendance, stage)},
    ).exists()


def open_attendances(now):
    """
    Sessions still open and recent enough for this job to act on.

    Deliberately narrow. Only rows whose own date is today or yesterday are
    considered, so a job that has been down for a week closes yesterday's
    forgotten session and leaves the rest of history exactly as it is. This
    is not a cleanup sweep over stale data, and it must never become one.
    """
    from attendance.models import Attendance

    earliest = timezone.localdate(now) - datetime.timedelta(days=MAX_RECOVERY_DAYS)
    return (
        Attendance.objects.filter(
            # Phase NOTIFICATION B: both check-out columns, which is the
            # canonical definition of open in
            # `attendance.methods.session`. This used to test the time
            # column alone, so a half-written row read as open here and
            # closed to `check_online()` — two parts of one system
            # disagreeing about who is at work. A half-written row is
            # neither, and is left for an administrator.
            attendance_clock_out__isnull=True,
            attendance_clock_out_date__isnull=True,
            attendance_date__gte=earliest,
            attendance_date__lte=timezone.localdate(now),
        )
        .select_related("employee_id", "shift_id", "attendance_day")
        .order_by("attendance_date", "id")
    )


def _still_open(attendance):
    """
    Re-read, immediately before acting.

    There is no row lock here on purpose, so the employee may check out at
    the same moment this job decides to close their session. Reading the
    row again shrinks that window to almost nothing, and the check-out path
    itself does nothing when no activity is open — so the worst case is a
    wasted query, never a second check-out or an overwritten one.
    """
    from attendance.methods.session import attendance_is_open, open_activities_for
    from attendance.models import Attendance

    fresh = (
        Attendance.objects.filter(pk=attendance.pk)
        .select_related("attendance_day", "shift_id")
        .first()
    )
    if fresh is None or attendance_is_open(fresh) is not True:
        return None
    # Phase NOTIFICATION B: scoped to this session's own date. The
    # previous version asked whether the employee had *any* open
    # activity, on any date — the same cross-date pattern FIX A removed
    # from check-out, which would let a session forgotten last week keep
    # today's reminders alive.
    if not open_activities_for(fresh.employee_id, fresh.attendance_date):
        return None
    return fresh


def push_copy(stage, effective_end):
    """
    Title and body for an end reminder, carrying the real end time.

    The wording never claims "the normal workday has ended", because on
    an overtime day the effective end is later and the time printed here
    is that later one — saying 17:00 to somebody approved until 19:00
    would be simply untrue.
    """
    when = format_clock(effective_end)
    if stage == STAGE_END_MINUS_5:
        return (
            "Nhắc chấm công ra",
            f"Ca làm việc của bạn kết thúc lúc {when}. Đừng quên chấm công ra.",
        )
    return (
        "Bạn chưa chấm công ra",
        "Ca làm việc của bạn đã kết thúc. Bạn chưa chấm công ra. "
        "Vui lòng kiểm tra chấm công.",
    )


def in_app_copy(stage, effective_end):
    """The same message as it appears inside the app."""
    return push_copy(stage, effective_end)[1]


def _send_reminder(attendance, stage, effective_end):
    """
    One reminder, at most once per attendance and stage.

    The in-app notification is written first and is what makes the
    reminder "sent": the stored row is the deduplication marker, so a
    rerun — or another worker — finds it and does nothing. The push is
    then attempted on top. Ordering them this way means a Firebase outage
    can cost somebody a push, but can never cost them the notification, or
    cause the reminder to be re-sent on the next tick.

    A user who has turned notifications off gets neither: `notify.send`
    already honours their preference and writes no row, and no push is
    attempted for a reminder that was not created.
    """
    from notifications.signals import notify

    employee = attendance.employee_id
    user = getattr(employee, "employee_user_id", None)
    if user is None:
        return False
    if reminder_already_sent(attendance, stage):
        return False

    created = notify.send(
        user,
        recipient=user,
        verb=in_app_copy(stage, effective_end),
        target=attendance,
        icon="time-outline",
        # The marker `reminder_already_sent` looks for. Stored on the
        # notification itself so a second worker, or a rerun after a
        # restart, can see that this reminder has already gone out.
        checkout_reminder=checkout_marker(attendance, stage),
        checkout_effective_end=effective_end.isoformat(),
    )
    if not _notification_created(created):
        # The recipient has notifications turned off. Nothing was written,
        # so nothing should be pushed either.
        return False

    _push_reminder(user, stage, attendance, effective_end)
    return True


def _notification_created(send_result):
    """
    Whether `notify.send` actually wrote a notification.

    The signal returns a list of `(receiver, result)` pairs; the handler's
    result is the list of notifications it created, which is empty when
    the recipient has opted out.
    """
    for _receiver, result in send_result or []:
        if result:
            return True
    return False


def _push_reminder(user, stage, attendance, effective_end=None):
    """
    Push the reminder to the user's devices, if that is possible at all.

    Deliberately best-effort and deliberately last: push is an extra
    channel, and every failure mode — no device registered, Firebase not
    configured, network down, a dead token — must leave the reminder
    itself intact and the loop running for everybody else. No token is
    ever logged.
    """
    from joydigi_api.push import send_to_user

    title, body = push_copy(stage, effective_end)
    try:
        return send_to_user(
            user,
            title,
            body,
            data={
                "type": "checkout_reminder",
                "stage": stage,
                "attendance_id": attendance.pk,
                "attendance_date": attendance.attendance_date.isoformat(),
            },
        )
    except Exception as error:
        logger.warning("push reminder failed for user %s: %s", user.pk, error)
        return None


def effective_end_instant(attendance, schedule, overtime_cache=None):
    """When this session should finish, as an aware instant.

    Phase NOTIFICATION B. The base is
    `attendance.methods.session.session_end_datetime`, which already
    knows that a shift whose `end_time` is earlier than its `start_time`
    finishes on the following calendar day — so a 22:00-06:00 night
    shift is reminded at 05:55 and 06:05 the next morning instead of
    being skipped entirely, which is what happened before. Sharing that
    helper with forgotten-session finalization means the two cannot
    disagree about when a night ends.

    Approved overtime moves the end later, exactly as it always has: the
    merged windows are read from the session's own date, and the later of
    the two instants wins. For an ordinary day shift this is
    arithmetically identical to the previous implementation, gap rule
    included — 17:00-18:00 plus 18:30-19:30 ends at 19:30.

    ## Overtime on a night shift is deliberately not handled

    A 22:00-06:00 session ends on the calendar day *after* the one it is
    filed under, while `OvertimeRequest` is explicitly a same-day window
    (the model says a cross-midnight request "is deferred to a later
    phase rather than guessed at"). So overtime continuing a night shift
    could only be recorded on the following date — and nothing in this
    codebase establishes which requests on that date belong to the night
    that just ended rather than to the day that has just begun. Inventing
    an association rule here would be inventing business behaviour, so
    this function reads overtime from the session's own date only.

    The consequence, stated plainly: a night worker with approved
    overtime past 06:00 is reminded at 05:55 and 06:05, not at the end of
    their overtime. The ordinary night-shift reminders are unaffected and
    are tested. Extending them needs a product decision about how a
    next-day request is attributed, not a guess from this module.

    `None` when there is no shift end and no approved overtime — nothing
    to measure against, so this job has no opinion about the day.
    """
    from attendance.methods.session import session_end_datetime

    base = session_end_datetime(attendance, schedule)
    candidates = [
        base,
        _overtime_end_on(
            attendance.employee_id,
            attendance.attendance_date,
            cache=overtime_cache,
        ),
    ]

    known = [moment for moment in candidates if moment is not None]
    return max(known) if known else None


def _overtime_end_on(employee, date, cache=None):
    """The latest approved overtime instant on one calendar date, or None."""
    windows = approved_overtime_windows(employee, date, cache=cache)
    merged = merge_time_windows(windows or [])
    if not merged:
        return None
    # `merge_time_windows` returns seconds from midnight, which is
    # exactly what `effective_end_datetime` anchors to the date.
    return effective_end_datetime(date, merged[-1][1])


def process_end_of_day(now=None):
    """
    One pass: remind whoever is due a reminder. Nothing is ever closed.

    Returns a small tally so the job's effect is visible in logs and
    assertable in tests. Safe to run as often as the scheduler likes —
    reminders are deduplicated through stored notifications.

    Phase NOTIFICATION B2: the schedules, the approved overtime and the
    reminders already sent are all loaded in one query each for the whole
    set of open sessions, instead of three queries per session. What
    remains per-session is the deliberate re-read in `_still_open`, and
    only for somebody actually about to be reminded — that one is a
    correctness requirement, not an accident.
    """
    from attendance.models import Attendance

    now = now or timezone.localtime()
    tally = {"first_reminder": 0, "second_reminder": 0, "skipped": 0}

    rows = list(open_attendances(now))
    if not rows:
        return tally

    schedules = _schedules_by_key(rows)
    overtime = _overtime_windows_by_key(rows)
    already = sent_markers(
        CHECK_OUT_MARKER_KEY,
        Attendance,
        [row.pk for row in rows],
        [
            marker_for(stage, row.attendance_date)
            for row in rows
            for stage in (STAGE_END_MINUS_5, STAGE_END_PLUS_5)
        ],
        since=now,
    )

    for attendance in rows:
        schedule = schedules.get(
            (attendance.shift_id_id, attendance.attendance_day_id)
        )
        effective_end = effective_end_instant(
            attendance, schedule, overtime_cache=overtime
        )
        if effective_end is None:
            # No shift end and no approved overtime: nothing to measure
            # against, so this job has no opinion about the day.
            tally["skipped"] += 1
            continue

        stage = stage_due(now, effective_end)
        if stage is None:
            continue

        if f"{attendance.pk}|{marker_for(stage, attendance.attendance_date)}" in already:
            continue

        fresh = _still_open(attendance)
        if fresh is None:
            # Checked out manually in the meantime — their time stands.
            continue

        try:
            if _send_reminder(fresh, stage, effective_end):
                key = (
                    "first_reminder"
                    if stage == STAGE_FIRST_REMINDER
                    else "second_reminder"
                )
                tally[key] += 1
        except Exception as error:  # one bad row must not stop the rest
            logger.error(
                "end-of-day processing failed for attendance %s: %s",
                attendance.pk,
                error,
            )

    return tally
