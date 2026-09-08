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


def ensure_available_leave(employee, leave_type):
    """Give ``employee`` their ``leave_type`` balance row, once.

    The single place an ``AvailableLeave`` is opened automatically. It applies
    exactly what the manual "Assign Leave" screens do — ``available_days``
    seeded from ``LeaveType.total_days``, ``carryforward_days`` left at its
    default, and ``reset_date`` / ``expired_date`` / ``total_leave_days``
    derived by ``AvailableLeave.pre_save_processing()`` inside ``save()`` — so
    an auto-assigned row is indistinguishable from a hand-assigned one.

    Returns ``(available_leave, created)``. Idempotent and safe to call
    repeatedly: an existing row is returned untouched, and its balance is never
    read, reset or overwritten.

    Skips silently (returning ``(None, False)``) when the employee is inactive,
    when the type belongs to another company, or when the type's own conditions
    (gender, department, once-per-employment, …) rule the employee out — the
    same gate ``leave_assign`` applies before creating a row by hand.
    """
    from leave.models import AvailableLeave

    if employee is None or leave_type is None:
        return None, False
    if not getattr(employee, "is_active", True):
        return None, False
    if not getattr(leave_type, "is_active", True):
        return None, False
    if not leave_type_matches_company(leave_type, employee):
        return None, False

    existing = AvailableLeave.objects.filter(
        employee_id=employee, leave_type_id=leave_type
    ).first()
    if existing is not None:
        return existing, False

    is_eligible, _msg = evaluate_leave_type_conditions(leave_type, employee)
    if not is_eligible:
        return None, False

    available_leave = AvailableLeave(
        employee_id=employee,
        leave_type_id=leave_type,
        available_days=leave_type.total_days,
    )
    # save() runs pre_save_processing(), which is what fills reset_date,
    # expired_date and total_leave_days — the manual path relies on the same
    # method, so nothing is recomputed differently here.
    available_leave.save()
    return available_leave, True


def assignable_employees(leave_type):
    """Active employees whose company is offered ``leave_type``."""
    from employee.models import Employee

    return [
        employee
        for employee in Employee.objects.filter(is_active=True).select_related(
            "employee_work_info__company_id"
        )
        if leave_type_matches_company(leave_type, employee)
    ]


def assignable_leave_types(employee):
    """Active leave types offered to ``employee``'s company."""
    from leave.models import LeaveType

    return [
        leave_type
        for leave_type in LeaveType.objects.filter(is_active=True)
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
