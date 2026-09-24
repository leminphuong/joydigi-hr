import datetime
import sys
from datetime import timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.utils import timezone

from base.backends import logger


def auto_punch_out():
    from attendance.methods.utils import Request
    from attendance.models import Attendance, AttendanceActivity
    from attendance.views.clock_in_out import perform_clock_out
    from base.models import EmployeeShiftSchedule

    automatic_check_out_shifts = EmployeeShiftSchedule.objects.filter(
        is_auto_punch_out_enabled=True
    )

    for shift_schedule in automatic_check_out_shifts:
        activities = AttendanceActivity.objects.filter(
            shift_day=shift_schedule.day,
            clock_out_date=None,
            clock_out=None,
        ).order_by("-created_at")

        for activity in activities:
            attendance = Attendance.objects.filter(
                employee_id=activity.employee_id,
                attendance_clock_out=None,
                attendance_clock_out_date=None,
                shift_id=shift_schedule.shift_id,
                attendance_day=shift_schedule.day,
                attendance_date=activity.attendance_date,
            ).first()

            if attendance:
                date = activity.attendance_date
                if (
                    shift_schedule.is_night_shift
                    and shift_schedule.start_time
                    and shift_schedule.end_time
                    and shift_schedule.start_time > shift_schedule.end_time
                ):
                    date += timedelta(days=1)

                combined_datetime = timezone.make_aware(
                    datetime.datetime.combine(date, shift_schedule.auto_punch_out_time)
                )

                if combined_datetime < timezone.now():
                    try:
                        # Phase ATTENDANCE-CHECKOUT-FINAL-WORKTIME-2:
                        # calls `perform_clock_out` rather than the
                        # `clock_out` view. Same shared business logic,
                        # minus the view's closing `render()` — which
                        # needs a real HttpRequest and so raised on this
                        # lightweight shim *after* the check-out had
                        # already been written, turning every successful
                        # auto-punch-out into a logged "error".
                        #
                        # This selects only rows with no clock-out yet
                        # (see the queryset above), so it always takes the
                        # check-out #1 path and leaves `checkout_count` at
                        # 1 — the employee whose day it closed still has
                        # their one manual correction to fix the assumed
                        # time. It never consumes that correction.
                        _attendance, allowed, reason = perform_clock_out(
                            Request(
                                user=attendance.employee_id.employee_user_id,
                                date=date,
                                time=shift_schedule.auto_punch_out_time,
                                datetime=combined_datetime,
                                # Genuinely trusted: an internal scheduled
                                # job, not user-facing input.
                                trusted_device=True,
                                # Exempt from the 30-minute minimum only:
                                # a late check-in must not leave the row
                                # open forever.
                                system_checkout=True,
                            )
                        )
                        if not allowed:
                            logger.error(
                                "auto_punch_out rejected for attendance %s: %s",
                                attendance.pk,
                                (reason or {}).get("code"),
                            )
                    except Exception as e:
                        logger.error(f"auto_punch_out error: {e}")


def attendance_reminders():
    """Remind people to check in, before and just after their shift starts.

    Phase NOTIFICATION B2, and a separate job from `end_of_day_checkout`
    on purpose: that one starts from attendance rows that are open, this
    one starts from the shift schedule and looks for people who have no
    row at all. Two different questions, so two different passes — and
    switching one off never silently disables the other.

    The implementation is imported inside the function, not at module
    level: this module's import is what registers every job, in every
    gunicorn worker, and paying for the reminder module (or touching the
    database) there is how a job registration turns into a slow boot.

    Read-only with respect to attendance. Writes notifications only.
    """
    from attendance.methods.reminders import process_check_in_reminders

    try:
        process_check_in_reminders()
    except Exception as error:
        logger.error(f"attendance_reminders error: {error}")


def forgotten_session_finalization():
    """Close day shifts nobody remembered to close.

    Phase FIX A.1, and deliberately a separate job from `auto_punch_out`
    even though the two share `perform_clock_out` underneath. They answer
    different questions: Auto Check Out is a setting an administrator
    switches on and nominates a time for; this is a backend invariant — a
    forgotten day shift must not stay open into a later workday — and it
    uses the shift's own configured `end_time`. Keeping them apart means
    switching one off never silently disables the other.

    Only employees who actually have an expired open row are considered,
    found in one query rather than by walking every employee.
    """
    from attendance.methods.session import (
        finalization_cutoff,
        finalization_cutoff_state,
    )
    from attendance.models import Attendance
    from attendance.views.clock_in_out import (
        FINALIZE_ONLY_AFTER_DAY_ROLLOVER,
        finalize_forgotten_sessions,
    )
    from employee.models import Employee

    # Phase FIX A.1B: no cutoff configured means no policy period, so
    # there is nothing to reconcile and nothing to load. Checked first,
    # before any query, so a deployment that has not set the value does
    # not so much as read a historical row.
    cutoff = finalization_cutoff()
    if cutoff is None:
        # Phase FUTURE-SAFE: say so. Failing closed is right; failing
        # closed in complete silence is what let a month of forgotten
        # sessions accumulate with nothing anywhere reporting that the
        # feature was switched off. One line per pass, naming the state
        # and never the value — an unparseable setting is a string
        # somebody typed and may contain anything.
        logger.warning(
            "forgotten_session_finalization is DISABLED: cutoff %s. Set "
            "ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF to a YYYY-MM-DD "
            "date to enable automatic finalization of forgotten day "
            "shifts; until then no forgotten session will ever be closed.",
            finalization_cutoff_state(),
        )
        return

    now = timezone.localtime()
    candidates = Attendance.objects.filter(
        attendance_clock_out__isnull=True,
        attendance_clock_out_date__isnull=True,
        # Sessions from before the policy began are excluded in the
        # query itself rather than filtered out later.
        attendance_date__gte=cutoff,
    )
    if FINALIZE_ONLY_AFTER_DAY_ROLLOVER:
        candidates = candidates.filter(
            attendance_date__lt=timezone.localdate(now)
        )
    employee_ids = list(
        candidates.values_list("employee_id_id", flat=True).distinct()
    )
    if not employee_ids:
        return

    for employee in Employee.objects.filter(id__in=employee_ids).select_related(
        "employee_user_id"
    ):
        try:
            finalized, _blocked = finalize_forgotten_sessions(employee, now=now)
            if finalized:
                logger.info(
                    "forgotten_session_finalization closed %s session(s) for "
                    "employee %s",
                    len(finalized),
                    employee.pk,
                )
        except Exception as error:
            # One employee's unreasonable data must not stop the rest.
            logger.error(
                "forgotten_session_finalization error for employee %s: %s",
                employee.pk,
                error,
            )


def end_of_day_checkout():
    """
    Remind people to check out, and close the sessions they forget.

    Thin wrapper: the logic lives in `attendance.methods.end_of_day` so it
    can be tested at an arbitrary instant instead of only at whatever time
    the scheduler happens to fire.
    """
    from attendance.methods.end_of_day import process_end_of_day

    try:
        process_end_of_day()
    except Exception as error:
        logger.error(f"end_of_day_checkout error: {error}")


def create_work_record():
    from attendance.models import WorkRecords
    from employee.models import Employee

    date = datetime.date.today()
    work_records = WorkRecords.objects.filter(date=date).values_list(
        "employee_id", flat=True
    )
    employees = Employee.objects.exclude(id__in=work_records)
    records_to_create = []

    for employee in employees:
        try:
            shift_schedule = employee.get_shift_schedule()
            if shift_schedule is None:
                continue

            shift = employee.get_shift()
            record = WorkRecords(
                employee_id=employee,
                date=date,
                work_record_type="DFT",
                shift_id=shift,
                message="",
            )
            records_to_create.append(record)
        except Exception as e:
            logger.error(f"Error preparing work record for {employee}: {e}")

    if records_to_create:
        try:
            WorkRecords.objects.bulk_create(records_to_create, ignore_conflicts=True)
        except Exception as e:
            logger.error(f"Failed to bulk create work records: {e}")


if not any(
    cmd in sys.argv
    for cmd in ["makemigrations", "migrate", "compilemessages", "flush", "shell"]
):
    """
    Initializes and starts background tasks using APScheduler when the server is running.
    """
    scheduler = BackgroundScheduler(timezone=pytz.timezone(settings.TIME_ZONE))

    scheduler.add_job(
        create_work_record, "interval", minutes=30, misfire_grace_time=3600 * 3
    )
    scheduler.add_job(
        create_work_record,
        "cron",
        hour=0,
        minute=30,
        misfire_grace_time=3600 * 9,
        id="create_daily_work_record",
        replace_existing=True,
    )
    scheduler.add_job(
        auto_punch_out,
        "interval",
        minutes=5,
        misfire_grace_time=600,
        id="auto_punch_out",
        replace_existing=True,
    )
    # Phase NOTIFICATION B2 — the two start reminders. Every minute,
    # because the moments it watches are minute-exact (shift start -5m
    # and +5m) and a coarser tick would drift them. Each stage has a
    # bounded recovery window so a late run still delivers, and every
    # reminder is deduplicated through a stored notification, so a
    # repeated run costs a few queries and sends nothing twice.
    scheduler.add_job(
        attendance_reminders,
        "interval",
        minutes=1,
        misfire_grace_time=300,
        id="attendance_reminders",
        replace_existing=True,
    )
    # Phase FIX A.1 — a separate job from `auto_punch_out` on purpose; see
    # the function's docstring. Ten minutes: it only ever acts on days
    # that have already rolled over, so a tighter tick would gain nothing.
    scheduler.add_job(
        forgotten_session_finalization,
        "interval",
        minutes=10,
        misfire_grace_time=3600,
        id="forgotten_session_finalization",
        replace_existing=True,
    )
    # Phase NOTIFICATION B2 — the two end reminders, on the same
    # one-minute tick and for the same reason: effective end -5m and +5m
    # are minute-exact. The pass is cheap because it looks only at
    # sessions still open today or yesterday, and does nothing at all
    # until one of those moments is due.
    scheduler.add_job(
        end_of_day_checkout,
        "interval",
        minutes=1,
        misfire_grace_time=300,
        id="end_of_day_checkout",
        replace_existing=True,
    )

    scheduler.start()
