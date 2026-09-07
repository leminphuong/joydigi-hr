"""
weekly.py

Phase ATTENDANCE-WEEKLY-UI-1 — "Theo tuần" mode for the attendance table.

A presentation/query layer only. Every judgement about what a date counts as —
present, absent, leave, holiday, week-off — and every number of credited
seconds comes from ``attendance.period``, the module the monthly summary uses.
This file decides how to *draw* a week; it decides nothing about attendance.

Phase ATTENDANCE-WEEKLY-UI-1-FIX-1 replaced the partial copy of those rules
that used to live here (a local ``_day_value`` plus a hard-coded
``weekday() >= 5`` week-off) with calls into that shared module, so the weekly
"Số công" now equals the monthly "Ngày có mặt" for the same employees over the
same dates, and Roster week-offs, HR conflict resolutions and manually edited
hours are all honoured.

Pagination happens before the grid is built: the Attendance query behind the
grid covers only the employees on the page being rendered. The summary cards
describe the whole filtered set, so they run their own pass — a fixed number of
queries, never one per employee, and never the grid.
"""

import datetime

from django.core.paginator import Paginator
from django.shortcuts import render

from attendance.models import AttendanceLateComeEarlyOut
from attendance.period import (
    BUCKET_HOLIDAY,
    BUCKET_WEEK_OFF,
    build_period_context,
    classify_employee_period,
)
from base.methods import paginator_qry
from base.models import Department
from base.roles import checkin_leader_required, is_checkin_admin
from employee.filters import EmployeeFilter
from joydigi.decorators import hx_request_required, login_required

VIETNAMESE_WEEKDAYS = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]

#: Offered in the "N / trang" selector.
PAGE_SIZE_CHOICES = (10, 20, 50, 100)

#: Stable order so a given page always shows the same employees.
EMPLOYEE_ORDERING = ("employee_first_name", "employee_last_name", "id")


def _today():
    return datetime.date.today()


def week_bounds(anchor):
    """Monday..Sunday containing ``anchor``."""
    monday = anchor - datetime.timedelta(days=anchor.weekday())
    return monday, monday + datetime.timedelta(days=6)


def parse_week(value, fallback_anchor):
    """Read ``?week=YYYY-MM-DD`` (any day inside the week) into its bounds."""
    try:
        anchor = datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        anchor = fallback_anchor
    return week_bounds(anchor)


def _seconds_label(seconds):
    """42h15 — the compact form the weekly grid uses."""
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}"


def _day_count_label(present):
    """5 / 4.5 — trailing ".0" trimmed, matching how the monthly table reads."""
    return int(present) if present == int(present) else round(present, 1)


def _late_early_maps(emp_pks, from_date, to_date):
    """Late/early flags per employee per date.

    Read from ``AttendanceLateComeEarlyOut`` — the same model the dashboard and
    the "Đi muộn và về sớm" screen use. One query; nothing is recomputed from
    clock times here.
    """
    late_map = {}
    early_map = {}
    if not emp_pks:
        return late_map, early_map
    flags = AttendanceLateComeEarlyOut.objects.filter(
        employee_id__in=emp_pks,
        attendance_id__attendance_date__range=(from_date, to_date),
    ).values("type", "employee_id_id", "attendance_id__attendance_date")
    for flag in flags:
        target = late_map if flag["type"] == "late_come" else early_map
        target.setdefault(flag["employee_id_id"], set()).add(
            flag["attendance_id__attendance_date"]
        )
    return late_map, early_map


def build_weekly_grid(from_date, to_date, employees):
    """One row per employee, one cell per day in [from_date, to_date].

    ``employees`` must be the *page* of employees being rendered — pagination
    happens before this call, so the queries below are bounded by page size and
    not by headcount.

    Query budget is fixed, not per employee: whatever
    ``attendance.period.build_period_context`` issues for the page, plus one
    for the late/early flags.

    Returns ``(rows, day_headers)``.
    """
    employees = list(employees)
    emp_pks = [employee.pk for employee in employees]

    ctx = build_period_context(from_date, to_date, emp_pks)
    late_map, early_map = _late_early_maps(emp_pks, from_date, to_date)

    rows = []
    for employee in employees:
        days, totals = classify_employee_period(employee.pk, ctx)
        emp_late = late_map.get(employee.pk, ())
        emp_early = early_map.get(employee.pk, ())

        cells = []
        for day in days:
            record = day["record"]
            work_type = (record or {}).get("work_type_id__work_type") or ""
            cells.append(
                {
                    "date": day["date"],
                    # Which bucket the shared rules put this date in. The
                    # template only chooses how to draw it.
                    "bucket": day["bucket"],
                    "is_off_day": day["is_week_off"] or day["is_holiday"],
                    "clock_in": record["attendance_clock_in"] if record else None,
                    "clock_out": record["attendance_clock_out"] if record else None,
                    "is_late": day["date"] in emp_late,
                    "is_early": day["date"] in emp_early,
                    "is_remote": work_type.strip().lower().startswith(
                        "làm việc từ xa"
                    ),
                    "hours_label": (
                        _seconds_label(day["hours_second"]) if record else ""
                    ),
                    "day_value": day["value"],
                    # True when the date is a holiday or a week-off that the
                    # employee nonetheless clocked in on: the times are worth
                    # showing, but the day is not a working day and the shared
                    # rules have already kept it out of "Số công".
                    "worked_on_off_day": bool(
                        record
                        and day["bucket"] in (BUCKET_HOLIDAY, BUCKET_WEEK_OFF)
                    ),
                }
            )

        # Same resolution order as the monthly table: a manually edited period
        # total wins over the computed one.
        worked_seconds = totals["hours_second"]
        if employee.pk in ctx.hours_override_map:
            worked_seconds = ctx.hours_override_map[employee.pk]

        work_info = getattr(employee, "employee_work_info", None)
        department = getattr(work_info, "department_id", None) if work_info else None

        rows.append(
            {
                "employee": employee,
                "department": department,
                "cells": cells,
                "worked_seconds": worked_seconds,
                "worked_label": _seconds_label(worked_seconds),
                # "Số công" == the monthly table's "Ngày có mặt" for these
                # dates. Same helper, same number.
                "day_count": _day_count_label(totals["present"]),
            }
        )

    day_headers = [
        {
            "date": day,
            "label": VIETNAMESE_WEEKDAYS[day.weekday()],
            "short": day.strftime("%d/%m"),
            # Calendar striping only — Saturday and Sunday. Whether a date is
            # actually off for a given employee is a per-cell question the
            # shared rules answer via Roster.
            "is_weekend": day.weekday() >= 5,
        }
        for day in ctx.dates
    ]

    return rows, day_headers


def weekly_summary_totals(from_date, to_date, employee_qs):
    """The five cards, over the whole filtered set rather than the page.

    Deliberately independent of :func:`build_weekly_grid` — the cards describe
    the filter, the grid describes the page, and neither should have to build
    the other. Every card counts *employees*, never attendance rows: one person
    with five attendance records in the week is still one person.

    Fixed query count; no per-employee query.

    Alongside each count, the employees behind it are kept so a card can open
    the list it stands for (ATTENDANCE-WEEKLY-UI-2). The membership tests are
    the very same expressions that produced the counts — nothing is classified
    twice and no second rule exists that could drift from the first. The names
    ride along on the queryset read that already had to happen, so this costs
    no extra query.
    """
    #: Names rendered into a card's list. The count on the card is always the
    #: true total; only the rendered list is capped, so a large company does
    #: not turn one filter change into a megabyte of HTML.
    modal_name_limit = 100

    #: Read once, with the columns a name needs. This replaces the previous
    #: values_list("pk") — same single query, a few more columns.
    employees = list(
        employee_qs.values(
            "pk", "employee_first_name", "employee_last_name", "badge_id"
        )
    )
    emp_pks = [employee["pk"] for employee in employees]

    ctx = build_period_context(from_date, to_date, emp_pks)
    late_map, _early_map = _late_early_maps(emp_pks, from_date, to_date)

    members = {
        "employees": [],
        "with_attendance": [],
        "late": [],
        "on_leave": [],
        "absent": [],
    }

    for employee in employees:
        emp_pk = employee["pk"]
        # collect_days=False: the cards need the totals, never the day cells.
        _days, totals = classify_employee_period(emp_pk, ctx, collect_days=False)

        members["employees"].append(employee)
        if ctx.att_dates_map.get(emp_pk):
            members["with_attendance"].append(employee)
        if late_map.get(emp_pk):
            members["late"].append(employee)
        if ctx.leave_dates_map.get(emp_pk):
            members["on_leave"].append(employee)
        if totals["absent"] > 0:
            members["absent"].append(employee)

    def _full_name(employee):
        """Same shape as ``Employee.get_full_name`` — read off the values row."""
        first = employee["employee_first_name"] or ""
        last = employee["employee_last_name"]
        return f"{first} {last}" if last else first

    result = {key: len(matched) for key, matched in members.items()}
    result["lists"] = [
        {
            "key": key,
            "count": len(matched),
            "names": [
                {"name": _full_name(e), "badge": e["badge_id"] or ""}
                for e in matched[:modal_name_limit]
            ],
            "more": max(0, len(matched) - modal_name_limit),
        }
        # Fixed order so the lists line up with the cards above them.
        for key, matched in (
            (key, members[key])
            for key in (
                "employees",
                "with_attendance",
                "late",
                "on_leave",
                "absent",
            )
        )
    ]
    return result


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _weekly_employees(request):
    """Same visibility rule as the monthly summary — reused, not re-invented."""
    from attendance.views.summary import _summary_employees

    return _summary_employees(request)


def _filtered_employees(request):
    """Apply the filters the weekly mode supports.

    Deliberately the same mechanisms the monthly table already uses:
    ``EmployeeFilter`` for the shared params (including ``search``) plus the
    explicit ``department_id`` multi-select the monthly view handles the same
    way. Company scoping needs nothing here — ``Employee.objects`` is a
    ``JoydigiCompanyManager``, so it is already applied.
    """
    employee_filter = EmployeeFilter(request.GET, queryset=_weekly_employees(request))
    queryset = employee_filter.qs

    department_ids = request.GET.getlist("department_id")
    if department_ids:
        queryset = queryset.filter(
            employee_work_info__department_id__in=department_ids
        )

    return (
        queryset.select_related("employee_work_info__department_id")
        .distinct()
        .order_by(*EMPLOYEE_ORDERING)
    )


def _querystring(request, drop=(), **overrides):
    """Current query string, minus ``drop``, with ``overrides`` applied."""
    params = request.GET.copy()
    for key in drop:
        params.pop(key, None)
    for key, value in overrides.items():
        params[key] = value
    return params.urlencode()


def _export_querystring(request, from_date, to_date):
    """Params for the existing monthly export endpoint.

    Only what that endpoint actually reads is passed: its date range and the
    ``search`` term ``EmployeeFilter`` handles. ``department_id`` is left out
    on purpose — the monthly export does not consume it, and inventing a
    parameter it ignores would promise a filter the file would not honour.
    """
    params = {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
    }
    search = request.GET.get("search")
    if search:
        params["search"] = search
    return "&".join(f"{key}={value}" for key, value in params.items())


def _page_size(request):
    """Requested rows-per-page, or 0 to mean "use the shared default"."""
    try:
        per_page = int(request.GET.get("per_page") or 0)
    except (TypeError, ValueError):
        return 0
    return per_page if per_page in PAGE_SIZE_CHOICES else 0


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@login_required
@checkin_leader_required
def attendance_weekly_summary(request):
    """Full-page shell for the weekly mode. The grid loads over HTMX."""
    from_date, to_date = parse_week(request.GET.get("week"), _today())
    prev_monday = from_date - datetime.timedelta(days=7)
    next_monday = from_date + datetime.timedelta(days=7)

    return render(
        request,
        "attendance/weekly_summary/weekly_summary.html",
        {
            "week": from_date.isoformat(),
            "from_date": from_date,
            "to_date": to_date,
            # Stepping a week keeps every filter and drops only the page
            # number, which a new week has no meaning for.
            "prev_week_qs": _querystring(
                request, drop=("page",), week=prev_monday.isoformat()
            ),
            "next_week_qs": _querystring(
                request, drop=("page",), week=next_monday.isoformat()
            ),
            "departments": Department.objects.filter(
                employeeworkinformation__employee_id__in=_weekly_employees(request)
            ).distinct(),
            "search": request.GET.get("search", ""),
            "selected_departments": [
                int(x) for x in request.GET.getlist("department_id") if x.isdigit()
            ],
            # The export endpoint is admin-only. Showing the button to a leader
            # who would only get a 403 is worse than not showing it.
            "can_export": is_checkin_admin(request.user),
            "export_qs": _export_querystring(request, from_date, to_date),
        },
    )


@login_required
@checkin_leader_required
@hx_request_required
def attendance_weekly_summary_table(request):
    """HTMX partial — the summary cards plus the weekly grid.

    Order matters: the employee queryset is filtered, then paginated, and only
    the resulting page is handed to ``build_weekly_grid``. The cards are
    computed separately over the full filtered set, so describing the filter
    costs a fixed number of queries rather than a grid nobody renders.
    """
    from_date, to_date = parse_week(request.GET.get("week"), _today())

    employees = _filtered_employees(request)
    totals = weekly_summary_totals(from_date, to_date, employees)
    total_row_count = totals["employees"]

    per_page = _page_size(request)
    if per_page:
        page = Paginator(employees, per_page).get_page(request.GET.get("page"))
    else:
        # `paginator_qry` has no size argument; using it here keeps the shared
        # per-user default — and every other screen that calls it — untouched.
        page = paginator_qry(employees, request.GET.get("page"))

    rows, days = build_weekly_grid(from_date, to_date, page.object_list)

    # Percentages shown under each card: arithmetic on numbers already counted
    # above — no new rule about who is late or absent, only "how many of them".
    def _pct(value):
        if not total_row_count:
            return None
        return round(value * 100.0 / total_row_count, 1)

    percentages = {
        "with_attendance": _pct(totals["with_attendance"]),
        "late": _pct(totals["late"]),
        "on_leave": _pct(totals["on_leave"]),
        "absent": _pct(totals["absent"]),
    }

    return render(
        request,
        "attendance/weekly_summary/weekly_table_partial.html",
        {
            "rows": rows,
            "page": page,
            "days": days,
            "totals": totals,
            "from_date": from_date,
            "to_date": to_date,
            "week": from_date.isoformat(),
            "total_row_count": total_row_count,
            "percentages": percentages,
            "per_page": per_page,
            "page_size_choices": PAGE_SIZE_CHOICES,
            "pd": _querystring(request, drop=("page", "per_page")),
        },
    )
