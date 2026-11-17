"""Recording a decision on one step of a multi-step leave (2026-11-17).

Who may decide is ``leave.request_access.review_problem`` (an ordinary
decision) or ``override_problem`` (a Director, or a superuser, deciding the
current step on behalf of its approver). This module only writes the
outcome, the same way for the web, the API and the approval hub:

* the step records who decided and when (``acted_by``, ``acted_at``) and,
  for a decision on someone's behalf, ``is_override`` and the reason;
* exactly one step per action, the current one; the next approver is told;
* an explicit activity-log row with the outcome (the request log alone says
  "200" for a refusal too).

Nothing here creates, deletes or re-orders steps: the chain chosen when the
leave was filed stays as it is.
"""

from __future__ import annotations

import contextlib
import logging

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from leave.request_access import (
    APPROVE,
    OVERRIDE_REASON_MIN,
    REJECT,
    _company_id,
    actor_of,
    clean_reason,
    current_step,
    override_problem,
)

logger = logging.getLogger(__name__)

NEXT_STEP_VERB = "Có đơn nghỉ phép đang chờ bạn duyệt."
OVERRIDE_REASON_REQUIRED = (
    f"Lý do duyệt thay phải có ít nhất {OVERRIDE_REASON_MIN} ký tự."
)
REJECT_REASON_REQUIRED = "Vui lòng nhập lý do từ chối."
NOT_FOUND = "Không tìm thấy đơn nghỉ."
NO_BALANCE = "Nhân viên chưa có quỹ phép cho loại phép này."


class BalanceRefusal(str):
    """A refusal message caused by the leave balance (not by the rights)."""


def mark_step(step, actor, *, approved: bool, override_reason: str | None = None):
    """Write the decision on ``step`` (already locked or about to be saved
    inside the caller's transaction)."""
    step.is_approved = bool(approved)
    step.is_rejected = not approved
    step.acted_at = timezone.now()
    step.acted_by = actor
    fields = ["is_approved", "is_rejected", "acted_at", "acted_by"]
    if override_reason is not None:
        step.is_override = True
        step.override_reason = override_reason
        fields += ["is_override", "override_reason"]
    step.save(update_fields=fields)
    return step


def locked_steps(leave_request) -> list:
    """The leave's steps, locked, in order. No select_related: PostgreSQL
    refuses FOR UPDATE on the nullable side of an outer join."""
    from leave.models import LeaveRequestConditionApproval

    return list(
        LeaveRequestConditionApproval._base_manager.select_for_update()
        .filter(leave_request_id_id=leave_request.pk)
        .order_by("sequence", "id")
    )


def approval_hub_url() -> str:
    try:
        return reverse("approval-hub")
    except Exception:  # pragma: no cover - the hub is always installed
        return "/"


def notify_next_step(leave_request, sender, steps=None) -> None:
    """Tell the approver of the step now current (if any) that it is theirs."""
    step = current_step(leave_request, steps)
    if step is None:
        return
    recipient = getattr(step.manager_id, "employee_user_id", None)
    if recipient is None:
        return
    from notifications.signals import notify

    with contextlib.suppress(Exception):
        notify.send(
            sender,
            recipient=recipient,
            verb=NEXT_STEP_VERB,
            icon="people-circle",
            redirect=approval_hub_url(),
        )


def log_decision(user, leave_request, *, decision, result, step=None, reason=None,
                 override=False, request=None, channel="web", message=None):
    """The explicit activity-log row of one decision (or refusal)."""
    from joydigi_audit.middleware import log_business_event

    if override:
        action = "Duyệt thay đơn nghỉ" if decision == APPROVE else "Từ chối thay đơn nghỉ"
    else:
        action = "Duyệt đơn nghỉ" if decision == APPROVE else "Từ chối đơn nghỉ"
    if result != "ok":
        action = f"{action} (bị chặn)"
    details = {
        "leave_id": leave_request.pk,
        "employee_id": leave_request.employee_id_id,
        "decision": decision,
        "result": result,
        "channel": channel,
        "override": bool(override),
    }
    if step is not None:
        details["step"] = step.sequence
        details["assigned_manager_id"] = step.manager_id_id
    if reason:
        details["reason"] = reason[:500]
    if message:
        details["message"] = str(message)[:255]
    log_business_event(
        user,
        action=action,
        resource="Đơn nghỉ phép",
        details=details,
        request=request,
        company_id=_company_id(leave_request.employee_id),
        status_code=200 if result == "ok" else 403,
    )


def decide_on_behalf(user, leave_id, decision, reason, *, request=None, channel="web"):
    """A Director (or a superuser) approves or rejects the *current* step of
    a waiting leave on behalf of its approver. Returns ``(ok, message)``.

    One step only; a reason of at least ``OVERRIDE_REASON_MIN`` characters;
    the assigned approver stays on the step (``manager_id``), the decision is
    recorded as theirs being taken over (``acted_by``, ``is_override``,
    ``override_reason``). The last step approves the leave and takes the days
    exactly once, through the same balance functions as every approval."""
    from leave.balance_ops import (
        LeaveBalanceError,
        deduct_for_request,
        ensure_sufficient,
        lock_balance,
        lock_leave_request,
        refund_for_request,
    )
    from leave.models import LeaverequestComment

    if decision not in (APPROVE, REJECT):
        return False, "Thao tác không hợp lệ."
    text = clean_reason(reason, OVERRIDE_REASON_MIN)
    actor = actor_of(user)
    final = False
    with transaction.atomic():
        leave_request = lock_leave_request(leave_id)
        if leave_request is None:
            return False, NOT_FOUND
        steps = locked_steps(leave_request)
        problem = override_problem(user, leave_request, steps=steps)
        step = current_step(leave_request, steps)
        if problem is None and text is None:
            problem = OVERRIDE_REASON_REQUIRED
        if problem:
            log_decision(user, leave_request, decision=decision, result="refused", step=step,
                         override=True, request=request, channel=channel, message=problem)
            return False, problem
        try:
            if decision == APPROVE:
                balance = lock_balance(leave_request.employee_id, leave_request.leave_type_id)
                if balance is None:
                    return False, NO_BALANCE
                ensure_sufficient(leave_request, balance)
                mark_step(step, actor, approved=True, override_reason=text)
                final = current_step(leave_request, steps) is None
                if final:
                    deduct_for_request(leave_request, balance)
                    leave_request.status = "approved"
                    leave_request.save()
            else:
                mark_step(step, actor, approved=False, override_reason=text)
                balance = lock_balance(leave_request.employee_id, leave_request.leave_type_id)
                if balance is not None:
                    refund_for_request(leave_request, balance)
                leave_request.status = "rejected"
                leave_request.reject_reason = text
                leave_request.leave_clashes_count = 0
                leave_request.save()
                if actor is not None:
                    LeaverequestComment.objects.create(
                        request_id=leave_request, employee_id=actor, comment=text
                    )
        except LeaveBalanceError as error:
            transaction.set_rollback(True)
            return False, BalanceRefusal(error.message)
        log_decision(user, leave_request, decision=decision, result="ok", step=step,
                     reason=text, override=True, request=request, channel=channel)

    sender = actor or user
    assigned = getattr(step.manager_id, "employee_user_id", None)
    if assigned is not None:
        from notifications.signals import notify

        verb = (
            "Bước duyệt của bạn cho một đơn nghỉ đã được Giám đốc duyệt thay."
            if decision == APPROVE
            else "Bước duyệt của bạn cho một đơn nghỉ đã được Giám đốc từ chối thay."
        )
        with contextlib.suppress(Exception):
            notify.send(sender, recipient=assigned, verb=verb, icon="people-circle",
                        redirect=approval_hub_url())
    if decision == APPROVE and not final:
        notify_next_step(leave_request, sender)
    if decision == APPROVE:
        return True, "Đã duyệt thay bước hiện tại." if not final else "Đã duyệt thay, đơn đã được duyệt."
    return True, "Đã từ chối thay, đơn đã bị từ chối."
