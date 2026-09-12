"""
end_of_day.py

Closing the working day for people who forget to check out.

Two reminders, both measured from the day's *effective* end — the shift's
end time, pushed later when the employee has approved overtime:

    effective_end - 5m   remind them their day is nearly over
    effective_end + 10m  remind them again, they still have not checked out

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

from attendance.methods.worktime import merge_time_windows

logger = logging.getLogger(__name__)

#: How long before the effective end the first reminder goes out.
REMINDER_BEFORE_END = datetime.timedelta(minutes=5)

#: How long after it the second reminder goes out.
SECOND_REMINDER_AFTER_END = datetime.timedelta(minutes=10)

#: Stage names, also the marker stored on the notification so a rerun can
#: tell what it has already sent.
STAGE_FIRST_REMINDER = "before_end"
STAGE_SECOND_REMINDER = "after_end"

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


def approved_overtime_windows(employee, date):
    """Approved, uncancelled overtime windows for one employee and day."""
    from attendance.models import OvertimeRequest

    return list(
        OvertimeRequest.objects.filter(
            employee_id=employee,
            request_date=date,
            approved=True,
            canceled=False,
        ).values_list("start_time", "end_time")
    )


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
    Which of the three moments `now` has reached, if any.

    Only the latest one that is due: a run at 17:10 owes the second
    reminder, not both. Nothing is due after that — there is no third
    moment, and in particular no automatic close.
    """
    if effective_end is None or now is None:
        return None
    if now >= effective_end + SECOND_REMINDER_AFTER_END:
        return STAGE_SECOND_REMINDER
    if now >= effective_end - REMINDER_BEFORE_END:
        return STAGE_FIRST_REMINDER
    return None


def reminder_already_sent(attendance, stage):
    """
    Whether this reminder has already gone out for this attendance.

    The check is a stored notification, not process memory: production
    runs several workers, each with its own scheduler, so an in-memory
    guard would let every worker send its own copy. `notify.send` writes
    the marker below onto the notification itself.
    """
    from django.contrib.contenttypes.models import ContentType
    from notifications.models import Notification

    return Notification.objects.filter(
        target_content_type=ContentType.objects.get_for_model(attendance),
        target_object_id=str(attendance.pk),
        data__checkout_reminder=stage,
    ).exists()


def _shift_schedule_for(attendance):
    """The schedule row governing this attendance's day, or None."""
    from base.models import EmployeeShiftSchedule

    if not attendance.attendance_day or not attendance.shift_id:
        return None
    return EmployeeShiftSchedule.objects.filter(
        shift_id=attendance.shift_id, day=attendance.attendance_day
    ).first()


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
            attendance_clock_out__isnull=True,
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
    from attendance.models import Attendance, AttendanceActivity

    fresh = Attendance.objects.filter(pk=attendance.pk).first()
    if fresh is None or fresh.attendance_clock_out is not None:
        return None
    has_open_activity = AttendanceActivity.objects.filter(
        employee_id=fresh.employee_id, clock_out__isnull=True
    ).exists()
    return fresh if has_open_activity else None


#: What each reminder says on the phone's lock screen. The wording avoids
#: claiming the normal workday has ended, because on an overtime day the
#: effective end is later and saying otherwise would be simply untrue.
PUSH_COPY = {
    STAGE_FIRST_REMINDER: (
        "Nhắc chấm công",
        "Sắp hết giờ làm việc. Hãy nhớ chấm công ra về.",
    ),
    STAGE_SECOND_REMINDER: (
        "Bạn chưa chấm công ra về",
        "Bạn vẫn chưa chấm công ra về. Vui lòng chấm công để kết thúc ngày làm việc.",
    ),
}

#: The same message as it appears inside the app.
IN_APP_COPY = {
    STAGE_FIRST_REMINDER: "Bạn sắp hết giờ làm việc. Hãy nhớ chấm công ra về.",
    STAGE_SECOND_REMINDER: (
        "Bạn vẫn chưa chấm công ra về. "
        "Vui lòng chấm công để kết thúc ngày làm việc."
    ),
}


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
        verb=IN_APP_COPY[stage],
        target=attendance,
        icon="time-outline",
        # The marker `reminder_already_sent` looks for. Stored on the
        # notification itself so a second worker, or a rerun after a
        # restart, can see that this reminder has already gone out.
        checkout_reminder=stage,
        checkout_effective_end=effective_end.isoformat(),
    )
    if not _notification_created(created):
        # The recipient has notifications turned off. Nothing was written,
        # so nothing should be pushed either.
        return False

    _push_reminder(user, stage, attendance)
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


def _push_reminder(user, stage, attendance):
    """
    Push the reminder to the user's devices, if that is possible at all.

    Deliberately best-effort and deliberately last: push is an extra
    channel, and every failure mode — no device registered, Firebase not
    configured, network down, a dead token — must leave the reminder
    itself intact and the loop running for everybody else.
    """
    from joydigi_api.push import send_to_user

    title, body = PUSH_COPY[stage]
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


def process_end_of_day(now=None):
    """
    One pass: remind whoever is due a reminder. Nothing is ever closed.

    Returns a small tally so the job's effect is visible in logs and
    assertable in tests. Safe to run as often as the scheduler likes —
    reminders are deduplicated through stored notifications.
    """
    now = now or timezone.localtime()
    tally = {"first_reminder": 0, "second_reminder": 0, "skipped": 0}

    for attendance in open_attendances(now):
        schedule = _shift_schedule_for(attendance)
        if schedule is not None and schedule.is_night_shift:
            # A night shift's day legitimately ends after midnight, so a
            # reminder aimed at an evening deadline would fire at the wrong
            # time entirely. Working that out is a separate problem; until
            # it is, saying nothing beats reminding somebody mid-shift.
            tally["skipped"] += 1
            continue

        end_secs = effective_end_seconds(
            schedule.end_time if schedule else None,
            approved_overtime_windows(attendance.employee_id, attendance.attendance_date),
        )
        effective_end = effective_end_datetime(attendance.attendance_date, end_secs)
        if effective_end is None:
            # No shift end and no approved overtime: nothing to measure
            # against, so this job has no opinion about the day.
            tally["skipped"] += 1
            continue

        stage = stage_due(now, effective_end)
        if stage is None:
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
