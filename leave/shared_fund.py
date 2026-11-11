"""One annual fund for two request types — conversion and monthly accrual.

2026-11-11, JOYDIGI policy (Admin): 1 day of leave per month, 12 days for the
year 2026; "NGHỈ PHÉP THÁNG" and "Nghỉ Phép Năm" are two ways to request
days from that ONE fund, never two funds. "Nghỉ Chế Độ" stays separate.

The fund is the balance row (``AvailableLeave``) of one leave type — the
"pool". The other type points at it (``LeaveType.shared_balance_with``) and
has no balance of its own: its requests are checked against, deducted from
and refunded to the pool row (``leave.models.balance_leave_type``).

This module only plans and applies the two data operations HR runs by hand:

* ``plan_conversion`` / ``apply_conversion`` — link the types and turn the
  old, separate balances into the one fund: for each employee the fund is
  set to ``entitlement − days approved this year over both types``. The
  entitlement is given explicitly (HR's decision: the full 12, or what has
  accrued so far); nothing is guessed.
* ``plan_accrual`` / ``apply_accrual`` — credit one month (default 1 day),
  never past the yearly cap, never twice for the same month.

Both write only with an explicit, expected row count, all-or-nothing, and
leave a marker in each changed row's history (``history_change_reason``):
that marker is the audit trail and what makes reruns harmless. Old balance
rows of the sharing type are switched off, never deleted.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.db.models import Sum

ACCRUAL_MARK = "[tích lũy {period}]"
CONVERSION_MARK = "[quỹ chung {year}: tích lũy {days:g} ngày đến {period}]"
MERGED_MARK = "[quỹ chung {year}: gộp vào quỹ loại #{pool}]"
_ACCRUAL_RE = re.compile(r"\[tích lũy (\d{4})-(\d{2})\]")
_CONVERSION_RE = re.compile(r"\[quỹ chung (\d{4}): tích lũy ([\d.]+) ngày đến (\d{4})-(\d{2})\]")


class SharedFundError(Exception):
    """A request that can not be carried out as asked; nothing was written."""


# -- reading -------------------------------------------------------------------


def fund_types(pool):
    """The pool and every type drawing on it."""
    from leave.models import LeaveType

    return [pool] + list(LeaveType._base_manager.filter(shared_balance_with=pool).order_by("pk"))


def requests_days(employee, leave_types, year, statuses):
    """Requested days of ``employee`` in ``year`` over ``leave_types``, by type id."""
    from leave.models import LeaveRequest

    rows = (
        LeaveRequest._base_manager.filter(
            employee_id=employee,
            leave_type_id__in=[t.pk for t in leave_types],
            status__in=statuses,
            start_date__year=year,
        )
        .values_list("leave_type_id")
        .annotate(days=Sum("requested_days"))
    )
    return {type_id: float(days or 0) for type_id, days in rows}


def accrual_state(row, year):
    """``(accrued_days, credited_periods)`` already recorded on a fund row for ``year``.

    A conversion marker sets a baseline ("accrued N days up to month M");
    every accrual marker after it adds one month. Read from the row's history.
    """
    from leave.models import AvailableLeave

    reasons = (
        AvailableLeave.history.filter(id=row.pk, history_change_reason__startswith="[")
        .order_by("history_date")
        .values_list("history_change_reason", flat=True)
    )
    accrued, periods = 0.0, set()
    for reason in reasons:
        conversion = _CONVERSION_RE.search(reason or "")
        if conversion and int(conversion.group(1)) == year:
            accrued = float(conversion.group(2))
            last = int(conversion.group(4))
            periods = {f"{year}-{m:02d}" for m in range(1, last + 1)}
            continue
        accrual = _ACCRUAL_RE.search(reason or "")
        if accrual and int(accrual.group(1)) == year:
            period = f"{accrual.group(1)}-{accrual.group(2)}"
            if period not in periods:
                periods.add(period)
                accrued += _credited_days(reason)
    return accrued, periods


def conversion_baseline(row, year):
    """``(days, "YYYY-MM", accrued_after)`` of the latest conversion marker of
    ``year`` on a fund row, or ``None``. ``accrued_after``: months credited
    since that marker."""
    from leave.models import AvailableLeave

    reasons = (
        AvailableLeave.history.filter(id=row.pk, history_change_reason__startswith="[")
        .order_by("history_date")
        .values_list("history_change_reason", flat=True)
    )
    baseline, after = None, 0
    for reason in reasons:
        conversion = _CONVERSION_RE.search(reason or "")
        if conversion and int(conversion.group(1)) == year:
            baseline, after = (float(conversion.group(2)), f"{conversion.group(3)}-{conversion.group(4)}"), 0
            continue
        accrual = _ACCRUAL_RE.search(reason or "")
        if baseline and accrual and int(accrual.group(1)) == year:
            after += 1
    return (*baseline, after) if baseline else None


def _credited_days(reason):
    match = re.search(r"\+([\d.]+)", reason or "")
    return float(match.group(1)) if match else 1.0


# -- conversion ----------------------------------------------------------------


@dataclass
class ConversionRow:
    employee: object
    pool_row: object = None
    alias_rows: list = field(default_factory=list)
    entitlement: float = None
    approved: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    current_remaining: float = None
    new_remaining: float = None
    action: str = "unchanged"
    reason: str = ""

    @property
    def approved_total(self):
        return sum(self.approved.values())

    @property
    def pending_total(self):
        return sum(self.pending.values())


@dataclass
class ConversionPlan:
    pool: object
    aliases: list
    year: int
    period: str
    rows: list = field(default_factory=list)
    link_aliases: list = field(default_factory=list)

    @property
    def changes(self):
        return [r for r in self.rows if r.action in ("update", "create", "baseline")]

    @property
    def refused(self):
        return [r for r in self.rows if r.action == "refuse"]


def plan_conversion(*, pool, aliases, year, period, entitlement=None, entitlement_for=None, company_ids=(), employee_ids=()):
    """What converting to one fund would do, per employee. Reads only.

    ``entitlement``: days of the year's fund each employee is entitled to at
    ``period`` (HR's decision); ``entitlement_for`` overrides it per employee
    id. An employee with neither is refused ("Chưa xác định")."""
    from employee.models import Employee
    from leave.models import AvailableLeave, leave_type_shared_balance_problem
    from leave.services import leave_type_matches_company, revoked_pairs

    entitlement_for = dict(entitlement_for or {})
    for alias in aliases:
        if alias.pk == pool.pk:
            raise SharedFundError("the pool can not share with itself")
        if alias.shared_balance_with_id not in (None, pool.pk):
            raise SharedFundError(f"leave type {alias.pk} already shares another fund ({alias.shared_balance_with_id})")
        probe = type(alias)(**{f.attname: getattr(alias, f.attname) for f in alias._meta.concrete_fields})
        probe.shared_balance_with = pool
        problem = leave_type_shared_balance_problem(probe)
        if problem:
            raise SharedFundError(f"leave type {alias.pk}: {problem}")
    plan = ConversionPlan(pool=pool, aliases=list(aliases), year=year, period=period)
    plan.link_aliases = [a for a in aliases if a.shared_balance_with_id != pool.pk]

    employees = Employee._base_manager.filter(is_active=True).select_related("employee_work_info__company_id")
    if company_ids:
        employees = employees.filter(employee_work_info__company_id__in=company_ids)
    if employee_ids:
        employees = employees.filter(pk__in=employee_ids)
    employees = [e for e in employees.order_by("pk") if leave_type_matches_company(pool, e)]
    types = [pool] + list(aliases)
    revoked = revoked_pairs([e.pk for e in employees], [pool.pk]) or {}

    for employee in employees:
        row = ConversionRow(employee=employee)
        row.pool_row = AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id=pool).first()
        row.alias_rows = list(AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id__in=aliases))
        row.approved = requests_days(employee, types, year, ["approved"])
        row.pending = requests_days(employee, types, year, ["requested"])
        row.entitlement = entitlement_for.get(employee.pk, entitlement)
        if row.pool_row is not None:
            row.current_remaining = (row.pool_row.available_days or 0) + (row.pool_row.carryforward_days or 0)
        plan.rows.append(row)

        if row.entitlement is None:
            row.action, row.reason = "refuse", "Chưa xác định mức quyền lợi (cần --entitlement / --entitlement-for)"
            continue
        if row.pool_row is None and (employee.pk, pool.pk) in revoked:
            row.action, row.reason = "refuse", "Quỹ này từng bị HR thu hồi — HR quyết định, không tự mở lại"
            continue
        if row.pool_row is not None and (row.pool_row.carryforward_days or 0):
            row.action, row.reason = "refuse", "Có ngày chuyển kỳ — HR xử lý tay"
            continue
        row.new_remaining = round(row.entitlement - row.approved_total, 3)
        if row.new_remaining < 0:
            row.action, row.reason = "refuse", (
                f"Đã dùng {row.approved_total:g} ngày, vượt quyền lợi {row.entitlement:g} — HR quyết định"
            )
            continue
        if row.pool_row is None:
            row.action = "create"
            continue
        baseline = conversion_baseline(row.pool_row, year)
        if baseline and baseline[2]:
            row.action, row.reason = "refuse", (
                f"Đã tích lũy {baseline[2]} tháng sau lần chuyển đổi {baseline[1]} — không chạy lại chuyển đổi"
            )
            continue
        same_number = row.current_remaining == row.new_remaining
        if same_number and not any(r.is_active for r in row.alias_rows) and baseline and baseline[:2] == (row.entitlement, period):
            row.action = "unchanged"
        elif same_number and not any(r.is_active for r in row.alias_rows):
            # Days already right: write only the baseline marker the monthly
            # accrual continues from — without it, next month would push the
            # year past its cap.
            row.action = "baseline"
        else:
            row.action = "update"
    return plan


def apply_conversion(plan, *, expect_count):
    """Link the types and write the one fund per employee, all or nothing."""
    from leave.models import AvailableLeave

    if plan.refused:
        raise SharedFundError(
            f"{len(plan.refused)} employee(s) refused (see the dry run) — nothing written"
        )
    if expect_count != len(plan.changes):
        raise SharedFundError(f"--expect-count {expect_count} but {len(plan.changes)} change(s) — nothing written")

    with transaction.atomic():
        for alias in plan.link_aliases:  # validated by plan_conversion
            type(alias)._base_manager.filter(pk=alias.pk).update(shared_balance_with=plan.pool)
            alias.shared_balance_with = plan.pool
        for row in plan.rows:
            if row.action not in ("update", "create", "baseline"):
                continue
            reason = CONVERSION_MARK.format(year=plan.year, days=row.entitlement, period=plan.period)
            if row.action == "create":
                fund = AvailableLeave(employee_id=row.employee, leave_type_id=plan.pool, available_days=row.new_remaining)
            else:
                fund = AvailableLeave._base_manager.select_for_update().get(pk=row.pool_row.pk)
                fund.available_days = row.new_remaining
            fund._change_reason = reason[:100]
            fund.save()
            for old in row.alias_rows:
                if old.is_active:
                    old = AvailableLeave._base_manager.select_for_update().get(pk=old.pk)
                    old.is_active = False
                    old._change_reason = MERGED_MARK.format(year=plan.year, pool=plan.pool.pk)[:100]
                    old.save()
    return plan.changes


# -- monthly accrual -----------------------------------------------------------


@dataclass
class AccrualRow:
    employee: object
    row: object = None
    accrued: float = 0
    credit: float = 0
    action: str = "credit"
    reason: str = ""


def plan_accrual(*, pool, year, month, days=1.0, cap=12.0, company_ids=(), employee_ids=(), include_missing_joining_date=False):
    """Who gets ``days`` for ``year``-``month`` on the ``pool`` fund. Reads only.

    Skipped, with the reason: no fund row yet; joined after that month;
    joining date missing (unless ``include_missing_joining_date`` — an HR
    decision, never a guessed date); no starting point for the year (a
    conversion marker); month already credited; cap reached."""
    from employee.models import Employee
    from leave.models import AvailableLeave
    from leave.services import leave_type_matches_company

    period = f"{year}-{month:02d}"
    month_end = date(year + (month == 12), month % 12 + 1, 1)
    employees = Employee._base_manager.filter(is_active=True).select_related("employee_work_info")
    if company_ids:
        employees = employees.filter(employee_work_info__company_id__in=company_ids)
    if employee_ids:
        employees = employees.filter(pk__in=employee_ids)
    rows = []
    for employee in employees.order_by("pk"):
        if not leave_type_matches_company(pool, employee):
            continue
        entry = AccrualRow(employee=employee)
        rows.append(entry)
        entry.row = AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id=pool).first()
        joined = getattr(getattr(employee, "employee_work_info", None), "date_joining", None)
        if entry.row is None:
            entry.action, entry.reason = "skip", "Chưa có quỹ"
            continue
        if joined is None and not include_missing_joining_date:
            entry.action, entry.reason = "skip", "Thiếu ngày vào làm — chưa xác định"
            continue
        if joined is not None and joined >= month_end:
            entry.action, entry.reason = "skip", "Vào làm sau tháng này"
            continue
        if conversion_baseline(entry.row, year) is None:
            # Without a starting point for the year, what was already granted
            # is unknown: crediting could push a fund given 12 upfront to 13.
            entry.action, entry.reason = "skip", "Chưa có mốc của năm — chạy convert_shared_annual_balance trước"
            continue
        entry.accrued, periods = accrual_state(entry.row, year)
        if period in periods:
            entry.action, entry.reason = "skip", "Đã tích lũy tháng này"
            continue
        entry.credit = round(max(min(days, cap - entry.accrued), 0), 3)
        if entry.credit <= 0:
            entry.action, entry.reason = "skip", f"Đã đủ {cap:g} ngày của năm"
    return rows


def apply_accrual(rows, *, year, month, expect_count):
    """Credit the planned rows, all or nothing; a month is never credited twice."""
    from leave.models import AvailableLeave

    credits = [r for r in rows if r.action == "credit"]
    if expect_count != len(credits):
        raise SharedFundError(f"--expect-count {expect_count} but {len(credits)} credit(s) — nothing written")
    period = f"{year}-{month:02d}"
    with transaction.atomic():
        for entry in credits:
            fund = AvailableLeave._base_manager.select_for_update().get(pk=entry.row.pk)
            if period in accrual_state(fund, year)[1]:
                raise SharedFundError(f"employee={entry.employee.pk}: {period} credited meanwhile — rolled back")
            fund.available_days = round((fund.available_days or 0) + entry.credit, 3)
            fund._change_reason = f"{ACCRUAL_MARK.format(period=period)} +{entry.credit:g}"[:100]
            fund.save()
    return credits
