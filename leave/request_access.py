"""Who may see or delete one leave request (2026-11-14).

Several web views took a leave id from the browser and acted on it after
checking only "is logged in" or "manages *somebody*":

* ``user-request-bulk-delete/`` deleted any pending leave by id;
* ``user-request-one/<id>/`` showed any leave;
* ``one-request-view/<id>/``, ``leave-requests-detail-view/<pk>/``,
  ``request-delete/<id>/`` and ``leave-request-bulk-delete/`` let in any
  reporting manager and then acted on any leave of the company.

The rules here are the ones the mobile API already applied
(``_may_handle`` in joydigi_api, ``base.access.can_manage_employee``), plus
the company of the employee:

* one's OWN leave;
* or a leave of an employee the caller may manage — superuser, the
  employee's own reporting manager, or HR/Admin who hold the leave
  permission or the admin role *in the employee's company* (one of their
  companies; group permissions count per company). The mobile API needs
  this check most: the company-scoped managers do not filter JWT requests,
  so a permission held in another company used to reach every company's
  leaves;
* for viewing only, also a leave the caller approves as a step manager
  (multi-step approval).

Callers find the leave through the company-scoped manager first (who may
see it at all), then check here. A delete is decided on the row locked in
the same transaction as the delete: the owner, the caller's right and the
"still waiting" status are read again under the lock, so nothing the
browser sent and nothing read earlier decides it.
"""

from __future__ import annotations

import json

from django.db import transaction

from base.access import actor_of, can_manage_employee, is_direct_manager

VIEW_PERM = "leave.view_leaverequest"
DELETE_PERM = "leave.delete_leaverequest"


def _company_id(employee):
    from employee.models import EmployeeWorkInformation

    return (
        EmployeeWorkInformation._base_manager.filter(employee_id=employee)
        .values_list("company_id", flat=True)
        .first()
    )


def holds_right_in_company(user, company_id, perm) -> bool:
    """The caller holds ``perm`` — or the admin role — *in* ``company_id``,
    one of their companies (work company or a company role assignment).

    Group permissions count per company (CompanyGroupAssignment): an HR role
    in company A and an employee role in company B give HR rights in A only.
    A user-level permission is global, but still only inside the caller's
    own companies. Read from the assignments, not from the request: a JWT
    request has no selected company."""
    from base.auth_backends import (
        get_allowed_company_ids,
        get_effective_permission_codenames,
        get_user_groups_for_company,
    )
    from base.roles import ADMIN_ROLE

    if company_id is None or company_id not in get_allowed_company_ids(user):
        return False
    codename = perm.split(".", 1)[-1] if perm else None
    if codename and codename in get_effective_permission_codenames(user, company_id):
        return True
    return get_user_groups_for_company(user, company_id).filter(name=ADMIN_ROLE).exists()


def companies_with_right(user, perm) -> set:
    from base.auth_backends import get_allowed_company_ids

    return {company for company in get_allowed_company_ids(user) if holds_right_in_company(user, company, perm)}


def may_manage_employee(user, employee, perm, *, allow_self=False) -> bool:
    """``base.access.can_manage_employee``, with HR/Admin rights counted in
    the employee's company only.

    Yes for a superuser; never one's own record unless ``allow_self``; yes
    for the employee's own reporting manager; otherwise yes when the caller
    holds ``perm`` or the admin role in the employee's company. An employee
    with no company keeps the old rule (``can_manage_employee``)."""
    from base.auth_backends import company_scoped_active

    if employee is None or not getattr(user, "is_authenticated", False):
        return False
    if not company_scoped_active():
        # Company scoping switched off (the settings rollback switch):
        # permissions and roles are global again, so is this rule.
        return can_manage_employee(user, employee, perm, allow_self=allow_self)
    if getattr(user, "is_superuser", False):
        return True
    actor = actor_of(user)
    if actor is not None and employee.pk == actor.pk and not allow_self:
        return False
    if is_direct_manager(user, employee):
        return True
    company = _company_id(employee)
    if company is None:
        return can_manage_employee(user, employee, perm, allow_self=allow_self)
    return holds_right_in_company(user, company, perm)


def within_reach(user, queryset, perm, *, extra_ids=()):
    """The rows of a request list (``employee_id`` field) the caller may see:
    everything for a superuser; else their own, their direct reports', those
    of companies where they hold ``perm`` or the admin role, those of
    employees with no company, and ``extra_ids`` (e.g. steps they approve).

    For the API lists: JWT requests are not filtered by company, and
    ``filtersubordinates`` returns every row to a holder of the permission."""
    from django.db.models import Q

    from base.auth_backends import company_scoped_active

    if getattr(user, "is_superuser", False) or not company_scoped_active():
        return queryset
    company = "employee_id__employee_work_info__company_id"
    reach = Q(**{f"{company}__in": list(companies_with_right(user, perm))}) | Q(**{f"{company}__isnull": True})
    actor = actor_of(user)
    if actor is not None:
        reach |= Q(employee_id=actor.pk) | Q(employee_id__employee_work_info__reporting_manager_id=actor.pk)
    if extra_ids:
        reach |= Q(id__in=list(extra_ids))
    return queryset.filter(reach)


def is_own_leave(user, leave_request) -> bool:
    actor = actor_of(user)
    return actor is not None and leave_request.employee_id_id == actor.pk


def is_step_manager(user, leave_request) -> bool:
    """The caller approves one step of this leave (multi-step approval)."""
    from leave.models import LeaveRequestConditionApproval

    actor = actor_of(user)
    return (
        actor is not None
        and LeaveRequestConditionApproval._base_manager.filter(
            leave_request_id_id=leave_request.pk, manager_id_id=actor.pk
        ).exists()
    )


def may_manage_leave(user, leave_request, perm) -> bool:
    """Own request, or a request of someone the caller may manage (a leave
    or an allocation request: anything with ``employee_id``)."""
    return is_own_leave(user, leave_request) or may_manage_employee(user, leave_request.employee_id, perm)


def may_view_leave(user, leave_request) -> bool:
    return may_manage_leave(user, leave_request, VIEW_PERM) or is_step_manager(user, leave_request)


def may_view_employee_leaves(user, employee) -> bool:
    """The leave tab of an employee profile: one's own, or an employee whose
    leave the caller may see — the tab's own visibility rule
    (``leave/cbv/accessibility.py`` ``leave_accessibility``), per company."""
    actor = actor_of(user)
    if employee is None:
        return False
    if actor is not None and employee.pk == actor.pk:
        return True
    return may_manage_employee(user, employee, VIEW_PERM)


def whole_number(value):
    """``value`` as a positive id, or None (ASCII digits only: ``"²"`` passes
    ``str.isdigit`` but not ``int``)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not (value.isascii() and value.isdigit()):
            return None
    if not isinstance(value, (int, str)):
        return None
    number = int(value)
    return number if 0 < number < 2**63 else None


def requested_ids(raw) -> list[int]:
    """The leave ids of a bulk action, from the browser's JSON list (or an
    already parsed list); anything that is not a whole number is dropped —
    never trusted, never a crash."""
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, RecursionError):
        return []
    if not isinstance(values, (list, tuple)):
        return []
    ids = []
    for value in values:
        number = whole_number(value)
        if number is not None and number not in ids:
            ids.append(number)
    return ids


DELETED, MISSING, REFUSED, DECIDED = "deleted", "missing", "refused", "decided"


def delete_waiting_leave(leave_request, may_delete) -> str:
    """Delete ``leave_request`` when, on its locked row, ``may_delete(row)``
    holds and the leave still waits for approval.

    Returns ``"deleted"``, ``"missing"`` (gone meanwhile), ``"refused"``
    (the caller may not) or ``"decided"`` (approved, rejected or cancelled:
    an approved leave is cancelled through a cancellation request, never
    deleted). ``ProtectedError`` reaches the caller with nothing deleted.
    """
    from leave.balance_ops import relock

    with transaction.atomic():
        try:
            row = relock(leave_request)
        except type(leave_request).DoesNotExist:
            return MISSING
        if not may_delete(row):
            return REFUSED
        if row.status != "requested":
            return DECIDED
        row.delete()
    return DELETED


DECIDED_MESSAGE = "Đơn đã được xử lý nên không thể xóa. Đơn đã duyệt cần gửi yêu cầu hủy."


# -- approvals: check-in admin of the employee's company (2026-11-14) ---------


def is_admin_for(user, employee) -> bool:
    """A superuser, or a check-in admin *of the employee's company*.

    ``base.roles.is_checkin_admin`` reads the role for the selected company;
    a JWT request has none, so it answered with the union of the caller's
    companies: an admin role in company A approved company B's leaves.
    With company scoping switched off roles are global again, so is this."""
    from base.auth_backends import company_scoped_active, get_allowed_company_ids, get_user_groups_for_company
    from base.roles import ADMIN_ROLE, is_checkin_admin

    if getattr(user, "is_superuser", False):
        return True
    if not getattr(user, "is_authenticated", False) or employee is None:
        return False
    company = _company_id(employee)
    if not company_scoped_active() or company is None:
        return is_checkin_admin(user)
    return (
        company in get_allowed_company_ids(user)
        and get_user_groups_for_company(user, company).filter(name=ADMIN_ROLE).exists()
    )


# -- compensatory leave requests (2026-11-14) ----------------------------------

COMP_VIEW_PERM = "leave.view_compensatoryleaverequest"
COMP_CHANGE_PERM = "leave.change_compensatoryleaverequest"
COMP_DELETE_PERM = "leave.delete_compensatoryleaverequest"


def may_view_compensatory(user, comp) -> bool:
    return is_own_leave(user, comp) or may_manage_employee(user, comp.employee_id, COMP_VIEW_PERM)


# -- comments and their attachments (2026-11-14) -------------------------------
#
# A comment belongs to a request (leave, allocation, compensatory). Reading,
# adding and attaching need the right to see that request's comments; a
# comment is deleted by its author, or by someone who may manage the
# employee. The permissions are the comment models' own, as before; what is
# new is that they count only for employees the caller may manage (their
# company, their team) — "manager of somebody" used to be enough.

COMMENT_PERMS = {
    # request model -> (view, add, delete) comment permission
    "leaverequest": (
        "leave.view_leaverequestcomment", "leave.add_leaverequestcomment", "leave.delete_leaverequestcomment",
    ),
    "leaveallocationrequest": (
        "leave.view_leaveallocationrequestcomment",
        "leave.add_leaveallocationrequestcomment",
        "leave.delete_leaveallocationrequestcomment",
    ),
    "compensatoryleaverequest": (
        "leave.view_compensatoryleaverequestcomment",
        "leave.add_compensatoryleaverequestcomment",
        "leave.delete_compensatoryleaverequestcomment",
    ),
}
FILE_DELETE_PERM = "leave.delete_leaverequestfile"


def _comment_perms(request_obj):
    return COMMENT_PERMS[request_obj._meta.model_name]


def may_read_comments(user, request_obj) -> bool:
    """The request's employee, someone who may manage them (with the comment
    view permission, or as their reporting manager), or — for a leave — a
    step approver."""
    if request_obj is None:
        return False
    if is_own_leave(user, request_obj):
        return True
    if request_obj._meta.model_name == "leaverequest" and is_step_manager(user, request_obj):
        return True
    return may_manage_employee(user, request_obj.employee_id, _comment_perms(request_obj)[0])


def may_add_comment(user, request_obj) -> bool:
    return may_read_comments(user, request_obj) or (
        request_obj is not None and may_manage_employee(user, request_obj.employee_id, _comment_perms(request_obj)[1])
    )


def _is_author(user, comment) -> bool:
    actor = actor_of(user)
    return actor is not None and comment.employee_id_id == actor.pk


def may_attach_to(user, comment) -> bool:
    """Add files to a comment: its author, or someone who may manage the
    employee with the add-comment permission."""
    request_obj = comment.request_id
    if _is_author(user, comment):
        return may_read_comments(user, request_obj)
    # The add-comment permission, or the add-file one the templates show the
    # button for.
    return may_manage_employee(user, request_obj.employee_id, _comment_perms(request_obj)[1]) or may_manage_employee(
        user, request_obj.employee_id, "leave.add_leaverequestfile"
    )


def may_delete_comment(user, comment, perm=None) -> bool:
    """Its author (who may still see the request's comments), or someone who
    may manage the employee with the delete permission (``perm``, by default
    the comment model's)."""
    request_obj = comment.request_id
    if _is_author(user, comment) and may_read_comments(user, request_obj):
        return True
    return may_manage_employee(user, request_obj.employee_id, perm or _comment_perms(request_obj)[2])


def deletable_attachments(user, comment_model, request_obj, file_ids, comment_id=None):
    """The attachments among ``file_ids`` that belong to a comment of
    ``request_obj`` (and to ``comment_id`` when given) which the caller may
    delete. The browser used to send any file ids and they were deleted,
    whatever comment they belonged to."""
    from leave.models import LeaverequestFile

    ids = requested_ids(list(file_ids))
    if request_obj is None or not ids:
        return LeaverequestFile.objects.none()
    comments = comment_model._base_manager.filter(request_id=request_obj, files__id__in=ids).distinct()
    if comment_id is not None:
        comments = comments.filter(pk=whole_number(comment_id) or 0)
    allowed = []
    for comment in comments:
        if may_delete_comment(user, comment, FILE_DELETE_PERM):
            allowed.extend(comment.files.filter(id__in=ids).values_list("id", flat=True))
    return LeaverequestFile.objects.filter(id__in=allowed)
