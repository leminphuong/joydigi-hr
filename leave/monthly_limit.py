"""Monthly usage limit of the shared annual leave fund (2026-11-15).

JOYDIGI policy:

* the fund keeps its accrual (1 day a month, at most 12 a year), shared by
  NGHỈ PHÉP THÁNG and Nghỉ Phép Năm (``LeaveType.shared_balance_with``);
* an employee USES at most ``LEAVE_MONTHLY_USAGE_LIMIT_DAYS`` (2) days of
  that fund in each calendar month — a limit on use, not days added;
* the balance must still cover the request (checked as before).

What counts in a month: the days of the employee's requests of any type of
the fund that are waiting (``requested``) or ``approved`` — an approved leave
with a cancellation request still waiting stays approved, so it keeps
counting until the cancellation is approved. Rejected and cancelled requests
count nothing (their days are released).

How a request's days are split over months: day by day, with the same
rules as the request's own day count (``LeaveRequest.save``) — half days,
the type's holiday / company-leave (weekly off) exclusions — then scaled so
the parts add up to exactly the days the request records (or will record).
A date covered by two requests counts once (at most one day per date).

Types with their own fund (Nghỉ Chế Độ) and compensatory leave (Nghỉ Bù) are
never limited. Setting the limit to 0 turns the rule off.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError

COUNTED_STATUSES = ("requested", "approved")
#: Fields of a request that decide its days and whose fund they come from.
BALANCE_FIELDS = (
    "employee_id_id",
    "leave_type_id_id",
    "start_date",
    "end_date",
    "start_date_breakdown",
    "end_date_breakdown",
)
_EPSILON = 1e-6


class LeaveRuleError(ValidationError):
    """A leave rule refused a save; the message is for the employee/HR."""


class LeaveMonthlyLimitError(LeaveRuleError):
    """The request would take the month over the limit; the message says so."""


class LeaveBalanceShortError(LeaveRuleError):
    """The fund, less the days other waiting requests hold, can not cover it."""


def limit_days() -> float:
    value = getattr(settings, "LEAVE_MONTHLY_USAGE_LIMIT_DAYS", 2)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 2.0


def fund_type_ids(leave_type) -> list[int]:
    """The ids of every type drawing on the same shared fund as
    ``leave_type`` (the fund's own type first), or ``[]`` when the type is not
    limited: no shared fund, or compensatory leave."""
    from leave.models import LeaveType, balance_leave_type

    if leave_type is None or getattr(leave_type, "is_compensatory_leave", False):
        return []
    pool = balance_leave_type(leave_type)
    if pool is None:
        return []
    users = list(
        LeaveType._base_manager.filter(shared_balance_with=pool).values_list("pk", "is_active")
    )
    if not any(active for _pk, active in users):
        # A type with its own fund (e.g. Nghỉ Chế Độ) — the same test as the
        # summaries' (``shared_fund_types``: active types sharing the fund),
        # so an enforced limit is always also shown.
        return []
    # Requests of a switched-off type of the fund still count.
    return [pool.pk] + [pk for pk, _active in users]


def is_limited(leave_type) -> bool:
    return limit_days() > 0 and bool(fund_type_ids(leave_type))


def fmt(days: float) -> str:
    """1.5 -> "1,5"; 2.0 -> "2" (Vietnamese decimal comma)."""
    value = round(float(days or 0), 2)
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


# -- splitting a request over its dates ----------------------------------------


def _excluded_dates(leave_type, employee, first: date, last: date) -> set:
    """The dates in [first, last] the type does not count: holidays and/or
    company leave (weekly offs), as configured on the type."""
    from django.db.models import Q

    from base.models import CompanyLeaves, Holidays
    from leave.methods import company_leave_dates_list, holiday_dates_list

    excluded = set()
    if getattr(leave_type, "exclude_holiday", None) == "yes":
        holidays = Holidays.objects.filter(Q(is_specific=False) | Q(employees=employee)) if employee else Holidays.objects.all()
        excluded.update(holiday_dates_list(holidays))
    if getattr(leave_type, "exclude_company_leave", None) == "yes":
        company_leaves = list(CompanyLeaves.objects.all())
        for year in range(first.year, last.year + 1):
            excluded.update(company_leave_dates_list(company_leaves, date(year, 1, 1)))
    return {d for d in excluded if first <= d <= last}


def day_weights(start: date, end: date | None, start_breakdown: str, end_breakdown: str,
                leave_type, employee, total_days: float) -> dict:
    """``{date: days}`` of one request, adding up to ``total_days``."""
    end = end or start
    if end < start:
        return {}
    dates = [start + timedelta(i) for i in range((end - start).days + 1)]
    weights = {}
    for day in dates:
        if start == end:
            weight = 1.0 if start_breakdown == "full_day" and end_breakdown == "full_day" else 0.5
        elif day == start:
            weight = 1.0 if start_breakdown == "full_day" else 0.5
        elif day == end:
            weight = 1.0 if end_breakdown == "full_day" else 0.5
        else:
            weight = 1.0
        weights[day] = weight
    for day in _excluded_dates(leave_type, employee, start, end):
        weights[day] = 0.0
    counted = sum(weights.values())
    total_days = float(total_days or 0)
    if counted > _EPSILON:
        factor = total_days / counted
        return {day: weight * factor for day, weight in weights.items() if weight}
    if total_days > _EPSILON:
        return {start: total_days}  # nothing countable: keep the recorded days on the first day
    return {}


def _request_weights(request) -> dict:
    return day_weights(
        request.start_date, request.end_date, request.start_date_breakdown, request.end_date_breakdown,
        request.leave_type_id, request.employee_id, request.requested_days,
    )


def _month(day: date):
    return day.year, day.month


def _month_bounds(year: int, month: int):
    first = date(year, month, 1)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return first, last


# -- what a month holds ---------------------------------------------------------


def _held_by_date(employee, type_ids, first: date, last: date, exclude_pk=None) -> dict:
    """``{date: days}`` held by the employee's counted requests of the fund
    in [first, last], at most one day per date."""
    from leave.models import LeaveRequest

    rows = LeaveRequest._base_manager.select_related("leave_type_id").filter(
        employee_id=employee,
        leave_type_id__in=type_ids,
        status__in=COUNTED_STATUSES,
        start_date__lte=last,
    ).filter(models_q_end_on_or_after(first))
    if exclude_pk:
        rows = rows.exclude(pk=exclude_pk)
    held = defaultdict(float)
    for row in rows:
        for day, days in _request_weights(row).items():
            if first <= day <= last:
                held[day] += days
    return {day: min(1.0, days) for day, days in held.items()}


def models_q_end_on_or_after(first: date):
    from django.db.models import Q

    return Q(end_date__gte=first) | Q(end_date__isnull=True, start_date__gte=first)


def month_usage(employee, leave_type, year: int, month: int, exclude_pk=None) -> dict:
    """For one calendar month: the limit, the days used (approved), waiting
    (requested), held (both) and what can still be asked for."""
    from leave.models import LeaveRequest

    limit = limit_days()
    type_ids = fund_type_ids(leave_type)
    first, last = _month_bounds(year, month)
    result = {
        "year": year, "month": month, "limit_days": limit,
        "used_days": 0.0, "pending_days": 0.0, "held_days": 0.0, "remaining_days": limit,
    }
    if not type_ids or limit <= 0:
        return result
    rows = LeaveRequest._base_manager.select_related("leave_type_id").filter(
        employee_id=employee, leave_type_id__in=type_ids, status__in=COUNTED_STATUSES, start_date__lte=last,
    ).filter(models_q_end_on_or_after(first))
    if exclude_pk:
        rows = rows.exclude(pk=exclude_pk)
    by_date = defaultdict(lambda: {"approved": 0.0, "requested": 0.0})
    for row in rows:
        for day, days in _request_weights(row).items():
            if first <= day <= last:
                by_date[day][row.status] += days
    used = pending = 0.0
    for parts in by_date.values():
        approved = min(1.0, parts["approved"])
        used += approved
        pending += min(1.0 - approved, parts["requested"])
    held = used + pending
    result.update(
        used_days=round(used, 4), pending_days=round(pending, 4), held_days=round(held, 4),
        remaining_days=round(max(0.0, limit - held), 4),
    )
    return result


def months_ahead(employee, leave_type, today: date | None = None, count: int = 3) -> list:
    """``month_usage`` for this month and the next ``count - 1`` months."""
    today = today or date.today()
    year, month = today.year, today.month
    months = []
    for _ in range(count):
        months.append(month_usage(employee, leave_type, year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


# -- the check ------------------------------------------------------------------


def candidate_days(employee, leave_type, start, end, start_breakdown, end_breakdown) -> float:
    """The days a new request would record, by the same rules as
    ``LeaveRequest.save`` (an unsaved copy computes them)."""
    from leave.methods import calculate_requested_days
    from leave.models import LeaveRequest

    probe = LeaveRequest(
        employee_id=employee, leave_type_id=leave_type, start_date=start, end_date=end or start,
        start_date_breakdown=start_breakdown or "full_day", end_date_breakdown=end_breakdown or "full_day",
    )
    probe.requested_days = calculate_requested_days(
        probe.start_date, probe.end_date, probe.start_date_breakdown, probe.end_date_breakdown
    )
    if leave_type.exclude_company_leave == "yes" and leave_type.exclude_holiday == "yes":
        probe.exclude_all_leaves()
    else:
        probe.exclude_leaves()
    return float(probe.requested_days or 0)


def monthly_limit_problem(employee, leave_type, start, end, start_breakdown="full_day", end_breakdown="full_day",
                          *, requested_days=None, exclude_pk=None) -> str | None:
    """Why this request would take a month over the limit (a sentence for
    the employee), or ``None``. ``exclude_pk``: the request being edited."""
    if employee is None or leave_type is None or start is None or not is_limited(leave_type):
        return None
    end = end or start
    if end < start:
        return None
    limit = limit_days()
    if requested_days is None:
        requested_days = candidate_days(employee, leave_type, start, end, start_breakdown, end_breakdown)
    weights = day_weights(start, end, start_breakdown, end_breakdown, leave_type, employee, requested_days)
    if not weights:
        return None
    type_ids = fund_type_ids(leave_type)
    by_month = defaultdict(dict)
    for day, days in weights.items():
        by_month[_month(day)][day] = days
    for (year, month) in sorted(by_month):
        first, last = _month_bounds(year, month)
        held = _held_by_date(employee, type_ids, first, last, exclude_pk=exclude_pk)
        before = sum(held.values())
        after_dates = dict(held)
        for day, days in by_month[(year, month)].items():
            after_dates[day] = min(1.0, after_dates.get(day, 0.0) + days)
        after = sum(after_dates.values())
        if after > limit + _EPSILON:
            remaining = max(0.0, limit - before)
            return (
                f"Mỗi tháng chỉ được nghỉ phép tối đa {fmt(limit)} ngày. "
                f"Tháng {month:02d}/{year} đã dùng hoặc đang chờ duyệt {fmt(before)} ngày, "
                f"chỉ còn đăng ký thêm được {fmt(remaining)} ngày "
                f"(đơn này cần {fmt(after - before)} ngày trong tháng đó)."
            )
    return None


def _same(name, stored_value, request):
    value = getattr(request, name)
    if name == "end_date":
        # An old row without an end date means a one-day leave.
        stored_value = stored_value or request.start_date
        value = value or request.start_date
    return stored_value == value


def adds_usage(request) -> bool:
    """Does saving ``request`` (maybe) hold more days of a month than its
    stored row did? New requests, requests moving into a counted status, or
    whose dates, type or employee changed. A decision on a waiting request
    (approve) or a note edit does not."""
    if request.status not in COUNTED_STATUSES:
        return False
    if not request.pk:
        return True
    from leave.models import LeaveRequest

    stored = LeaveRequest._base_manager.filter(pk=request.pk).values("status", *BALANCE_FIELDS).first()
    if stored is None:
        return True
    if stored["status"] not in COUNTED_STATUSES:
        return True
    return not all(_same(name, stored[name], request) for name in BALANCE_FIELDS)


def check_request(request):
    """Raise ``LeaveMonthlyLimitError`` when ``request`` would take a month
    over the limit. Call it with the fund's balance row locked (see
    ``LeaveRequest.save``) so two requests at once can not both pass."""
    if not adds_usage(request):
        return
    problem = monthly_limit_problem(
        request.employee_id, request.leave_type_id, request.start_date, request.end_date,
        request.start_date_breakdown, request.end_date_breakdown,
        requested_days=request.requested_days, exclude_pk=request.pk,
    )
    if problem:
        raise LeaveMonthlyLimitError(problem)
    if request.status == "requested":
        _check_balance_hold(request)


BALANCE_SHORT = "Không đủ số dư phép: một phần quỹ đang được các đơn chờ duyệt khác giữ."


def _check_balance_hold(request):
    """The fund, less the days the employee's OTHER waiting requests of the
    fund hold, must cover this waiting request — the same test as the web
    form and the API, again under the fund's lock (2026-11-15)."""
    from leave.models import AvailableLeave, balance_leave_type
    from leave.services import pending_shared_days

    pool = balance_leave_type(request.leave_type_id)
    row = AvailableLeave._base_manager.filter(employee_id=request.employee_id, leave_type_id=pool).first()
    if row is None:
        return
    free = float(row.available_days or 0) + float(row.carryforward_days or 0)
    try:
        # days the fund will have accrued by then, as the web form allows
        free += float(row.forcasted_leaves(request.start_date) or 0)
    except Exception:  # noqa: BLE001 — the forecast is a bonus, never a block
        pass
    free -= pending_shared_days(request.employee_id, pool, exclude_request=request)
    if float(request.requested_days or 0) > free + _EPSILON:
        raise LeaveBalanceShortError(BALANCE_SHORT)


# -- approved requests: what may not change -------------------------------------

APPROVED_EDIT_REFUSED = (
    "Đơn đã được duyệt nên không thể sửa nhân viên, loại phép, ngày nghỉ hay số ngày. "
    "Chỉ sửa được ghi chú. Để điều chỉnh, hãy gửi yêu cầu hủy đơn (hoàn phép có ghi lịch sử) "
    "rồi tạo đơn mới."
)


class ApprovedLeaveChangeError(LeaveRuleError):
    """A save would change the days or fund of a decided request."""


def _stored_approved(request):
    if not request.pk:
        return None
    from leave.models import LeaveRequest

    stored = LeaveRequest._base_manager.filter(pk=request.pk).values(
        "status", "requested_days", *BALANCE_FIELDS
    ).first()
    if stored is None or stored["status"] != "approved" or request.status != "approved":
        return None
    return stored


def decided_balance_change(request) -> bool:
    """``request`` is stored as approved and stays approved, but one of the
    fields that decide its days or its fund would change."""
    stored = _stored_approved(request)
    return stored is not None and not all(_same(name, stored[name], request) for name in BALANCE_FIELDS)


def stored_approved_days(request):
    """The days an approved request recorded, when it stays approved with the
    same dates (a note edit): kept as they are, never recomputed — the days
    taken from the fund were counted on them."""
    stored = _stored_approved(request)
    if stored is None or not all(_same(name, stored[name], request) for name in BALANCE_FIELDS):
        return None
    return stored["requested_days"]
