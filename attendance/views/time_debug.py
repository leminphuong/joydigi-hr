"""Phase ATT-TIME-2H — TEMPORARY read-only attendance-time diagnostic.

Production keeps recording attendance about 1h30m early even though the
shipped default is `Asia/Ho_Chi_Minh` and the write path reads
`django_timezone.localtime()`. ATT-TIME-2G added `ATT_TIME_DEBUG` log
lines, but the operator has no SSH access and cannot read `journalctl`,
so the same evidence is surfaced in the browser here instead.

The page answers one question: **where does the 1h30m enter?**

    Django runtime timezone        -> is the process even in Vietnam?
    vs Attendance.attendance_clock_in   (naive TimeField, what everyone displays)
    vs AttendanceActivity.in_datetime   (aware DateTimeField, the true instant)

Strictly read-only: it renders values, runs no shell, and has no action
that can change data or configuration. Delete this module, its template
and its URL once the cause is identified.
"""

import os
from datetime import timezone as dt_timezone

from django.shortcuts import render
from django.utils import timezone as django_timezone

from attendance.models import Attendance, AttendanceActivity
from joydigi.decorators import login_required, permission_required

#: What the business expects. Used only to classify the status box.
EXPECTED_TIME_ZONE = "Asia/Ho_Chi_Minh"
EXPECTED_UTC_OFFSET_HOURS = 7

RECENT_LIMIT = 20


def _runtime_panel():
    """Everything about the *running process*, nothing about config files.

    `os.environ.get("TZ")` is read individually on purpose — dumping the
    environment would expose SECRET_KEY, DATABASE_URL and every other
    credential the process holds.
    """
    local_now = django_timezone.localtime()
    offset = local_now.utcoffset()

    from django.conf import settings

    return {
        "time_zone": settings.TIME_ZONE,
        "use_tz": settings.USE_TZ,
        "utc_now": django_timezone.now().isoformat(),
        "local_now": local_now.isoformat(),
        "utc_offset": offset,
        "tz_env": os.environ.get("TZ"),
        "server_date": django_timezone.localdate(),
    }


def _status(panel):
    """Classifies the runtime into the three cases the operator cares
    about. Purely descriptive — the page offers no way to change it."""
    offset = panel["utc_offset"]
    offset_hours = offset.total_seconds() / 3600 if offset else None

    if (
        panel["time_zone"] == EXPECTED_TIME_ZONE
        and offset_hours == EXPECTED_UTC_OFFSET_HOURS
    ):
        return {
            "level": "ok",
            "message": "OK — Django runtime đang dùng giờ Việt Nam",
        }
    if panel["time_zone"] == "Asia/Kolkata":
        return {
            "level": "error",
            "message": "ERROR — Django runtime đang dùng Asia/Kolkata (UTC+05:30)",
        }
    return {
        "level": "warning",
        "message": "WARNING — Runtime timezone không đúng cấu hình Việt Nam",
    }


def _local(value):
    """Converts an aware datetime to the active timezone for display.

    Never adds a fixed offset: the whole point is to show what Django
    itself thinks local time is.
    """
    if value is None:
        return None
    if django_timezone.is_aware(value):
        return django_timezone.localtime(value).isoformat()
    return value.isoformat()


def _raw(value):
    """The stored value as-is, in UTC, rendered by this view rather than
    by the template.

    Django templates silently convert aware datetimes into the active
    timezone when rendering, which would make a column labelled "raw"
    quietly show local time — precisely the kind of hidden conversion
    this page exists to rule out.
    """
    if value is None:
        return None
    if django_timezone.is_aware(value):
        return value.astimezone(dt_timezone.utc).isoformat()
    return value.isoformat()


def _activity_for(attendance):
    """The most recent `AttendanceActivity` for the same employee and
    attendance date.

    There is deliberately no guessing here about a relationship that
    does not exist: `AttendanceActivity` has **no** foreign key to
    `Attendance`. This is the exact key that
    `clock_in_attendance_and_activity` uses when it creates the two rows
    together, which is why they can be lined up — the template labels it
    as such so nobody reads it as a declared relation.
    """
    return (
        AttendanceActivity.objects.filter(
            employee_id=attendance.employee_id,
            attendance_date=attendance.attendance_date,
        )
        .order_by("-id")
        .first()
    )


@login_required
@permission_required("attendance.view_attendance")
def attendance_time_debug_view(request):
    """Renders the diagnostic. `Attendance.objects` is a
    `JoydigiCompanyManager`, so the rows listed stay inside whatever
    company scope this admin session already has — this page does not
    widen visibility."""
    panel = _runtime_panel()

    rows = []
    recent = (
        Attendance.objects.select_related("employee_id")
        .order_by("-id")[:RECENT_LIMIT]
    )
    for attendance in recent:
        activity = _activity_for(attendance)
        rows.append(
            {
                "attendance": attendance,
                "attendance_date": attendance.attendance_date.isoformat(),
                "created_at_raw": _raw(attendance.created_at),
                "created_at_local": _local(attendance.created_at),
                "activity": activity,
                "activity_in_raw": _raw(activity.in_datetime) if activity else None,
                "activity_in_local": _local(activity.in_datetime)
                if activity
                else None,
                "activity_out_raw": _raw(activity.out_datetime) if activity else None,
                "activity_out_local": _local(activity.out_datetime)
                if activity
                else None,
            }
        )

    return render(
        request,
        "attendance/time_debug/attendance_time_debug.html",
        {
            "panel": panel,
            "status": _status(panel),
            "rows": rows,
            "latest": rows[0] if rows else None,
            "recent_limit": RECENT_LIMIT,
        },
    )
