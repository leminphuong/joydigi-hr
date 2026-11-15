"""Web HR: the cancellation requests a manager or HR may decide (2026-11-13).

The decisions go through ``leave.cancellation.decide_cancellation`` — the same
locked, permission-checked code the mobile API uses.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from leave.cancellation import CancellationError, decide_cancellation, may_decide, visible_waiting_requests


@login_required
def cancellation_requests_view(request):
    """Waiting requests the user may decide, and the latest decided ones."""
    from leave.models import LeaveCancellationRequest

    decided = [
        row
        for row in LeaveCancellationRequest._base_manager.exclude(status="requested")
        .select_related("leave_request__employee_id", "leave_request__leave_type_id", "decided_by", "requested_by")
        .order_by("-decided_at", "-id")[:200]
        if may_decide(request.user, row.leave_request)
    ][:50]
    return render(
        request,
        "leave/leave_cancellation/requests.html",
        {"waiting": visible_waiting_requests(request.user), "decided": decided},
    )


@login_required
@require_POST
def cancellation_request_decide(request, pk, decision):
    """POST approve/reject with an optional ``reason``."""
    if decision not in ("approve", "reject"):
        messages.error(request, "Thao tác không hợp lệ.")
        return redirect("leave-cancellation-requests")
    try:
        decide_cancellation(
            request.user, pk, approve=decision == "approve", reason=request.POST.get("reason", "")
        )
    except CancellationError as error:
        messages.error(request, error.message)
    else:
        messages.success(
            request,
            "Đã chấp thuận hủy; số ngày phép đã được hoàn cho nhân viên."
            if decision == "approve"
            else "Đã từ chối yêu cầu hủy; đơn vẫn có hiệu lực.",
        )
    return redirect("leave-cancellation-requests")
