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
    from base.roles import admin_level_group_q

    if company_id is None or company_id not in get_allowed_company_ids(user):
        return False
    codename = perm.split(".", 1)[-1] if perm else None
    if codename and codename in get_effective_permission_codenames(user, company_id):
        return True
    # The admin role, or the Director role (2026-11-17), in that company.
    return get_user_groups_for_company(user, company_id).filter(admin_level_group_q()).exists()


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
    from base.roles import admin_level_group_q, is_checkin_admin

    if getattr(user, "is_superuser", False):
        return True
    if not getattr(user, "is_authenticated", False) or employee is None:
        return False
    company = _company_id(employee)
    if not company_scoped_active() or company is None:
        return is_checkin_admin(user)
    # The admin role or the Director role (2026-11-17) of that company.
    return (
        company in get_allowed_company_ids(user)
        and get_user_groups_for_company(user, company).filter(admin_level_group_q()).exists()
    )


def is_director_for(user, employee) -> bool:
    """The Director role *in the employee's company* (2026-11-17). Not a
    superuser shortcut: callers decide what a superuser may do."""
    from base.auth_backends import company_scoped_active, get_allowed_company_ids
    from base.roles import user_has_director_role

    if not getattr(user, "is_authenticated", False) or employee is None:
        return False
    company = _company_id(employee)
    if company is None:
        return False  # no company: nobody is its Director
    if not company_scoped_active():
        return user_has_director_role(user)
    return company in get_allowed_company_ids(user) and user_has_director_role(user, company)


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


# -- multi-step approval: one rule for the web, the API, the approval hub,
# -- the menu and the pending count (2026-11-17) ------------------------------
#
# A leave whose type routes it through several approvers has one
# ``LeaveRequestConditionApproval`` row per step. The approver of a step is
# chosen when the leave is filed (``LeaveRequest._save_and_route``): the
# reporting manager, the managers of a matching "Điều kiện duyệt nhiều cấp",
# and by default an admin of the company. Nothing there looks at roles, so
# every check below starts from the step rows, not from roles.

NOT_CURRENT_STEP = "Bạn không phải người duyệt ở bước hiện tại."
EARLIER_STEP_WAITING = "Đơn này chưa được duyệt ở bước trước."
NOT_WAITING = "Đơn không còn ở trạng thái chờ duyệt."
NOT_TEAM = {
    "approve": "Bạn chỉ được duyệt đơn của thành viên trong nhóm mình.",
    "reject": "Bạn chỉ được từ chối đơn của thành viên trong nhóm mình.",
}
APPROVE, REJECT = "approve", "reject"
OVERRIDE_REASON_MIN = 10


def _step_model():
    from leave.models import LeaveRequestConditionApproval

    return LeaveRequestConditionApproval


def steps_of(leave_request) -> list:
    """The leave's steps in order (read through the base manager)."""
    return list(
        _step_model()
        ._base_manager.filter(leave_request_id_id=leave_request.pk)
        .select_related("manager_id", "acted_by")
        .order_by("sequence", "id")
    )


def current_step(leave_request, steps=None):
    """The first step neither approved nor rejected, or None."""
    for step in steps if steps is not None else steps_of(leave_request):
        if not step.is_approved and not step.is_rejected:
            return step
    return None


def step_was_decided(step, leave_request) -> bool:
    """A step someone decided: rejected, or approved by somebody other than
    the employee (a step whose approver is the employee is approved by
    itself when the leave is filed, see ``leave.signals``)."""
    if step.is_rejected:
        return True
    return bool(step.is_approved and step.manager_id_id != leave_request.employee_id_id)


def review_problem(user, leave_request, action=APPROVE, *, steps=None, admin_cache=None):
    """None when ``user`` may approve (or reject) ``leave_request`` now, else
    the reason. The single rule of the web views, the API, the approval hub
    and the pending count.

    * A waiting leave with steps: only the approver of the *current* step,
      whatever role they hold, superuser included (a superuser who is not
      that approver decides on someone's behalf, with a reason:
      ``override_problem``). Never several steps at once.
    * Otherwise (a one-step leave; a decided multi-step leave: HR rejecting
      an approved leave; or a waiting leave whose every step is already
      approved, e.g. an admin's own leave whose only step approved itself):
      a superuser; else never one's own leave; else an admin or Director of
      the employee's company, or the employee's own reporting manager.
    """
    from django.utils.translation import gettext as _

    if not getattr(user, "is_authenticated", False):
        return _("You do not have permission to perform this action.")
    actor = actor_of(user)
    steps = steps if steps is not None else steps_of(leave_request)
    step = current_step(leave_request, steps) if leave_request.status == "requested" else None
    if step is not None:
        if actor is None or step.manager_id_id != actor.pk:
            if actor is not None and any(
                later.manager_id_id == actor.pk and not later.is_approved and not later.is_rejected
                for later in steps
            ):
                return EARLIER_STEP_WAITING
            return NOT_CURRENT_STEP
        if leave_request.employee_id_id == actor.pk:
            return _("You cannot approve your own leave request.")
        return None
    if getattr(user, "is_superuser", False):
        return None
    if actor is None:
        return _("You do not have permission to perform this action.")
    if leave_request.employee_id_id == actor.pk:
        return _("You cannot approve your own leave request.")
    employee = leave_request.employee_id
    if admin_cache is not None:
        company = _company_id(employee)
        if company not in admin_cache:
            admin_cache[company] = is_admin_for(user, employee)
        is_admin = admin_cache[company]
    else:
        is_admin = is_admin_for(user, employee)
    if is_admin or is_direct_manager(user, employee):
        return None
    return NOT_TEAM.get(action, NOT_TEAM[APPROVE])


def _pending_steps_of_actor(actor):
    return _step_model()._base_manager.filter(
        manager_id_id=actor.pk,
        is_approved=False,
        is_rejected=False,
        leave_request_id__status="requested",
        leave_request_id__employee_id__is_active=True,
    )


def has_assigned_step(user) -> bool:
    """The user approves a step of a leave that is still waiting."""
    actor = actor_of(user)
    if actor is None:
        return False
    return _pending_steps_of_actor(actor).exists()


def has_any_step(user) -> bool:
    """The user approves, or approved, a step of some leave (the approval
    hub also shows them the history of those leaves)."""
    actor = actor_of(user)
    if actor is None:
        return False
    return _step_model()._base_manager.filter(manager_id_id=actor.pk).exists()


def assigned_leave_ids(user) -> list:
    """Every leave with a step approved, rejected or still to be decided by
    the user, any status (their history included)."""
    actor = actor_of(user)
    if actor is None:
        return []
    return list(
        _step_model()
        ._base_manager.filter(manager_id_id=actor.pk)
        .values_list("leave_request_id_id", flat=True)
        .distinct()
    )


def current_step_leave_ids(user) -> list:
    """Waiting leaves whose *current* step is the user's."""
    actor = actor_of(user)
    if actor is None:
        return []
    candidate_ids = set(
        _pending_steps_of_actor(actor).values_list("leave_request_id_id", flat=True)
    )
    if not candidate_ids:
        return []
    firsts = {}
    for leave_id, manager_id in (
        _step_model()
        ._base_manager.filter(
            leave_request_id_id__in=candidate_ids, is_approved=False, is_rejected=False
        )
        .order_by("leave_request_id_id", "sequence", "id")
        .values_list("leave_request_id_id", "manager_id_id")
    ):
        firsts.setdefault(leave_id, manager_id)
    return sorted(
        leave_id for leave_id, manager_id in firsts.items() if manager_id == actor.pk
    )


def actionable_leave_ids(user, one_step_employee_ids=None) -> list:
    """Waiting leaves the user may decide now: those whose current step is
    theirs, plus one-step leaves of ``one_step_employee_ids`` (the screen's
    own employee scope) that ``review_problem`` lets them decide."""
    from leave.models import LeaveRequest

    ids = set(current_step_leave_ids(user))
    if one_step_employee_ids is not None:
        # A leave with a step still to decide belongs to that step's approver;
        # every other waiting leave (no steps, or every step approved) follows
        # the one-step rule.
        with_steps = _step_model()._base_manager.filter(
            is_approved=False, is_rejected=False
        ).values("leave_request_id_id")
        candidates = (
            LeaveRequest._base_manager.filter(
                status="requested",
                employee_id_id__in=list(one_step_employee_ids),
                employee_id__is_active=True,
            )
            .exclude(pk__in=with_steps)
            .select_related("employee_id")
        )
        actor = actor_of(user)
        if actor is not None:
            # Never one's own leave in "waiting for me" (a superuser may
            # still decide it on the full page, as before).
            candidates = candidates.exclude(employee_id_id=actor.pk)
        admin_cache = {}
        for leave in candidates:
            if review_problem(user, leave, steps=[], admin_cache=admin_cache) is None:
                ids.add(leave.pk)
    return sorted(ids)


def clean_reason(reason, minimum=1):
    """The reason, stripped, or None when it is shorter than ``minimum``."""
    text = reason.strip() if isinstance(reason, str) else ""
    return text if len(text) >= minimum else None


def override_problem(user, leave_request, *, steps=None, director_cache=None):
    """None when ``user`` may decide the current step of ``leave_request`` on
    behalf of its approver (2026-11-17), else the reason.

    A Director of the employee's company, or a superuser; never one's own
    leave; only a waiting leave with steps, only its current step, and not a
    step that is the caller's own (that is an ordinary decision). The reason
    is checked by the caller (``OVERRIDE_REASON_MIN``)."""
    if not getattr(user, "is_authenticated", False):
        return "Bạn không có quyền duyệt thay."
    if leave_request.status != "requested":
        return NOT_WAITING
    steps = steps if steps is not None else steps_of(leave_request)
    step = current_step(leave_request, steps)
    if step is None:
        return "Đơn không có bước duyệt đang chờ."
    actor = actor_of(user)
    if actor is not None and leave_request.employee_id_id == actor.pk:
        return "Bạn không thể duyệt thay đơn nghỉ của chính mình."
    if getattr(user, "is_superuser", False):
        is_director = True
    elif director_cache is not None:
        company = _company_id(leave_request.employee_id)
        if company not in director_cache:
            director_cache[company] = is_director_for(user, leave_request.employee_id)
        is_director = director_cache[company]
    else:
        is_director = is_director_for(user, leave_request.employee_id)
    if not is_director:
        return "Chỉ Giám đốc của công ty nhân viên mới được duyệt thay."
    if actor is not None and step.manager_id_id == actor.pk:
        return "Đây là bước của bạn, hãy duyệt hoặc từ chối bình thường."
    return None
