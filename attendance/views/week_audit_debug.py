"""Phase WEEK-AUDIT — TEMPORARY forensic view of attendance consistency.

=====================================================================
 TEMPORARY. READ-ONLY. DELETE WHEN THE WEEK HAS BEEN EXPLAINED.
=====================================================================

An employee's phone said they were checked in; the server refused their
check-out as already closed; the admin's "today" list did not show them
at all; and their monthly calendar showed yesterday open and today
empty. Four screens, four different answers, and no way to tell from
outside which of them is wrong.

Each of those screens asks a *different question of a different table
with a different date rule*, so disagreement between them is not
evidence of corruption on its own. This page puts all of the questions
side by side for every employee across a few days, so a real
inconsistency can be told apart from four correct answers to four
different questions.

Strictly read-only. It runs queries and arithmetic. It never calls
`perform_clock_in`, `perform_clock_out`, `clock_out_attendance_and_activity`,
`auto_punch_out`, `attendance_validate`, or `.save()` on anything. The
selection logic of check-out is *re-implemented* here rather than
invoked, precisely so that asking the question cannot answer it by
changing it.

What it shows, per employee per day:

* the `Attendance` row's fields, as stored;
* the `AttendanceActivity` rows, as stored;
* a classification — no attendance, open, closed, or anomalous;
* issue codes naming each specific inconsistency found;
* a simulation of `check_online()` evaluated *for that date* rather
  than for right now;
* a simulation of which rows a check-out would pick, and whether those
  two rows belong to the same day;
* whether the admin "today" query would show them, and why not if not.

Safety of what it reports: employee id, badge, display name, active
flag, company, department, shift. Never an email, phone, address, bank
detail, document, token, cookie, header or credential. Times and dates
of attendance are the subject of the audit and are shown as stored.

To remove: delete this module, its route in `attendance/urls.py`, and
`attendance/tests/test_week_audit_debug.py`.
"""

from collections import defaultdict
from datetime import date as date_cls
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from joydigi.decorators import login_required, permission_required

#: How many days one request may audit. The window is widened by a day
#: on each side internally, so this is the reported range, not the
#: inspected one.
MAX_RANGE_DAYS = 7

#: Issues that mean the stored data cannot be right, as opposed to
#: merely being unusual. These are what a repair phase would act on.
CRITICAL_ISSUES = frozenset(
    {
        "CLOCK_OUT_DATE_WITHOUT_CLOCK_OUT",
        "CLOCK_OUT_WITHOUT_CLOCK_OUT_DATE",
        "MULTIPLE_OPEN_ATTENDANCE_ROWS",
        "MULTIPLE_OPEN_ACTIVITIES",
        "ACTIVITY_ATTENDANCE_DATE_MISMATCH",
        "CHECKOUT_WOULD_SELECT_DIFFERENT_DATES",
    }
)


class AuditError(Exception):
    """A bad request, answered as a controlled 400."""


# ---------------------------------------------------------------------
# request parsing
# ---------------------------------------------------------------------


def _parse_date(raw, field):
    try:
        return date_cls.fromisoformat(raw)
    except (TypeError, ValueError):
        raise AuditError(f"{field} phải có dạng YYYY-MM-DD.")


def _resolve_range(request):
    """The reported range, defaulted to the current week so far.

    `timezone.localdate()` on purpose, never `date.today()` — the two
    disagree for seven hours a day when the server process runs UTC,
    and a diagnostic that inherits the bug it is looking for is worse
    than no diagnostic.
    """
    today = timezone.localdate()
    start_raw = request.GET.get("start_date")
    end_raw = request.GET.get("end_date")

    end = _parse_date(end_raw, "end_date") if end_raw else today
    if start_raw:
        start = _parse_date(start_raw, "start_date")
    else:
        # Monday of the week `end` falls in, so the default is
        # "this week so far".
        start = end - timedelta(days=end.weekday())

    if start > end:
        raise AuditError("start_date không được sau end_date.")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise AuditError(
            f"Khoảng ngày tối đa là {MAX_RANGE_DAYS} ngày, đã yêu cầu {span}."
        )
    return start, end


def _iter_dates(start, end):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


# ---------------------------------------------------------------------
# bulk fetch
# ---------------------------------------------------------------------


def _visible_employees(request):
    """Exactly the scope the admin already has.

    Deliberately the project's own helper, so this page can never show
    somebody an employee their normal screens would hide — and so the
    "would the admin's today list include them" answer below is the
    real one rather than an approximation of it.
    """
    from base.checkin_portal import _visible_employees as portal_scope

    return portal_scope(request)


def _employee_identity(employee):
    work_info = getattr(employee, "employee_work_info", None)
    company = getattr(work_info, "company_id", None)
    department = getattr(work_info, "department_id", None)
    shift = getattr(work_info, "shift_id", None)
    return {
        "employee_id": employee.pk,
        "badge_id": employee.badge_id or None,
        "name": employee.get_full_name(),
        "is_active": bool(employee.is_active),
        "has_work_info": work_info is not None,
        "company_id": getattr(company, "pk", None),
        "company": str(company) if company else None,
        "department": str(department) if department else None,
        "shift_id": getattr(shift, "pk", None),
        "shift": str(shift) if shift else None,
    }


def _attendance_payload(row):
    """The stored fields, named as the model names them."""
    return {
        "id": row.pk,
        "attendance_date": row.attendance_date.isoformat()
        if row.attendance_date
        else None,
        "attendance_clock_in": str(row.attendance_clock_in)
        if row.attendance_clock_in
        else None,
        "attendance_clock_in_date": row.attendance_clock_in_date.isoformat()
        if row.attendance_clock_in_date
        else None,
        "attendance_clock_out": str(row.attendance_clock_out)
        if row.attendance_clock_out
        else None,
        "attendance_clock_out_date": row.attendance_clock_out_date.isoformat()
        if row.attendance_clock_out_date
        else None,
        "attendance_validated": bool(row.attendance_validated),
        "attendance_day": str(row.attendance_day) if row.attendance_day else None,
        "attendance_worked_hour": row.attendance_worked_hour or None,
        "shift_id": row.shift_id_id,
    }


def _activity_payload(row):
    return {
        "id": row.pk,
        "attendance_date": row.attendance_date.isoformat()
        if row.attendance_date
        else None,
        "clock_in": str(row.clock_in) if row.clock_in else None,
        "clock_in_date": row.clock_in_date.isoformat() if row.clock_in_date else None,
        "clock_out": str(row.clock_out) if row.clock_out else None,
        "clock_out_date": row.clock_out_date.isoformat()
        if row.clock_out_date
        else None,
        "out_datetime": row.out_datetime.isoformat() if row.out_datetime else None,
        "shift_day": str(row.shift_day) if row.shift_day else None,
    }


# ---------------------------------------------------------------------
# simulations — re-implemented, never invoked
# ---------------------------------------------------------------------


def _night_shift_dates(attendance_rows):
    """Which of these rows are night shifts, resolved in one query pass.

    `Attendance.is_night_shift()` queries per row; over a week of every
    employee that is an N+1. The answer is a property of
    (attendance_day, shift), so it is looked up once per distinct pair.
    """
    from base.models import EmployeeShiftSchedule

    pairs = {
        (row.attendance_day_id, row.shift_id_id)
        for row in attendance_rows
        if row.attendance_day_id and row.shift_id_id
    }
    if not pairs:
        return {}
    schedules = EmployeeShiftSchedule.objects.filter(
        day_id__in={day for day, _shift in pairs},
        shift_id__in={shift for _day, shift in pairs},
    ).values_list("day_id", "shift_id", "is_night_shift")
    return {(day, shift): bool(night) for day, shift, night in schedules}


def _simulate_check_online(employee_id, audit_date, rows_by_employee, night_map):
    """DIAGNOSTIC SIMULATION of `Employee.check_online()` for a past day.

    The production method answers "right now" and cannot be asked about
    a Tuesday. This mirrors its rule — open rows dated the audit day, or
    the day before only when that row is a night shift — with the date
    supplied instead of read from the clock.

    This is *not* production behaviour and `Employee.check_online()` is
    untouched. It exists so the audit can say "on that day, the server
    would have answered X", which is the question the incident raises.
    """
    yesterday = audit_date - timedelta(days=1)
    for row in rows_by_employee.get(employee_id, []):
        if row.attendance_clock_out_date is not None:
            continue
        if row.attendance_date == audit_date:
            return True
        if row.attendance_date == yesterday and night_map.get(
            (row.attendance_day_id, row.shift_id_id), False
        ):
            return True
    return False


def _simulate_checkout_selection(employee_id, rows_by_employee, activities_by_employee):
    """Which two rows a check-out *would* pick, without picking them.

    Mirrors `clock_out_attendance_and_activity`, which chooses the
    activity and the attendance by two rules that never consult each
    other:

        activity   = open activities, order_by("attendance_date", "id"), .last()
        attendance = all rows,        order_by("-attendance_date", "-id"), [0]

    Nothing constrains those two to the same day, which is the invariant
    this simulation exists to test. The ordering here is applied in
    Python over the already-fetched rows, so no extra query runs and
    nothing is written.

    Caveat, stated because it changes how the answer should be read: the
    real function looks at *every* row the employee has, not only those
    inside the audit window. Within a window this is therefore a
    faithful simulation only when the window contains the employee's
    newest rows — which for a week-to-date audit ending today it does.
    """
    open_activities = [
        activity
        for activity in activities_by_employee.get(employee_id, [])
        if activity.clock_out is None
    ]
    activity = None
    if open_activities:
        activity = sorted(
            open_activities, key=lambda a: (a.attendance_date or date_cls.min, a.pk)
        )[-1]

    rows = rows_by_employee.get(employee_id, [])
    attendance = None
    if rows:
        attendance = sorted(
            rows, key=lambda r: (r.attendance_date or date_cls.min, r.pk)
        )[-1]

    same_date = None
    if activity is not None and attendance is not None:
        same_date = activity.attendance_date == attendance.attendance_date

    return {
        "selected_activity_id": activity.pk if activity else None,
        "selected_activity_date": activity.attendance_date.isoformat()
        if activity and activity.attendance_date
        else None,
        "selected_attendance_id": attendance.pk if attendance else None,
        "selected_attendance_date": attendance.attendance_date.isoformat()
        if attendance and attendance.attendance_date
        else None,
        "same_date": same_date,
        # Both are fetched by the same employee id, so this is asserted
        # rather than computed — it is reported so the invariant is
        # visible rather than assumed.
        "same_employee": None
        if (activity is None or attendance is None)
        else True,
        "selected_attendance_already_closed": (
            attendance.attendance_clock_out_date is not None
        )
        if attendance
        else None,
    }


def _admin_today_visibility(request, employee, identity, end_date, rows_for_day):
    """Would "Chấm công hôm nay" show this person, and if not, why.

    The admin page selects employees first and only then their
    attendance, so a hidden employee hides a perfectly good attendance
    row with them. The reasons are checked in the order that page
    applies them.
    """
    selected_company = request.session.get("selected_company")
    reasons = []

    if not identity["is_active"]:
        reasons.append("inactive")
    if not identity["has_work_info"]:
        reasons.append("missing_work_info")
    if selected_company and selected_company != "all":
        if str(identity["company_id"] or "") != str(selected_company):
            reasons.append("company_scope")
    elif identity["company_id"] is None:
        reasons.append("no_company")

    # The date the admin page itself uses — `date.today()`, process
    # timezone — rather than the timezone-aware one. Reported as found.
    admin_today = date_cls.today()

    visible_employee = not reasons
    has_row_for_end_date = bool(rows_for_day)
    admin_would_query_date = admin_today == end_date

    return {
        "admin_today_employee_visible": visible_employee,
        "admin_today_attendance_visible": visible_employee
        and has_row_for_end_date
        and admin_would_query_date,
        "exclusion_reasons": reasons,
        "admin_query_date": admin_today.isoformat(),
        "admin_query_date_matches_end_date": admin_would_query_date,
    }


# ---------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------


def _classify_day(
    audit_date,
    rows,
    activities,
    previous_rows,
    previous_activities,
    next_rows,
    auto_punch_out_time,
):
    """One day for one employee: its state and everything wrong with it."""
    issues = []

    for row in rows:
        has_time = row.attendance_clock_out is not None
        has_date = row.attendance_clock_out_date is not None
        if has_time and not has_date:
            issues.append("CLOCK_OUT_WITHOUT_CLOCK_OUT_DATE")
        if has_date and not has_time:
            issues.append("CLOCK_OUT_DATE_WITHOUT_CLOCK_OUT")

    if len(rows) > 1:
        issues.append("MULTIPLE_ATTENDANCE_ROWS_SAME_DAY")
    open_rows = [r for r in rows if r.attendance_clock_out_date is None]
    if len(open_rows) > 1:
        issues.append("MULTIPLE_OPEN_ATTENDANCE_ROWS")

    if rows and not activities:
        issues.append("ATTENDANCE_WITHOUT_ACTIVITY")
    if activities and not rows:
        issues.append("ACTIVITY_WITHOUT_ATTENDANCE")

    open_activities = [a for a in activities if a.clock_out is None]
    if len(open_activities) > 1:
        issues.append("MULTIPLE_OPEN_ACTIVITIES")

    if open_activities and rows and not open_rows:
        issues.append("OPEN_ACTIVITY_WITH_CLOSED_ATTENDANCE")
    if activities and not open_activities and open_rows:
        issues.append("CLOSED_ACTIVITY_WITH_OPEN_ATTENDANCE")

    for activity in activities:
        if activity.attendance_date != audit_date:
            issues.append("ACTIVITY_ATTENDANCE_DATE_MISMATCH")
            break

    if any(r.attendance_clock_out_date is None for r in previous_rows):
        issues.append("PREVIOUS_DAY_OPEN_ATTENDANCE")
    if any(a.clock_out is None for a in previous_activities):
        issues.append("PREVIOUS_DAY_OPEN_ACTIVITY")
    # A row on the following day while this one is still open is how a
    # check-out comes to close the wrong record: the newest row wins.
    if open_rows and next_rows:
        issues.append("NEXT_DAY_CROSS_LINK_RISK")

    if auto_punch_out_time is not None:
        for row in rows:
            if (
                row.attendance_clock_out is not None
                and row.attendance_clock_out.hour == auto_punch_out_time.hour
                and row.attendance_clock_out.minute == auto_punch_out_time.minute
            ):
                # Evidence, not proof: an employee may check out at
                # exactly the configured time by hand.
                issues.append("AUTO_PUNCH_OUT_TIME_MATCH")
                break

    if not rows and not activities:
        state = "OK_NO_ATTENDANCE"
    elif issues:
        state = "ANOMALOUS"
    elif open_rows:
        state = "OK_OPEN"
    else:
        state = "OK_CLOSED"

    # Ordered and de-duplicated, so two runs of the same data read the
    # same way.
    return state, sorted(set(issues))


# ---------------------------------------------------------------------
# the view
# ---------------------------------------------------------------------


@login_required
@permission_required("attendance.view_attendance")
@require_http_methods(["GET"])
def attendance_week_audit_view(request):
    """Audit attendance consistency across a few days, read-only."""
    from attendance.models import Attendance, AttendanceActivity
    from base.models import EmployeeShiftSchedule

    try:
        start, end = _resolve_range(request)
    except AuditError as error:
        return JsonResponse(
            {"code": "INVALID_RANGE", "message": str(error)}, status=400
        )

    # A day either side, so an open row from before the window and a row
    # created after it are both visible to the cross-day checks.
    window_start = start - timedelta(days=1)
    window_end = end + timedelta(days=1)

    employees = list(
        _visible_employees(request).select_related(
            "employee_work_info__company_id",
            "employee_work_info__department_id",
            "employee_work_info__shift_id",
        )
    )
    employee_ids = [employee.pk for employee in employees]

    attendance_rows = list(
        Attendance.objects.filter(
            employee_id_id__in=employee_ids,
            attendance_date__range=(window_start, window_end),
        )
        # `attendance_day` is rendered as text for every row; without
        # this each one costs its own query for the same handful of
        # weekday rows.
        .select_related("attendance_day")
        .order_by("attendance_date", "id")
    )
    activity_rows = list(
        AttendanceActivity.objects.filter(
            employee_id_id__in=employee_ids,
            attendance_date__range=(window_start, window_end),
        )
        .select_related("shift_day")
        .order_by("attendance_date", "id")
    )

    rows_by_employee = defaultdict(list)
    rows_by_key = defaultdict(list)
    for row in attendance_rows:
        rows_by_employee[row.employee_id_id].append(row)
        rows_by_key[(row.employee_id_id, row.attendance_date)].append(row)

    activities_by_employee = defaultdict(list)
    activities_by_key = defaultdict(list)
    for activity in activity_rows:
        activities_by_employee[activity.employee_id_id].append(activity)
        activities_by_key[
            (activity.employee_id_id, activity.attendance_date)
        ].append(activity)

    night_map = _night_shift_dates(attendance_rows)

    shift_ids = {
        identity
        for identity in (
            getattr(
                getattr(employee, "employee_work_info", None), "shift_id_id", None
            )
            for employee in employees
        )
        if identity
    }
    auto_punch_out = {}
    if shift_ids:
        for shift_id, enabled, punch_time in EmployeeShiftSchedule.objects.filter(
            shift_id__in=shift_ids
        ).values_list("shift_id", "is_auto_punch_out_enabled", "auto_punch_out_time"):
            current = auto_punch_out.get(shift_id)
            # Any schedule on the shift with it enabled is enough for the
            # scheduler to act, so an enabled one is not overwritten by a
            # disabled one.
            if current is None or (enabled and not current["enabled"]):
                auto_punch_out[shift_id] = {
                    "enabled": bool(enabled),
                    "time": punch_time,
                }

    issues_only = request.GET.get("issues_only") == "1"
    summary_only = request.GET.get("summary_only") == "1"

    audit_dates = list(_iter_dates(start, end))
    issue_counts = defaultdict(int)
    employee_payloads = []
    ok_days = 0
    anomalous_days = 0
    employees_with_issue = set()

    for employee in employees:
        identity = _employee_identity(employee)
        shift_id = identity["shift_id"]
        punch = auto_punch_out.get(shift_id) or {"enabled": False, "time": None}
        punch_time = punch["time"] if punch["enabled"] else None

        days = []
        for audit_date in audit_dates:
            key = (employee.pk, audit_date)
            rows = rows_by_key.get(key, [])
            activities = activities_by_key.get(key, [])
            previous = (employee.pk, audit_date - timedelta(days=1))
            following = (employee.pk, audit_date + timedelta(days=1))

            state, issues = _classify_day(
                audit_date,
                rows,
                activities,
                rows_by_key.get(previous, []),
                activities_by_key.get(previous, []),
                rows_by_key.get(following, []),
                punch_time,
            )

            simulated_online = _simulate_check_online(
                employee.pk, audit_date, rows_by_employee, night_map
            )
            # The server would say "online" while this day itself holds
            # nothing — the shape that makes a phone offer check-out for
            # a session belonging to another day.
            if simulated_online and not rows:
                issues = sorted(set(issues) | {"CHECK_ONLINE_DISAGREES_WITH_TARGET_DAY"})
                state = "ANOMALOUS"

            for code in issues:
                issue_counts[code] += 1
            if state == "ANOMALOUS":
                anomalous_days += 1
                employees_with_issue.add(employee.pk)
            else:
                ok_days += 1

            days.append(
                {
                    "date": audit_date.isoformat(),
                    "state": state,
                    "issues": issues,
                    "attendance_rows": [_attendance_payload(r) for r in rows],
                    "activity_rows": [_activity_payload(a) for a in activities],
                    "diagnostic_simulation": {
                        "would_be_online_for_this_date": simulated_online,
                    },
                }
            )

        selection = _simulate_checkout_selection(
            employee.pk, rows_by_employee, activities_by_employee
        )
        selection_issues = []
        if selection["same_date"] is False:
            selection_issues.append("CHECKOUT_WOULD_SELECT_DIFFERENT_DATES")
        # Only meaningful when there is an open activity to close: with
        # none, `clock_out_attendance_and_activity` returns without
        # touching any attendance row at all, so the newest row being
        # closed is simply an ordinary finished day, not a hazard.
        if (
            selection["selected_activity_id"] is not None
            and selection["selected_attendance_already_closed"]
        ):
            selection_issues.append(
                "CHECKOUT_WOULD_SELECT_DIFFERENT_RECORD_RELATIONSHIP"
            )
        for code in selection_issues:
            issue_counts[code] += 1
            employees_with_issue.add(employee.pk)

        visibility = _admin_today_visibility(
            request,
            employee,
            identity,
            end,
            rows_by_key.get((employee.pk, end), []),
        )
        if rows_by_key.get((employee.pk, end)) and not visibility[
            "admin_today_attendance_visible"
        ]:
            issue_counts["ADMIN_TODAY_VISIBILITY_MISMATCH"] += 1
            employees_with_issue.add(employee.pk)
            visibility["issue"] = "ADMIN_TODAY_VISIBILITY_MISMATCH"

        payload = {
            "employee": identity,
            "auto_punch_out": {
                "enabled": punch["enabled"],
                "time": str(punch["time"]) if punch["time"] else None,
            },
            "checkout_selection_simulation": {
                **selection,
                "issues": selection_issues,
            },
            "admin_today": visibility,
            "days": days,
        }

        if issues_only:
            has_issue = (
                any(day["issues"] for day in days)
                or selection_issues
                or visibility.get("issue")
            )
            if not has_issue:
                continue
            payload["days"] = [day for day in days if day["issues"]]

        employee_payloads.append(payload)

    localdate = timezone.localdate()
    python_today = date_cls.today()
    summary = {
        "audit_start": start.isoformat(),
        "audit_end": end.isoformat(),
        "inspection_window_start": window_start.isoformat(),
        "inspection_window_end": window_end.isoformat(),
        "timezone_localdate": localdate.isoformat(),
        "python_date_today": python_today.isoformat(),
        "dates_match": localdate == python_today,
        "employee_count": len(employees),
        "employee_day_count": len(employees) * len(audit_dates),
        "ok_employee_days": ok_days,
        "anomalous_employee_days": anomalous_days,
        "employees_with_any_issue": len(employees_with_issue),
        "issue_counts": dict(sorted(issue_counts.items())),
        "critical_issue_count": sum(
            count for code, count in issue_counts.items() if code in CRITICAL_ISSUES
        ),
    }
    if localdate != python_today:
        summary["issue_counts"]["DATE_TODAY_VS_LOCALDATE_MISMATCH"] = 1

    if summary_only:
        return JsonResponse({"summary": summary})
    return JsonResponse({"summary": summary, "employees": employee_payloads})
