"""
period.py

The one place that decides, for a given employee and a given date, what that
date counts as: a present day, an absent day, leave, a holiday, a week-off —
and how many seconds of work it credits.

Extracted verbatim from ``attendance.views.summary.build_monthly_summary`` in
phase ATTENDANCE-WEEKLY-UI-1-FIX-1, which had been the sole owner of these
rules. Nothing here is new: the monthly table and the weekly table now call the
same code instead of the weekly one carrying a partial copy that disagreed with
it. ``build_monthly_summary`` produces identical output before and after the
extraction.

Two pieces:

``build_period_context``
    Batch-loads everything the rules depend on for a set of employees over a
    date range — attendance, approved leave, roster week-offs, HR conflict
    resolutions, manually edited hours, shift schedules, holidays, company
    leaves — in a fixed number of queries that does not grow with headcount.

``classify_employee_period``
    Pure function. Walks the dates for one employee and applies the rules, in
    the same order and with the same precedence the monthly summary has always
    used: an HR resolution wins over everything, then attendance, then leave,
    then holiday, then week-off, then "absent".

Read-only throughout: nothing in this module writes to the database.
"""

import datetime
from collections import defaultdict

from attendance.methods.utils import strtime_seconds
from attendance.methods.worktime import approved_overtime_seconds
from attendance.models import (
    Attendance,
    AttendanceConflictResolution,
    AttendanceDailyHours,
    AttendanceSummaryHours,
    GraceTime,
    OvertimeRequest,
)
from base.methods import (
    get_company_leave_dates,
    get_holiday_dates,
    get_working_days,
)
from base.models import EmployeeShiftSchedule, Roster

#: Weekday index -> the day name ``EmployeeShiftSchedule`` stores.
DAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

#: An HR conflict resolution -> the bucket and the value it forces.
RES_BUCKET = {
    "full_present": ("present", 1.0),
    "half_present": ("present", 0.5),
    "absent": ("absent", 1.0),
    "paid_leave": ("paid_leave", 1.0),
    "unpaid_leave": ("unpaid_leave", 1.0),
    "holiday": ("holiday", 1.0),
    "week_off": ("week_off", 1.0),
}

#: Credited when a resolution marks a day present but no shift schedule says
#: how long that day is.
DEFAULT_FULL_DAY_SECONDS = 28800  # 8h

#: Buckets ``classify_employee_period`` can put a date in. ``"off"`` means the
#: date contributed to nothing — a company-off day with no activity.
BUCKET_PRESENT = "present"
BUCKET_ABSENT = "absent"
BUCKET_PAID_LEAVE = "paid_leave"
BUCKET_UNPAID_LEAVE = "unpaid_leave"
BUCKET_HOLIDAY = "holiday"
BUCKET_WEEK_OFF = "week_off"
BUCKET_OFF = "off"


def iter_dates(start, end):
    """Yield every date from start to end inclusive."""
    current = start
    while current <= end:
        yield current
        current += datetime.timedelta(days=1)


def attendance_day_value(worked_seconds, minimum_hour, grace_secs):
    """How much of a day one attendance record is worth: 1.0, 0.5 or 0.0.

    Full day once the worked time reaches the shift minimum less the grace
    allowance, half a day at or above half the minimum, otherwise zero. A date
    with no shift schedule at all (holiday / week-off) counts as a full day.
    """
    minimum_secs = strtime_seconds(minimum_hour) if minimum_hour else 0
    if minimum_secs <= 0:
        return 1.0
    effective_minimum = max(0, minimum_secs - grace_secs)
    if worked_seconds >= effective_minimum:
        return 1.0
    if worked_seconds >= minimum_secs / 2:
        return 0.5
    return 0.0


class PeriodContext:
    """Everything the day rules need, batch-loaded for a set of employees.

    Built by :func:`build_period_context`; consumed by
    :func:`classify_employee_period`. Plain attribute bag — no behaviour.
    """

    __slots__ = (
        "from_date",
        "to_date",
        "dates",
        "total_working",
        "off_dates",
        "holiday_dates",
        "company_off_dates",
        "grace_secs",
        "att_value_map",
        "att_secs_map",
        "att_dates_map",
        "att_detail_map",
        "att_ot_secs_map",
        "att_ot_approved_map",
        "emp_shift_map",
        "shift_day_secs",
        "hours_override_map",
        "daily_manual_map",
        "paid_dates_map",
        "unpaid_dates_map",
        "leave_dates_map",
        "roster_has",
        "roster_off_dates",
        "resolutions_map",
        "approved_ot_secs_map",
    )

    def employee_off_dates(self, emp_pk):
        """Week-off dates for one employee.

        Roster is authoritative when the employee has any roster entry in the
        range; employees with no roster fall back to the company leave dates.
        """
        if emp_pk in self.roster_has:
            return self.roster_off_dates.get(emp_pk, set())
        return self.company_off_dates


def build_period_context(from_date, to_date, emp_pks):
    """Batch-load the period data for ``emp_pks`` over [from_date, to_date].

    Query count is fixed — it does not grow with the number of employees, so
    callers should pass only the employees they are about to render.
    """
    from leave.models import LeaveRequest  # local import to avoid circular

    emp_pks = list(emp_pks)
    ctx = PeriodContext()
    ctx.from_date = from_date
    ctx.to_date = to_date
    ctx.dates = list(iter_dates(from_date, to_date))

    # -- 1. Working days (respects CompanyLeaves + public Holidays) ----------
    working_data = get_working_days(from_date, to_date)
    ctx.total_working = working_data["total_working_days"]
    ctx.off_dates = working_data["company_leave_dates"]

    # -- 1b. Public holidays --------------------------------------------------
    ctx.holiday_dates = set(
        d for d in get_holiday_dates(from_date, to_date) if from_date <= d <= to_date
    )

    # -- 1c. Company leave dates (fallback week-off when no roster) -----------
    raw_cl = list(
        set(
            get_company_leave_dates(from_date.year)
            + get_company_leave_dates(to_date.year)
        )
    )
    ctx.company_off_dates = {d for d in raw_cl if from_date <= d <= to_date}

    # -- 2. Attendance --------------------------------------------------------
    ctx.grace_secs = 0
    default_grace = GraceTime.objects.filter(is_default=True, is_active=True).first()
    if default_grace:
        ctx.grace_secs = default_grace.allowed_time_in_secs or 0

    ctx.att_value_map = defaultdict(dict)  # {emp_pk: {date: 0.0|0.5|1.0}}
    ctx.att_secs_map = defaultdict(dict)  # {emp_pk: {date: at_work_second}}
    ctx.att_dates_map = defaultdict(set)  # {emp_pk: {date}}
    ctx.att_detail_map = defaultdict(dict)  # {emp_pk: {date: record}} — display
    ctx.att_ot_secs_map = defaultdict(dict)
    ctx.att_ot_approved_map = defaultdict(dict)

    if emp_pks:
        att_records = Attendance.objects.filter(
            employee_id__in=emp_pks,
            attendance_date__range=(from_date, to_date),
        ).values(
            "id",
            "employee_id_id",
            "attendance_date",
            "at_work_second",
            "overtime_second",
            "minimum_hour",
            "attendance_overtime_approve",
            # Display-only fields, carried on the same query so the weekly grid
            # needs no second pass over the same rows.
            "attendance_clock_in",
            "attendance_clock_out",
            "work_type_id__work_type",
        )
        for record in att_records:
            pk = record["employee_id_id"]
            date = record["attendance_date"]
            worked = record["at_work_second"] or 0
            ctx.att_dates_map[pk].add(date)
            ctx.att_value_map[pk][date] = attendance_day_value(
                worked, record.get("minimum_hour"), ctx.grace_secs
            )
            ctx.att_secs_map[pk][date] = worked
            ctx.att_detail_map[pk][date] = record
            ctx.att_ot_secs_map[pk][date] = record["overtime_second"] or 0
            ctx.att_ot_approved_map[pk][date] = bool(
                record["attendance_overtime_approve"]
            )

    # -- 2b. Shift schedules --------------------------------------------------
    from employee.models import EmployeeWorkInformation

    ctx.emp_shift_map = {}
    for work_info in EmployeeWorkInformation.objects.filter(
        employee_id__in=emp_pks
    ).values("employee_id_id", "shift_id_id"):
        ctx.emp_shift_map[work_info["employee_id_id"]] = work_info["shift_id_id"]

    shift_pks = {v for v in ctx.emp_shift_map.values() if v}
    ctx.shift_day_secs = defaultdict(dict)  # {shift_pk: {day_name: seconds}}
    if shift_pks:
        for schedule in (
            EmployeeShiftSchedule.objects.filter(shift_id__in=shift_pks)
            .select_related("day")
            .values("shift_id_id", "day__day", "minimum_working_hour")
        ):
            ctx.shift_day_secs[schedule["shift_id_id"]][schedule["day__day"]] = (
                strtime_seconds(schedule["minimum_working_hour"])
                if schedule["minimum_working_hour"]
                else 0
            )

    # -- 2c. Manually edited period hours -------------------------------------
    ctx.hours_override_map = {}
    for row in AttendanceSummaryHours.objects.filter(
        employee_id__in=emp_pks,
        from_date=from_date,
        to_date=to_date,
        is_manually_edited=True,
    ).values("employee_id_id", "hours_second"):
        ctx.hours_override_map[row["employee_id_id"]] = row["hours_second"]

    # -- 2d. Manually edited per-day hours ------------------------------------
    ctx.daily_manual_map = defaultdict(dict)
    for row in AttendanceDailyHours.objects.filter(
        employee_id__in=emp_pks,
        date__range=(from_date, to_date),
        is_manually_edited=True,
    ).values("employee_id_id", "date", "hours_second"):
        ctx.daily_manual_map[row["employee_id_id"]][row["date"]] = row["hours_second"]

    # -- 3. Approved leave ----------------------------------------------------
    leave_qs = (
        LeaveRequest.objects.filter(
            employee_id__in=emp_pks,
            status="approved",
            start_date__lte=to_date,
        )
        .filter(end_date__isnull=False, end_date__gte=from_date)
        .select_related("leave_type_id")
    )
    single_day_qs = LeaveRequest.objects.filter(
        employee_id__in=emp_pks,
        status="approved",
        start_date__range=(from_date, to_date),
        end_date__isnull=True,
    ).select_related("leave_type_id")

    ctx.leave_dates_map = defaultdict(set)
    ctx.paid_dates_map = defaultdict(set)
    ctx.unpaid_dates_map = defaultdict(set)
    for leave in list(leave_qs) + list(single_day_qs):
        span_start = max(leave.start_date, from_date)
        span_end = min(leave.end_date or leave.start_date, to_date)
        for date in iter_dates(span_start, span_end):
            ctx.leave_dates_map[leave.employee_id_id].add(date)
            if leave.leave_type_id.payment == "paid":
                ctx.paid_dates_map[leave.employee_id_id].add(date)
            else:
                ctx.unpaid_dates_map[leave.employee_id_id].add(date)

    # -- 4. Roster week-offs --------------------------------------------------
    ctx.roster_has = set()
    ctx.roster_off_dates = defaultdict(set)
    for entry in Roster.objects.filter(
        employee_id__in=emp_pks,
        date__range=(from_date, to_date),
    ).values("employee_id", "is_off", "date"):
        ctx.roster_has.add(entry["employee_id"])
        if entry["is_off"]:
            ctx.roster_off_dates[entry["employee_id"]].add(entry["date"])

    # -- 4b. Approved overtime requests ---------------------------------------
    # Phase ATTENDANCE-WEEKEND-OT-REQUEST-IMPLEMENT-1. An employee working a
    # weekend does not check in — the day is a week-off — so there is no
    # Attendance row for the overtime column to read. The approved requests
    # themselves are the record, and they are read here rather than projected
    # into a synthetic Attendance row: nothing is written, so approving twice
    # cannot double-count, a cancellation drops out of the total on its own,
    # and no row exists that a later reader could mistake for someone actually
    # having been at work.
    #
    # Loaded for every date in the range; which dates are allowed to consume
    # it (week-off / holiday only, never a normal working day) is decided by
    # the caller — see `build_monthly_summary`. Classification (present /
    # absent / week-off) never reads this map, so a request cannot change what
    # kind of day it is.
    ctx.approved_ot_secs_map = defaultdict(dict)
    if emp_pks:
        _windows = defaultdict(lambda: defaultdict(list))
        for row in OvertimeRequest.objects.filter(
            employee_id__in=emp_pks,
            request_date__range=(from_date, to_date),
            approved=True,
            canceled=False,
            is_active=True,
        ).values("employee_id_id", "request_date", "start_time", "end_time"):
            _windows[row["employee_id_id"]][row["request_date"]].append(
                (row["start_time"], row["end_time"])
            )
        for emp_pk, by_date in _windows.items():
            for date, windows in by_date.items():
                seconds = approved_overtime_seconds(windows)
                if seconds:
                    ctx.approved_ot_secs_map[emp_pk][date] = seconds

    # -- 5. HR conflict resolutions -------------------------------------------
    ctx.resolutions_map = defaultdict(dict)
    for row in AttendanceConflictResolution.objects.filter(
        date__range=(from_date, to_date),
    ).values("employee_id_id", "date", "resolution"):
        ctx.resolutions_map[row["employee_id_id"]][row["date"]] = row["resolution"]

    return ctx


def classify_employee_period(emp_pk, ctx, collect_days=True):
    """Apply the day rules to one employee over the context's date range.

    Returns ``(days, totals)``:

    ``days``   one dict per date, in date order, saying which bucket the date
               landed in, how much it was worth, how many seconds it credited,
               and the attendance record behind it (``None`` when there is
               none). This is what a per-day grid renders. Pass
               ``collect_days=False`` when only the totals are wanted — the
               summary cards count employees and never draw the days — and the
               list comes back empty instead of being materialised.
    ``totals`` the same accumulators ``build_monthly_summary`` has always
               produced: present, paid_leave, unpaid_leave, week_off, holiday,
               absent and hours_second.

    The accumulation runs inside this loop, in date order, exactly as it did
    when it lived in ``build_monthly_summary`` — so the sums are identical, not
    merely equivalent. ``collect_days`` only decides whether the per-day dicts
    are kept; it never changes a number.
    """
    att_values = ctx.att_value_map.get(emp_pk, {})
    att_secs = ctx.att_secs_map.get(emp_pk, {})
    att_detail = ctx.att_detail_map.get(emp_pk, {})
    paid_dates = ctx.paid_dates_map.get(emp_pk, set())
    unpaid_dates = ctx.unpaid_dates_map.get(emp_pk, set())
    emp_off = ctx.employee_off_dates(emp_pk)
    resolutions = ctx.resolutions_map.get(emp_pk, {})
    shift_pk = ctx.emp_shift_map.get(emp_pk)
    shift_sched = ctx.shift_day_secs.get(shift_pk, {}) if shift_pk else {}
    daily_hours = ctx.daily_manual_map.get(emp_pk, {})
    holiday_dates = ctx.holiday_dates
    off_set = frozenset(ctx.off_dates)

    present = paid_leave = unpaid_leave = week_off = holiday_c = absent = 0.0
    hours_second = 0
    days = []

    for date in ctx.dates:
        resolution = resolutions.get(date)
        day = {
            "date": date,
            "bucket": BUCKET_OFF,
            "value": 0.0,
            "absent_value": 0.0,
            "hours_second": 0,
            "resolution": resolution,
            "record": att_detail.get(date),
            "is_holiday": date in holiday_dates,
            "is_week_off": date in emp_off,
        }
        if collect_days:
            days.append(day)

        # Direct HR override — use as-is
        bucket_info = RES_BUCKET.get(resolution)
        if bucket_info is not None:
            bucket, val = bucket_info
            if bucket == "present":
                present += val
            elif bucket == "paid_leave":
                paid_leave += val
            elif bucket == "unpaid_leave":
                unpaid_leave += val
            elif bucket == "absent":
                absent += val
            elif bucket == "holiday":
                holiday_c += val
            elif bucket == "week_off":
                week_off += val

            day["bucket"] = bucket
            day["value"] = val
            if bucket == "absent":
                day["absent_value"] = val

            # Hours for regularized present days (per-day manual override wins)
            if bucket == "present":
                day_name = DAY_NAMES[date.weekday()]
                full_secs = shift_sched.get(day_name, 0) or DEFAULT_FULL_DAY_SECONDS
                day_manual = daily_hours.get(date)
                credited = (
                    day_manual if day_manual is not None else int(full_secs * val)
                )
                hours_second += credited
                day["hours_second"] = credited
            continue

        if resolution == "partial_hours":
            # Count as present proportionally: actual_secs / shift_min (capped at 1.0)
            actual_secs = att_secs.get(date, 0)
            day_name = DAY_NAMES[date.weekday()]
            full_secs = shift_sched.get(day_name, 0) or DEFAULT_FULL_DAY_SECONDS
            val = (
                min(1.0, actual_secs / full_secs)
                if full_secs > 0
                else (1.0 if actual_secs > 0 else 0.0)
            )
            present += val
            if val < 1.0:
                absent += 1.0 - val
                day["absent_value"] = 1.0 - val
            day_manual = daily_hours.get(date)
            credited = day_manual if day_manual is not None else actual_secs
            hours_second += credited
            day["bucket"] = BUCKET_PRESENT
            day["value"] = val
            day["hours_second"] = credited
            continue

        # No resolution — natural computation
        if date in att_values:
            val = att_values[date]
            if date in holiday_dates:
                holiday_c += 1.0  # HO — attendance on holiday
                day["bucket"] = BUCKET_HOLIDAY
                day["value"] = 1.0
            elif date in emp_off:
                week_off += 1.0  # WO — attendance on week-off
                day["bucket"] = BUCKET_WEEK_OFF
                day["value"] = 1.0
            else:
                present += val
                day["bucket"] = BUCKET_PRESENT
                day["value"] = val
                # Half-day (0.5) or zero-hour: remaining fraction is absent
                if val < 1.0:
                    absent += 1.0 - val
                    day["absent_value"] = 1.0 - val
            day_manual = daily_hours.get(date)
            credited = day_manual if day_manual is not None else att_secs.get(date, 0)
            hours_second += credited
            day["hours_second"] = credited
        elif date in paid_dates:
            paid_leave += 1.0
            day["bucket"] = BUCKET_PAID_LEAVE
            day["value"] = 1.0
        elif date in unpaid_dates:
            unpaid_leave += 1.0
            day["bucket"] = BUCKET_UNPAID_LEAVE
            day["value"] = 1.0
        elif date in holiday_dates:
            holiday_c += 1.0
            day["bucket"] = BUCKET_HOLIDAY
            day["value"] = 1.0
        elif date in emp_off:
            week_off += 1.0
            day["bucket"] = BUCKET_WEEK_OFF
            day["value"] = 1.0
        elif date not in off_set:
            absent += 1.0  # working day with no activity
            day["bucket"] = BUCKET_ABSENT
            day["value"] = 1.0
            day["absent_value"] = 1.0

    totals = {
        "present": present,
        "paid_leave": paid_leave,
        "unpaid_leave": unpaid_leave,
        "week_off": week_off,
        "holiday": holiday_c,
        "absent": absent,
        "hours_second": hours_second,
    }
    return days, totals
