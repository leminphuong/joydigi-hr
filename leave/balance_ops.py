"""Leave balance changes under row locks (2026-11-13).

Every decision that moves days — approve, reject, cancel, allocation
approve/reject — used to read a request and a balance, then write both back
with no lock. Two managers approving the same request together (web and
app, or two tabs) could both see "requested" and deduct twice; a reject and
a cancel together could refund twice.

The rules here, used by the web views and the mobile API alike:

* the request row and the balance row are locked with SELECT … FOR UPDATE
  inside the caller's transaction, and the request status is checked again
  *after* the lock — the second caller waits, then finds the first one's
  result;
* deduction takes the carry-forward days first (they expire first), then
  the year's days, and never lets the balance go below zero;
* a refund gives back exactly what the request recorded as taken and then
  zeroes those fields, so a second refund of the same request adds nothing.

Rows are locked through the base manager, by primary key: the
company-scoped managers join other tables and, on the web, add DISTINCT,
and PostgreSQL refuses FOR UPDATE with either. Callers find the row through
the scoped manager first (who may see it), then lock it here.
"""

from __future__ import annotations

from django.db import transaction


class LeaveBalanceError(Exception):
    """A decision that can not go through; `message` is shown to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _days(value) -> float:
    return float(value or 0)


def relock(obj):
    """`obj` read again and locked by primary key; None stays None.

    For a row the caller already found through a scoped manager."""
    if obj is None:
        return None
    assert transaction.get_connection().in_atomic_block, "relock needs transaction.atomic()"
    return type(obj)._base_manager.select_for_update().get(pk=obj.pk)


def lock_leave_request(pk):
    """The leave request, locked; None when it does not exist."""
    from leave.models import LeaveRequest

    assert transaction.get_connection().in_atomic_block, "lock_leave_request needs transaction.atomic()"
    return LeaveRequest._base_manager.select_for_update().filter(pk=pk).first()


def lock_allocation_request(pk):
    from leave.models import LeaveAllocationRequest

    assert transaction.get_connection().in_atomic_block, "lock_allocation_request needs transaction.atomic()"
    return LeaveAllocationRequest._base_manager.select_for_update().filter(pk=pk).first()


def lock_balance(employee, leave_type, *, create: bool = False):
    """The balance row that `leave_type` draws on (a shared fund's row when the
    type shares one), locked. With `create`, an empty row is made first."""
    from leave.models import AvailableLeave, balance_leave_type

    assert transaction.get_connection().in_atomic_block, "lock_balance needs transaction.atomic()"
    fund_type = balance_leave_type(leave_type)
    rows = AvailableLeave._base_manager.select_for_update().filter(employee_id=employee, leave_type_id=fund_type)
    row = rows.first()
    if row is None and create:
        AvailableLeave._base_manager.get_or_create(employee_id=employee, leave_type_id=fund_type)
        row = rows.first()
    return row


INSUFFICIENT = "Không đủ ngày phép để duyệt đơn này."


def ensure_sufficient(leave_request, balance) -> None:
    """Raise when `balance` can not cover the request (nothing is changed)."""
    if _days(balance.carryforward_days) + _days(balance.available_days) < _days(leave_request.requested_days):
        raise LeaveBalanceError("insufficient", INSUFFICIENT)


def deduct_for_request(leave_request, balance) -> None:
    """Take `leave_request.requested_days` from `balance` (both locked).

    Carry-forward first, then the year's days. Records what was taken on the
    request (`approved_carryforward_days`, `approved_available_days`) so a
    later refund gives back exactly that. Never below zero.
    """
    ensure_sufficient(leave_request, balance)
    requested = _days(leave_request.requested_days)
    carry = _days(balance.carryforward_days)
    available = _days(balance.available_days)
    from_carry = min(carry, requested)
    from_available = round(requested - from_carry, 3)
    balance.carryforward_days = round(carry - from_carry, 3)
    balance.available_days = round(available - from_available, 3)
    leave_request.approved_carryforward_days = from_carry
    leave_request.approved_available_days = from_available
    balance.save()


def refund_for_request(leave_request, balance) -> float:
    """Give back what `leave_request` took (both locked); idempotent.

    Returns the days given back. The request's recorded days are zeroed, so
    refunding the same request again gives back nothing.
    """
    back_available = _days(leave_request.approved_available_days)
    back_carry = _days(leave_request.approved_carryforward_days)
    if not (back_available or back_carry):
        return 0.0
    if balance is None:
        # No fund row to give the days back to: keep what the request
        # recorded, so a later refund can still give it back.
        return 0.0
    balance.available_days = round(_days(balance.available_days) + back_available, 3)
    balance.carryforward_days = round(_days(balance.carryforward_days) + back_carry, 3)
    balance.save()
    leave_request.approved_available_days = 0
    leave_request.approved_carryforward_days = 0
    return back_available + back_carry


def grant_allocation(allocation_request, balance) -> None:
    """Add an approved allocation's days to the (locked) balance."""
    balance.available_days = round(_days(balance.available_days) + _days(allocation_request.requested_days), 3)
    balance.save()


def withdraw_allocation(allocation_request, balance) -> None:
    """Take an approved allocation's days back from the (locked) balance, not below zero."""
    if balance is None:
        return
    balance.available_days = round(max(0.0, _days(balance.available_days) - _days(allocation_request.requested_days)), 3)
    balance.save()


# -- the other paths that move days (2026-11-13, second pass) -----------------


DECISION_FIELDS = ("status", "approved_available_days", "approved_carryforward_days")


def save_edited_leave(leave_request, *, only_if_status=None, owner_id=None, may_edit=None) -> bool:
    """Save a leave edited through a form, under its row lock (2026-11-13, P1-2).

    The form's copy was read when the request started; an approval, a
    rejection or a cancellation may have committed since. Its fields
    (status, approved_*) are never written from that stale copy: the edit is
    refused when the status changed meanwhile (or is not ``only_if_status``),
    and otherwise saved with the row's current decision fields. Returns True
    when saved, False when nothing was written.

    2026-11-14: who may edit is checked again on the locked row, in the same
    transaction as the save — ``owner_id``: the leave must (still) belong to
    that employee; ``may_edit(current)``: a permission check on the current
    row (e.g. the manager may manage its employee).
    """
    seen_status = leave_request.status
    with transaction.atomic():
        try:
            current = relock(leave_request)
        except type(leave_request).DoesNotExist:
            return False  # deleted meanwhile
        if current.status != seen_status or (only_if_status and current.status != only_if_status):
            return False
        if owner_id is not None and current.employee_id_id != owner_id:
            return False
        if may_edit is not None and not may_edit(current):
            return False
        for name in DECISION_FIELDS:
            setattr(leave_request, name, getattr(current, name))
        # 2026-11-15: an approved leave keeps its days, type and employee
        # (only its note may change), and a waiting one stays within the
        # monthly limit — the reason is kept for the caller's message.
        from leave import monthly_limit

        try:
            with transaction.atomic():
                leave_request.save()
        except monthly_limit.LeaveRuleError as error:
            leave_request.edit_refusal = " ".join(error.messages)
            return False
    return True


def edit_refusal(leave_request) -> str:
    """Why ``save_edited_leave`` refused, for the message."""
    return getattr(leave_request, "edit_refusal", None) or EDIT_REFUSED


EDIT_REFUSED = "Đơn vừa được xử lý (duyệt, từ chối hoặc hủy) nên không thể sửa. Vui lòng tải lại."


def approve_without_review(leave_request) -> bool:
    """Approve a just-saved request of a type that needs no approval
    (``require_approval == "no"``), under the same locks as a manager's
    approval. Returns True when approved. A request the balance can not cover
    (two requests raced for the last days) stays ``requested`` for HR instead
    of driving the balance below zero. ``leave_request`` is refreshed.
    """
    approved = False
    with transaction.atomic():
        locked = lock_leave_request(leave_request.pk)
        if locked is not None and locked.status == "requested":
            balance = lock_balance(locked.employee_id, locked.leave_type_id)
            if balance is not None:
                try:
                    deduct_for_request(locked, balance)
                except LeaveBalanceError:
                    pass
                else:
                    locked.status = "approved"
                    locked.save()
                    approved = True
    leave_request.refresh_from_db()
    return approved


PENALTY_MARK = "[phạt #{pk}: -{days:g}]"
_PENALTY_RE = r"\[phạt #{pk}: -([\d.]+)\]"


def deduct_penalty(penalty) -> float:
    """Take a penalty's leave cut (``minus_leaves``, in half days) from the
    employee's fund, locked, never below zero. What was really taken is
    written in the row's history so deleting the penalty gives back exactly
    that. Returns the days taken."""
    unit = round(_days(penalty.minus_leaves) * 2) / 2
    if unit <= 0:
        return 0.0
    with transaction.atomic():
        balance = lock_balance(penalty.employee_id, penalty.leave_type_id)
        if balance is None:
            return 0.0
        field = "carryforward_days" if penalty.deduct_from_carry_forward else "available_days"
        before = _days(getattr(balance, field))
        taken = round(min(before, unit), 3)
        setattr(balance, field, round(before - taken, 3))
        balance._change_reason = PENALTY_MARK.format(pk=penalty.pk, days=taken)[:100]
        balance.save()
    return taken


def restore_penalty(penalty) -> float:
    """Give back what :func:`deduct_penalty` took for this penalty, once.

    A penalty recorded before 2026-11-13 has no mark: its full cut is given
    back, as before."""
    import re

    from leave.models import AvailableLeave

    unit = round(_days(penalty.minus_leaves) * 2) / 2
    if unit <= 0:
        return 0.0
    with transaction.atomic():
        balance = lock_balance(penalty.employee_id, penalty.leave_type_id)
        if balance is None:
            return 0.0
        pattern = re.compile(_PENALTY_RE.format(pk=penalty.pk))
        reasons = list(
            AvailableLeave.history.filter(id=balance.pk, history_change_reason__startswith="[phạt #")
            .values_list("history_change_reason", flat=True)
        )
        marks = [m for m in (pattern.search(r or "") for r in reasons) if m]
        if any("hoàn" in (r or "") and f"#{penalty.pk}:" in (r or "") for r in reasons):
            return 0.0  # already given back
        back = float(marks[0].group(1)) if marks else unit
        field = "carryforward_days" if penalty.deduct_from_carry_forward else "available_days"
        setattr(balance, field, round(_days(getattr(balance, field)) + back, 3))
        balance._change_reason = f"[phạt #{penalty.pk}: hoàn +{back:g}]"[:100]
        balance.save()
    return back


def add_days(employee, leave_type, days, *, create: bool = False) -> None:
    """Add ``days`` to the fund ``leave_type`` draws on (compensatory leave,
    HR's "add available days"), locked."""
    if not days:
        return
    with transaction.atomic():
        balance = lock_balance(employee, leave_type, create=create)
        if balance is None:
            return
        balance.available_days = round(_days(balance.available_days) + _days(days), 3)
        balance.save()


def take_days(employee, leave_type, days) -> float:
    """Take ``days`` from the fund (the year's days first, then the
    carry-forward), locked, never below zero. Returns the days taken."""
    if not days:
        return 0.0
    with transaction.atomic():
        balance = lock_balance(employee, leave_type)
        if balance is None:
            return 0.0
        wanted = _days(days)
        from_available = min(_days(balance.available_days), wanted)
        from_carry = min(_days(balance.carryforward_days), round(wanted - from_available, 3))
        balance.available_days = round(_days(balance.available_days) - from_available, 3)
        balance.carryforward_days = round(_days(balance.carryforward_days) - from_carry, 3)
        balance.save()
    return from_available + from_carry
