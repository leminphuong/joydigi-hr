"""
leave/services.py

Centralised business-logic helpers for the leave app.
Condition evaluation follows the same pattern as payroll allowance eligibility checks.
"""

from django.utils.translation import gettext_lazy as _


def evaluate_leave_type_conditions(leave_type, employee):
    """
    Evaluate all conditions configured on a LeaveType against an employee.

    Returns a (is_eligible, error_message) tuple.  When all conditions pass,
    returns (True, None).  On the first failing condition it returns
    (False, <translated error string>).

    Usage::

        is_eligible, msg = evaluate_leave_type_conditions(leave_type, employee)
        if not is_eligible:
            raise ValidationError(msg)
    """
    from leave.models import AvailableLeave

    for condition in leave_type.conditions.all():
        ctype = condition.condition_type

        if ctype == "gender":
            emp_gender = (getattr(employee, "gender", None) or "").lower()
            required_gender = (condition.value or "").lower()
            if emp_gender and required_gender and emp_gender != required_gender:
                return False, _(
                    "This leave type is restricted to {gender} employees only."
                ).format(gender=condition.value)

        elif ctype == "once_per_employment":
            already_assigned = AvailableLeave.objects.filter(
                employee_id=employee,
                leave_type_id=leave_type,
            ).exists()
            if already_assigned:
                return False, _(
                    "'{leave_type}' can only be assigned once per employment and has already been assigned to this employee."
                ).format(leave_type=leave_type.name)

        elif ctype == "marital_status":
            emp_status = (getattr(employee, "marital_status", None) or "").lower()
            required_status = (condition.value or "").lower()
            if emp_status and required_status and emp_status != required_status:
                return False, _(
                    "This leave type is restricted to employees with marital status: {status}."
                ).format(status=condition.value)

        elif ctype == "nationality":
            emp_country = (getattr(employee, "country", None) or "").lower()
            required_country = (condition.value or "").lower()
            if emp_country and required_country and emp_country != required_country:
                return False, _(
                    "This leave type is restricted to employees with nationality: {nationality}."
                ).format(nationality=condition.value)

        elif ctype == "department":
            dept = None
            work_info = getattr(employee, "employee_work_info", None)
            if work_info:
                dept_obj = getattr(work_info, "department_id", None)
                if dept_obj:
                    dept = str(dept_obj).lower()
            required_dept = (condition.value or "").lower()
            if dept and required_dept and dept != required_dept:
                return False, _(
                    "This leave type is restricted to employees in the {department} department."
                ).format(department=condition.value)

        elif ctype == "employment_type":
            emp_type = None
            work_info = getattr(employee, "employee_work_info", None)
            if work_info:
                emp_type_obj = getattr(work_info, "employee_type_id", None)
                if emp_type_obj:
                    emp_type = str(emp_type_obj).lower()
            required_type = (condition.value or "").lower()
            if emp_type and required_type and emp_type != required_type:
                return False, _(
                    "This leave type is restricted to employees with employment type: {emp_type}."
                ).format(emp_type=condition.value)

        elif ctype == "grade":
            grade = None
            work_info = getattr(employee, "employee_work_info", None)
            if work_info:
                grade_obj = getattr(work_info, "job_position_id", None)
                if grade_obj:
                    grade = str(grade_obj).lower()
            required_grade = (condition.value or "").lower()
            if grade and required_grade and grade != required_grade:
                return False, _(
                    "This leave type is restricted to employees with grade: {grade}."
                ).format(grade=condition.value)

    return True, None


def employee_company(employee):
    """The company an employee belongs to, or None when work info is missing."""
    work_info = getattr(employee, "employee_work_info", None)
    return getattr(work_info, "company_id", None) if work_info else None


def leave_type_matches_company(leave_type, employee):
    """Whether ``leave_type`` is offered to ``employee``'s company.

    A type with no company is global and belongs to everyone — the same
    ``Q(company_id=company) | Q(company_id__isnull=True)`` rule
    ``LeaveTypeGetCreateAPIView`` and ``JoydigiCompanyManager`` already use.
    A type that *is* scoped never crosses into another company.
    """
    type_company = getattr(leave_type, "company_id", None)
    if type_company is None:
        return True
    return type_company == employee_company(employee)


# -- safety rails for opening balances automatically (2026-11-11) ------------
#
# Two things must never happen when a balance is opened without HR typing a
# number: re-opening a balance HR deliberately took away, and seeding a number
# of days that can not be right. Both are checked here, once, for every path
# (signals, the scheduled sync, the backfill command).

#: Most days a limited leave type may hand out automatically: a year.
MAX_AUTOMATIC_DAYS = 366


def revoked_pairs(employee_ids, leave_type_ids):
    """``{(employee_id, leave_type_id): deleted_at}`` for balances that were
    deleted — a deletion leaves a ``-`` record in the balance history.

    ``None`` when the history can not be read: the caller must then refuse
    to tell "never opened" from "revoked" and open nothing.
    """
    from leave.models import AvailableLeave

    history = getattr(AvailableLeave, "history", None)
    if history is None:
        return None
    revoked = {}
    rows = history.filter(
        history_type="-", employee_id__in=list(employee_ids), leave_type_id__in=list(leave_type_ids)
    ).values_list("employee_id", "leave_type_id", "history_date")
    for employee_id, leave_type_id, deleted_at in rows:
        key = (employee_id, leave_type_id)
        revoked[key] = max(deleted_at, revoked.get(key, deleted_at))
    return revoked


def was_revoked(employee, leave_type):
    """Whether HR deleted this employee's balance of this type before (or the
    history can not tell — treated the same, so nothing is re-opened)."""
    revoked = revoked_pairs([employee.pk], [leave_type.pk])
    return revoked is None or (employee.pk, leave_type.pk) in revoked


def leave_type_quota_problem(leave_type):
    """Why ``leave_type``'s configured days can not seed a balance on their
    own (``None`` when they can).

    ``total_days`` is the number every automatic path seeds. An unlimited
    type stores a sentinel there and is fine; a limited one must hand out
    more than 0 and at most a year.
    """
    if not getattr(leave_type, "limit_leave", True):
        return None
    total = getattr(leave_type, "total_days", None)
    if total == 0 and leave_type.pk and type(leave_type)._base_manager.filter(shared_balance_with=leave_type).exists():
        return None  # a shared fund may open empty and fill by monthly accrual
    if total is None or total <= 0:
        return f"total_days={total!r}: chưa có số ngày cấp"
    if total > MAX_AUTOMATIC_DAYS:
        return f"total_days={total:g}: vượt {MAX_AUTOMATIC_DAYS} ngày"
    return None


def ensure_available_leave(employee, leave_type, available_days=None, *, reason=None):
    """Give ``employee`` their ``leave_type`` balance row, once.

    The single place an ``AvailableLeave`` is opened automatically. It applies
    exactly what the manual "Assign Leave" screens do — ``available_days``
    seeded from ``LeaveType.total_days``, ``carryforward_days`` left at its
    default, and ``reset_date`` / ``expired_date`` / ``total_leave_days``
    derived by ``AvailableLeave.pre_save_processing()`` inside ``save()`` — so
    an auto-assigned row is indistinguishable from a hand-assigned one.

    ``available_days`` seeds a new row with an amount HR approved instead of
    the type's ``total_days`` (``backfill_available_leave --grant``). It never
    changes an existing row nor the leave type, and the signals never pass it,
    so automatic assignment still seeds ``total_days``.

    Returns ``(available_leave, created)``. Idempotent and safe to call
    repeatedly: an existing row is returned untouched, and its balance is never
    read, reset or overwritten.

    Skips silently (returning ``(None, False)``) when the employee is inactive,
    when the type belongs to another company, or when the type's own conditions
    (gender, department, once-per-employment, …) rule the employee out — the
    same gate ``leave_assign`` applies before creating a row by hand.

    Since 2026-11-11 it also skips:

    * a pair HR revoked — its deletion is in the balance history — or whose
      history can not be read. Re-opening it is HR's call, by hand;
    * seeding from ``total_days`` when ``leave_type_quota_problem`` objects
      (an explicit ``available_days`` is HR's own number and is used as is).

    ``reason`` is stored with the balance history (``history_change_reason``)
    so every automatic opening says what opened it.
    """
    from leave.models import AvailableLeave

    if employee is None or leave_type is None:
        return None, False
    if not getattr(employee, "is_active", True):
        return None, False
    if not getattr(leave_type, "is_active", True):
        return None, False
    if getattr(leave_type, "shared_balance_with_id", None):
        # Uses another type's fund: opening a row would create a second,
        # independent fund (2026-11-11). The fund's own type is opened instead.
        return None, False
    if not leave_type_matches_company(leave_type, employee):
        return None, False

    # Read through the base manager: the default managers filter by the company
    # selected in the current HR web session, so who saved what (and which
    # company they had picked) used to decide who got a leave type.
    existing = _existing_available_leave(employee, leave_type)
    if existing is not None:
        return existing, False

    is_eligible, _msg = evaluate_leave_type_conditions(leave_type, employee)
    if not is_eligible:
        return None, False
    if was_revoked(employee, leave_type):
        _logger().info(
            "leave balance not re-opened (revoked before): employee=%s leave_type=%s", employee.pk, leave_type.pk
        )
        return None, False
    if available_days is None:
        problem = leave_type_quota_problem(leave_type)
        if problem:
            _logger().warning(
                "leave balance not opened (configured days rejected: %s): employee=%s leave_type=%s",
                problem, employee.pk, leave_type.pk,
            )
            return None, False

    available_leave = AvailableLeave(
        employee_id=employee,
        leave_type_id=leave_type,
        available_days=leave_type.total_days if available_days is None else available_days,
    )
    available_leave._change_reason = (reason or "Tự động mở quỹ phép")[:100]
    # save() runs pre_save_processing(), which is what fills reset_date,
    # expired_date and total_leave_days — the manual path relies on the same
    # method, so nothing is recomputed differently here.
    #
    # Two admins (or an admin and a signal) can both see "no row" at once.
    # The (leave_type_id, employee_id) unique constraint lets only one
    # insert win; the other rolls back its savepoint and returns the winner.
    from django.db import IntegrityError, transaction

    try:
        with transaction.atomic():
            available_leave.save()
    except IntegrityError:
        winner = _existing_available_leave(employee, leave_type)
        if winner is None:
            raise
        return winner, False
    _logger().info(
        "leave balance opened: employee=%s leave_type=%s days=%s reason=%s",
        employee.pk, leave_type.pk, available_leave.available_days, available_leave._change_reason,
    )
    return available_leave, True


def _logger():
    import logging

    return logging.getLogger("leave.sync")


def assignable_employees(leave_type):
    """Active employees whose company is offered ``leave_type``."""
    from employee.models import Employee

    return [
        employee
        for employee in Employee._base_manager.filter(is_active=True).select_related(
            "employee_work_info__company_id"
        )
        if leave_type_matches_company(leave_type, employee)
    ]


def assignable_leave_types(employee):
    """Active leave types offered to ``employee``'s company that hold their
    own balance (a type sharing another's fund has none to open)."""
    from leave.models import LeaveType

    return [
        leave_type
        for leave_type in LeaveType._base_manager.filter(is_active=True, shared_balance_with__isnull=True)
        if leave_type_matches_company(leave_type, employee)
    ]


def has_sufficient_leave_balance(available_leave, requested_days) -> bool:
    """
    Gate used by leave_request_approve before deducting balance.

    Returns True when available_days + carryforward_days covers requested_days.
    """
    total = (available_leave.available_days or 0) + (
        available_leave.carryforward_days or 0
    )
    return total >= float(requested_days or 0)


def get_condition_display_choices():
    """
    Returns a dict of {condition_type: suggested value choices} for UI hints.
    """
    return {
        "gender": [("male", _("Male")), ("female", _("Female")), ("other", _("Other"))],
        "marital_status": [
            ("single", _("Single")),
            ("married", _("Married")),
            ("divorced", _("Divorced")),
        ],
        "once_per_employment": [],
        "nationality": [],
        "department": [],
        "employment_type": [],
        "grade": [],
        "service_duration": [],
    }


def compensatory_leave_type_for(selected_company):
    """The compensatory leave type, created once.

    ``LeaveType.clean`` allows a single compensatory type, but the settings
    pages used to ``get_or_create`` one per selected company *and* one with no
    company when "all" was selected, so visiting them in both modes produced
    two "Compensatory Leave Type" rows that every employee then received.
    Reuse whichever exists — an active one first, then the oldest, so once an
    admin switches off the duplicates the one left active is the one used —
    and only when there is none, create it for the selected company (or with
    no company for "all")."""
    from leave.models import LeaveType

    existing = (
        LeaveType._base_manager.filter(is_compensatory_leave=True)
        .order_by("-is_active", "id")
        .first()
    )
    if existing is not None:
        return existing
    company_id = None
    if selected_company and selected_company != "all":
        try:
            company_id = int(selected_company)
        except (TypeError, ValueError):
            company_id = None
    return LeaveType.objects.create(
        is_compensatory_leave=True,
        company_id_id=company_id,
        name="Compensatory Leave Type",
        payment="paid",
    )


def requestable_available_leave(employee):
    """An employee's balances for the leave types they may request: active
    types only, in name order (stable pages), whatever company an HR session
    has selected."""
    from leave.models import AvailableLeave

    from django.db.models import Q

    company = employee_company(employee)
    offered = Q(leave_type_id__company_id__isnull=True)
    if company is not None:
        offered |= Q(leave_type_id__company_id=company)
    return (
        AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id__is_active=True)
        .filter(offered)
        # A sharing type's own old row (from before the fund was shared) is
        # history, not a second fund: its requests use the shared balance.
        .filter(leave_type_id__shared_balance_with__isnull=True)
        .select_related("leave_type_id")
        .order_by("leave_type_id__name", "leave_type_id_id")
    )


# -- one fund, several request types (2026-11-11) ------------------------------

#: Appended to the name of every type drawing on a shared fund, in what the
#: app receives: released app builds show only the name and the days, so the
#: name is what tells "one fund" from "two funds of 11 days".
SHARED_FUND_SUFFIX = " · chung quỹ phép năm"


class SharedBalanceOption:
    """A request type shown with the balance row of the fund it draws on.

    Looks like an ``AvailableLeave`` to serializers and templates: every
    attribute is the fund row's, except ``leave_type_id`` (the type to
    request) and ``shared_fund`` (what the fund is shared with)."""

    def __init__(self, row, leave_type, fund):
        self._row = row
        self.leave_type_id = leave_type
        self.shared_fund = fund

    def __getattr__(self, name):
        return getattr(self._row, name)


def shared_fund_types(pool):
    """``pool`` and the active types drawing on it, by name."""
    from leave.models import LeaveType

    users = list(LeaveType._base_manager.filter(shared_balance_with=pool, is_active=True).order_by("name", "pk"))
    return [pool] + users if users else []


def pending_shared_days(employee, pool, exclude_request=None):
    """Days of the employee's requests still waiting for a decision, over
    every type of ``pool``'s fund."""
    from django.db.models import Sum

    from leave.models import LeaveRequest

    type_ids = [t.pk for t in shared_fund_types(pool)] or [pool.pk]
    pending = LeaveRequest._base_manager.filter(
        employee_id=employee, leave_type_id__in=type_ids, status="requested"
    )
    if exclude_request is not None and getattr(exclude_request, "pk", None):
        pending = pending.exclude(pk=exclude_request.pk)
    return float(pending.aggregate(total=Sum("requested_days"))["total"] or 0)


def requestable_leave_options(employee):
    """What the employee may request, one entry per request type.

    Ordinary balances as they are; for a shared fund, the fund row plus one
    ``SharedBalanceOption`` per type drawing on it — all carrying the SAME
    remaining days (one fund), each named with ``SHARED_FUND_SUFFIX``."""
    import copy

    options = []
    for row in requestable_available_leave(employee):
        types = shared_fund_types(row.leave_type_id)
        if not types:
            options.append(row)
            continue
        fund = {"fund_type": row.leave_type_id, "types": types}
        for leave_type in types:
            if not leave_type_offered_to(leave_type, employee_company(employee)):
                continue
            eligible, _msg = evaluate_leave_type_conditions(leave_type, employee)
            if not eligible:
                continue
            shown = copy.copy(leave_type)  # never saved: only the name in the answer differs
            shown.name = f"{leave_type.name}{SHARED_FUND_SUFFIX}"
            options.append(SharedBalanceOption(row, shown, fund))
    # The balances' own order (by name, as before); the types sharing a fund
    # follow the fund's row, so pages stay stable.
    return options


def shared_fund_summaries(employee, year=None):
    """One summary per shared fund the employee holds, for the HR screens:
    the request types using it, days left, used (approved) and pending this
    year, and what was granted/accrued so far (left + used)."""
    from datetime import date

    from django.db.models import Sum

    from leave.models import LeaveRequest

    year = year or date.today().year
    summaries = []
    for row in requestable_available_leave(employee):
        types = shared_fund_types(row.leave_type_id)
        if not types:
            continue
        used = LeaveRequest._base_manager.filter(
            employee_id=employee, leave_type_id__in=[t.pk for t in types], status="approved", start_date__year=year
        ).aggregate(total=Sum("requested_days"))["total"] or 0
        remaining = (row.available_days or 0) + (row.carryforward_days or 0)
        summaries.append(
            {
                "title": f"Quỹ phép năm {year}",
                "types": types,
                "remaining": remaining,
                "used": float(used),
                "pending": pending_shared_days(employee, row.leave_type_id),
                "granted": remaining + float(used),
            }
        )
    return summaries


def _existing_available_leave(employee, leave_type):
    """The employee's balance row for ``leave_type``, read without the HR
    session's company filter (``None`` when there is none)."""
    from leave.models import AvailableLeave

    return AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id=leave_type).first()


# -- automatic sync (2026-11-09) ----------------------------------------------
#
# A balance (AvailableLeave) is what lets an employee request a leave type. It
# is opened automatically, once, for every active employee the type is
# offered to (same company or a company-less type, conditions met), whenever
# something makes them newly eligible: the type is created, re-activated,
# moved to another company or to all companies, or its conditions change; the
# employee is created, re-activated or transferred. Opening is idempotent and
# never touches an existing balance: saving a type again does not re-add
# days, and nothing is ever deleted, so used days and past requests stay.


def sync_leave_type(leave_type, reason="Tự động: loại phép mới / bật lại / đổi phạm vi"):
    """Open ``leave_type`` for every eligible employee who has no balance yet.
    Returns how many balances were opened."""
    if leave_type is None or not getattr(leave_type, "is_active", False):
        return 0
    opened = 0
    for employee in assignable_employees(leave_type):
        _row, created = ensure_available_leave(employee, leave_type, reason=reason)
        opened += int(bool(created))
    return opened


def sync_employee(employee, reason="Tự động: nhân viên mới / đi làm lại / đổi điều kiện"):
    """Open every leave type ``employee`` is now eligible for. Returns how
    many balances were opened."""
    if employee is None or not getattr(employee, "is_active", False):
        return 0
    opened = 0
    for leave_type in assignable_leave_types(employee):
        _row, created = ensure_available_leave(employee, leave_type, reason=reason)
        opened += int(bool(created))
    return opened


# -- what the employee may request, in words they can act on -------------------

NO_EMPLOYEE_MESSAGE = (
    "Tài khoản của bạn chưa gắn với hồ sơ nhân viên nên chưa tạo được đơn. "
    "Vui lòng liên hệ HR."
)
NO_WORK_INFO_MESSAGE = (
    "Hồ sơ của bạn chưa có thông tin công việc nên chưa tạo được đơn nghỉ phép. "
    "Vui lòng liên hệ HR để cập nhật hồ sơ."
)
NO_COMPANY_MESSAGE = (
    "Hồ sơ công việc của bạn chưa có công ty nên chưa tạo được đơn nghỉ phép. "
    "Vui lòng liên hệ HR để cập nhật hồ sơ."
)
NOT_ASSIGNED_SHORT = "Chưa được cấp. Vui lòng liên hệ HR."
NO_BALANCE_MESSAGE = "Hết số ngày phép của loại này."


def employee_has_work_info(employee):
    """Whether ``employee`` has an ``EmployeeWorkInformation`` row (the
    approval chain and the company are read from it)."""
    return getattr(employee, "employee_work_info", None) is not None


def leave_type_offered_to(leave_type, company):
    """Whether ``leave_type`` is offered to ``company`` (its own or shared)."""
    type_company = getattr(leave_type, "company_id_id", None)
    return type_company is None or (company is not None and type_company == getattr(company, "pk", company))


def leave_option_status(available_leave):
    """``(status, requestable, message)`` for one balance row in the app list.

    ``available``: can be requested. ``no_balance``: a limited type whose
    remaining days (available + carried forward) are used up."""
    leave_type = available_leave.leave_type_id
    remaining = (available_leave.available_days or 0) + (available_leave.carryforward_days or 0)
    if getattr(leave_type, "limit_leave", True) and remaining <= 0:
        return "no_balance", False, NO_BALANCE_MESSAGE
    return "available", True, None


def unassigned_eligible_leave_types(employee):
    """Active types offered to the employee's company that they meet the
    conditions of, but have no balance for (data from before automatic sync,
    or a balance an admin removed). Shown in the app as 'contact HR'."""
    from leave.models import AvailableLeave

    # An employee whose work information has no company still gets the
    # shared (company-less) types: assignable_leave_types() already applies
    # that rule, so only a missing work information means "nothing".
    if not employee_has_work_info(employee):
        return []
    opened = set(
        AvailableLeave._base_manager.filter(employee_id=employee).values_list("leave_type_id", flat=True)
    )
    missing = []
    company = employee_company(employee)
    for leave_type in assignable_leave_types(employee):
        if leave_type.pk in opened:
            continue
        eligible, _msg = evaluate_leave_type_conditions(leave_type, employee)
        if not eligible:
            continue
        missing.append(leave_type)
        # Its fund missing: every type drawing on that fund is missing too.
        for user in shared_fund_types(leave_type)[1:]:
            if leave_type_offered_to(user, company) and evaluate_leave_type_conditions(user, employee)[0]:
                missing.append(user)
    return sorted(missing, key=lambda item: (item.name or "").lower())


def leave_request_problem(employee, leave_type):
    """Why ``employee`` cannot request ``leave_type`` at all, as a sentence for
    the employee (``None`` when nothing stands in the way). Dates, balance
    and overlaps are checked afterwards by the existing validation."""
    if employee is None:
        return NO_EMPLOYEE_MESSAGE
    if not employee_has_work_info(employee):
        return NO_WORK_INFO_MESSAGE
    if leave_type is None:
        return "Vui lòng chọn loại phép."
    company = employee_company(employee)
    name = leave_type.name
    if not leave_type.is_active:
        return f"Loại phép «{name}» đã ngừng áp dụng. Vui lòng chọn loại phép khác hoặc liên hệ HR."
    if company is None and not leave_type_offered_to(leave_type, None):
        # Shared types stay requestable, as before; only a company's own
        # type needs the company on the work information.
        return (
            f"Hồ sơ công việc của bạn chưa có công ty nên chưa dùng được loại phép «{name}». "
            "Vui lòng liên hệ HR để cập nhật hồ sơ."
        )
    if not leave_type_offered_to(leave_type, company):
        return (
            f"Loại phép «{name}» không áp dụng cho công ty của bạn. "
            "Vui lòng liên hệ HR nếu bạn cho rằng đây là nhầm lẫn."
        )
    from leave.models import balance_leave_type

    if _existing_available_leave(employee, balance_leave_type(leave_type)) is None:
        return f"Bạn chưa được cấp loại phép «{name}». Vui lòng liên hệ HR để được cấp."
    return None
