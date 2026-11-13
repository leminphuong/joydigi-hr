"""Safe context for HR mail templates (security fix 2026-11-13).

HR mail templates (``JoydigiMailTemplate`` bodies, and the free text typed in
the "send mail" dialogs) are rendered as Django templates so that
placeholders such as ``{{ instance.get_full_name }}`` work. Rendering them
against real model instances let a template walk the ORM to anything the
employee links to, for example
``{{ instance.employee_user_id.password }}`` (the password hash) or bank
details.

These helpers give the template plain strings only. Every placeholder the
templates are meant to use keeps working; anything else renders as an empty
string, because a dict of strings has nothing behind it to traverse.
"""

from __future__ import annotations


def _text(value) -> str:
    if value is None:
        return ""
    if callable(value):
        try:
            value = value()
        except Exception:  # a placeholder must never break a mail
            return ""
    return "" if value is None else str(value)


def _work_info(employee):
    try:
        return employee.employee_work_info
    except Exception:  # no work information yet
        return None


def employee_mail_context(employee) -> dict:
    """The placeholders one employee exposes to a mail template."""
    if employee is None:
        return {}
    work = _work_info(employee)
    manager = getattr(work, "reporting_manager_id", None) if work else None
    data = {
        "id": _text(employee.pk),
        "pk": _text(employee.pk),
        "badge_id": _text(employee.badge_id),
        "employee_first_name": _text(employee.employee_first_name),
        "employee_last_name": _text(employee.employee_last_name),
        "get_full_name": _text(employee.get_full_name),
        "email": _text(getattr(work, "email", None) or employee.email),
        "phone": _text(employee.phone),
    }
    for name in ("get_company", "get_job_position", "get_department", "get_shift",
                 "get_work_type", "get_employee_type", "get_mail", "get_email"):
        data[name] = _text(getattr(employee, name, None))
    data["get_reporting_manager"] = _text(manager.get_full_name) if manager else ""
    data["employee_work_info"] = {
        "email": _text(getattr(work, "email", None)),
        "mobile": _text(getattr(work, "mobile", None)),
        "date_joining": _text(getattr(work, "date_joining", None)),
        "company_id": _text(getattr(work, "company_id", None)),
        "department_id": _text(getattr(work, "department_id", None)),
        "job_position_id": _text(getattr(work, "job_position_id", None)),
        "job_role_id": _text(getattr(work, "job_role_id", None)),
        "shift_id": _text(getattr(work, "shift_id", None)),
        "work_type_id": _text(getattr(work, "work_type_id", None)),
        "employee_type_id": _text(getattr(work, "employee_type_id", None)),
        "reporting_manager_id": _text(manager.get_full_name) if manager else "",
        "location": _text(getattr(work, "location", None)),
    } if work else {}
    return data


def mail_template_context(employee, sender) -> dict:
    """Context for rendering an HR mail template: the recipient as
    ``instance`` / ``model_instance`` and the sender as ``self``."""
    recipient = employee_mail_context(employee)
    return {
        "instance": recipient,
        "model_instance": recipient,
        "self": employee_mail_context(sender),
    }
