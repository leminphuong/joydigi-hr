"""Cancelling a leave — JOYDIGI policy fixed on 2026-11-13.

* A leave still waiting for approval: the employee may withdraw it. Nothing
  was taken from the balance, so nothing is given back (whatever the request
  recorded as taken — normally nothing — is released once).
* An APPROVED leave: the employee only *asks* to cancel it. The leave stays
  ``approved`` — its days stay taken, its effect on attendance stays — and a
  ``LeaveCancellationRequest`` waits for the employee's reporting manager or
  HR/Admin of the same company. At most one request waits per leave.
* Approving the request: under row locks (leave first, then the request, then
  the balance — the same order as every other decision), the status is
  checked again, the leave becomes ``cancelled`` and gets back exactly the
  days it recorded as taken (``approved_available_days`` /
  ``approved_carryforward_days``), once. A leave approved before the balance
  was ever deducted (old data) records 0 and gets 0 back.
* Rejecting it: the leave stays approved, the balance is untouched.

Every decision is recorded on the request (who, when, outcome, reason) and as
a comment on the leave. Nothing the client sends decides who, which company
or how many days: all of it is read from the database.
"""

from __future__ import annotations

import contextlib
from datetime import date

from django.db import IntegrityError, transaction
from django.utils import timezone

from base.access import actor_of, is_direct_manager
from leave.balance_ops import lock_balance, lock_leave_request, refund_for_request


class CancellationError(Exception):
    """A request that can not go through; ``status`` is the HTTP answer."""

    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


# -- who may decide ------------------------------------------------------------


def _company_id(employee):
    from employee.models import EmployeeWorkInformation

    return (
        EmployeeWorkInformation._base_manager.filter(employee_id=employee)
        .values_list("company_id", flat=True)
        .first()
    )


def decision_problem(user, leave_request):
    """None when ``user`` may approve or reject cancelling ``leave_request``,
    else ``(status, message)``.

    A superuser; never one's own leave; the employee's own reporting manager;
    HR/Admin (check-in admin, or the ``leave.change_leaverequest`` permission)
    of the employee's company only.
    """
    from base.roles import is_checkin_admin

    if not getattr(user, "is_authenticated", False):
        return 403, "Bạn chưa đăng nhập."
    actor = actor_of(user)
    employee = leave_request.employee_id
    # Before the superuser shortcut (2026-11-13, P1-4): nobody, a superuser
    # included, decides the cancellation of their own leave.
    if actor is not None and employee.pk == actor.pk:
        return 403, "Bạn không thể tự duyệt yêu cầu hủy đơn của chính mình."
    if getattr(user, "is_superuser", False):
        return None
    if actor is None:
        return 403, "Bạn không có quyền duyệt yêu cầu hủy."
    if is_direct_manager(user, employee):
        return None
    if is_checkin_admin(user) or user.has_perm("leave.change_leaverequest"):
        if _company_id(actor) is not None and _company_id(actor) == _company_id(employee):
            return None
        return 403, "Đơn này thuộc công ty khác, bạn không có quyền xử lý."
    return 403, "Bạn chỉ được duyệt yêu cầu hủy của thành viên trong nhóm mình."


def may_decide(user, leave_request) -> bool:
    return decision_problem(user, leave_request) is None


# -- notifications (after commit, never inside the locks) ----------------------


def _approver_users(employee):
    """Who is told about a new request: the reporting manager, or else the
    company's check-in admins."""
    from django.contrib.auth.models import Group

    from base.roles import admin_level_group_q

    manager = employee.get_reporting_manager()
    if manager is not None and manager.employee_user_id_id:
        return [manager.employee_user_id]
    company = _company_id(employee)
    # The admins and Directors (2026-11-17) of the company.
    groups = Group.objects.filter(admin_level_group_q()).distinct()
    if not groups.exists() or company is None:
        return []
    from joydigi_auth.models import JoydigiUser

    return [
        user
        for user in JoydigiUser.objects.filter(is_active=True, groups__in=groups).distinct()
        if getattr(user, "employee_get", None) is not None
        and _company_id(user.employee_get) == company
        and user.employee_get.pk != employee.pk
    ]


def _notify(sender_employee, recipients, verb, leave_request):
    from notifications.signals import notify

    recipients = [r for r in recipients if r is not None]
    if not recipients or sender_employee is None:
        return
    with contextlib.suppress(Exception):
        notify.send(
            sender_employee,
            recipient=recipients,
            verb=verb,
            icon="people-circle",
            redirect="/leave/cancellation-requests/",
            api_redirect=f"/api/leave/user-request/{leave_request.pk}/",
        )


# -- the employee --------------------------------------------------------------


def latest_cancellation(leave_request):
    """The newest cancellation request of a leave (prefetch-friendly)."""
    rows = list(leave_request.cancellation_requests.all())
    return max(rows, key=lambda c: c.pk) if rows else None


def request_cancellation(user, leave_request_id, reason: str = ""):
    """The employee cancels their pending leave, or asks to cancel an approved
    one. Returns ``("cancelled", None)`` or ``("cancellation_pending", request)``.
    """
    from leave.models import LeaveCancellationRequest

    actor = actor_of(user)
    reason = (reason or "").strip()[:1000]
    with transaction.atomic():
        leave = lock_leave_request(leave_request_id)
        if leave is None:
            raise CancellationError(404, "Không tìm thấy đơn nghỉ phép.", "not_found")
        if actor is None or leave.employee_id_id != actor.pk:
            raise CancellationError(403, "Bạn chỉ được hủy đơn nghỉ phép của chính mình.", "not_owner")
        if leave.status == "requested":
            # Never approved: nothing was taken. Release whatever was
            # recorded (normally nothing), once, and withdraw the leave.
            refund_for_request(leave, lock_balance(leave.employee_id, leave.leave_type_id))
            leave.status = "cancelled"
            leave.save()
            return "cancelled", None
        if leave.status != "approved":
            raise CancellationError(409, "Đơn này không còn ở trạng thái có thể hủy.", "not_cancellable")
        if leave.start_date < date.today():
            raise CancellationError(
                409, "Đơn đã bắt đầu hoặc đã qua, không thể yêu cầu hủy.", "already_started"
            )
        if LeaveCancellationRequest._base_manager.filter(leave_request=leave, status="requested").exists():
            raise CancellationError(409, "Đơn đã có yêu cầu hủy đang chờ duyệt.", "cancellation_pending")
        try:
            with transaction.atomic():  # a savepoint: the constraint is the backstop
                cancellation = LeaveCancellationRequest._base_manager.create(
                    leave_request=leave, requested_by=actor, reason=reason
                )
        except IntegrityError:
            raise CancellationError(409, "Đơn đã có yêu cầu hủy đang chờ duyệt.", "cancellation_pending")
        employee = leave.employee_id
        transaction.on_commit(
            lambda: _notify(
                actor,
                _approver_users(employee),
                f"{employee.get_full_name()} yêu cầu hủy đơn nghỉ phép đã duyệt.",
                leave,
            )
        )
    return "cancellation_pending", cancellation


# -- the manager / HR ------------------------------------------------------------


def decide_cancellation(user, cancellation_id, *, approve: bool, reason: str = ""):
    """Approve (cancel the leave, give its days back once) or reject (change
    nothing) a waiting cancellation request. Returns the request."""
    from leave.models import LeaveCancellationRequest, LeaverequestComment

    reason = (reason or "").strip()[:1000]
    found = (
        LeaveCancellationRequest._base_manager.select_related("leave_request__employee_id")
        .filter(pk=cancellation_id)
        .first()
    )
    if found is None:
        raise CancellationError(404, "Không tìm thấy yêu cầu hủy.", "not_found")
    problem = decision_problem(user, found.leave_request)
    if problem:
        raise CancellationError(problem[0], problem[1], "forbidden")
    actor = actor_of(user)
    with transaction.atomic():
        # Lock order: the leave, then the request, then the balance.
        leave = lock_leave_request(found.leave_request_id)
        cancellation = LeaveCancellationRequest._base_manager.select_for_update().get(pk=cancellation_id)
        if cancellation.status != "requested":
            raise CancellationError(409, "Yêu cầu hủy này đã được xử lý.", "already_decided")
        if leave is None or leave.status != "approved":
            raise CancellationError(409, "Đơn không còn ở trạng thái đã duyệt.", "not_approved")
        if approve:
            balance = lock_balance(leave.employee_id, leave.leave_type_id, create=True)
            back_available = float(leave.approved_available_days or 0)
            back_carry = float(leave.approved_carryforward_days or 0)
            refund_for_request(leave, balance)
            leave.status = "cancelled"
            leave.save()
            cancellation.status = "approved"
            cancellation.refunded_available_days = back_available
            cancellation.refunded_carryforward_days = back_carry
        else:
            cancellation.status = "rejected"
        cancellation.decided_by = actor
        cancellation.decided_at = timezone.now()
        cancellation.decision_reason = reason
        cancellation.save()
        if actor is not None:
            outcome = "chấp thuận" if approve else "từ chối"
            LeaverequestComment.objects.create(
                request_id=leave,
                employee_id=actor,
                comment=(f"Yêu cầu hủy: {outcome}" + (f" — {reason}" if reason else ""))[:255],
            )
        employee = leave.employee_id
        verb = (
            "Yêu cầu hủy đơn nghỉ phép của bạn đã được chấp thuận; số ngày phép đã được hoàn."
            if approve
            else "Yêu cầu hủy đơn nghỉ phép của bạn bị từ chối; đơn vẫn có hiệu lực."
        )
        transaction.on_commit(lambda: _notify(actor, [employee.employee_user_id], verb, leave))
    return cancellation


def visible_waiting_requests(user):
    """Waiting cancellation requests ``user`` may decide, oldest first."""
    from leave.models import LeaveCancellationRequest

    rows = (
        LeaveCancellationRequest._base_manager.filter(status="requested", leave_request__status="approved")
        .select_related("leave_request__employee_id", "leave_request__leave_type_id", "requested_by")
        .order_by("id")
    )
    return [row for row in rows if may_decide(user, row.leave_request)]


def cancellation_json(cancellation):
    """What the API and the app read about a cancellation request."""
    if cancellation is None:
        return None
    return {
        "id": cancellation.pk,
        "status": cancellation.status,
        "reason": cancellation.reason,
        "decision_reason": cancellation.decision_reason,
        "requested_at": cancellation.created_at.isoformat() if cancellation.created_at else None,
        "decided_at": cancellation.decided_at.isoformat() if cancellation.decided_at else None,
    }
