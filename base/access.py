"""Object-level access rules shared by the web views and the mobile API
(security fix 2026-11-13).

The mobile API used to treat "manager of someone" as "manager of everyone".
These helpers answer the real question — may this user act on *this*
employee's record? — with the same rule the hardened web paths already use
(attendance/views/requests.py `_can_review_attendance_request`,
leave/views.py approve, leave/decorators.py).
"""

from __future__ import annotations


def actor_of(user):
    """The caller's employee record, or None."""
    return getattr(user, "employee_get", None)


def is_direct_manager(user, employee) -> bool:
    """True when the caller is `employee`'s own reporting manager."""
    from employee.models import EmployeeWorkInformation

    actor = actor_of(user)
    if actor is None or employee is None:
        return False
    work_info = (
        EmployeeWorkInformation.objects.filter(employee_id=employee)
        .only("reporting_manager_id")
        .first()
    )
    return work_info is not None and work_info.reporting_manager_id_id == actor.pk


def can_manage_employee(user, employee, perm=None, *, allow_self=False) -> bool:
    """May the caller act on `employee`'s record?

    Yes for a superuser. Otherwise never on one's own record unless
    `allow_self`; then yes for a check-in admin, a holder of the Django
    permission `perm`, or the employee's own reporting manager. Managing
    somebody else is not enough.
    """
    from base.roles import is_checkin_admin

    if employee is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    actor = actor_of(user)
    if actor is not None and employee.pk == actor.pk and not allow_self:
        return False
    if is_checkin_admin(user):
        return True
    if perm and user.has_perm(perm):
        return True
    return is_direct_manager(user, employee)
