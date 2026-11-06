"""Activity timeline (JOYOKR ``JoyOKR_Engagement_Repository::log``).

Each entry names exactly one target, like JOYOKR's ``object_type`` /
``object_id`` pair, so an objective's timeline holds objective-level events
and a key result's timeline holds its own.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from okr.models import OKRActivity
from okr.models.choices import ActivityAction


def _json_safe(value):
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # Decimal, date: keep the exact text


def log(actor, action: str, *, cycle=None, objective=None, key_result=None, metadata=None) -> OKRActivity:
    target = key_result or objective or cycle
    return OKRActivity.objects.create(
        company_id=target.company_id,
        actor=actor,
        action=action,
        cycle=cycle if key_result is None and objective is None else None,
        objective=objective if key_result is None else None,
        key_result=key_result,
        metadata=_json_safe(metadata or {}),
    )


def _percent(value) -> str:
    """'62.50' -> '62,5' (Vietnamese decimal comma, no trailing zeros)."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    text = format(number.quantize(Decimal("0.01")).normalize(), "f")
    return text.replace(".", ",")


_STATUS_LABELS = {"draft": "Nháp", "active": "Đang chạy", "closed": "Đã đóng"}


def summary(activity: OKRActivity) -> str:
    """One Vietnamese line describing an entry, for timelines."""
    meta = activity.metadata or {}
    action = activity.action
    if action == ActivityAction.CHECKED_IN:
        previous, current = meta.get("previous_progress"), meta.get("current_progress")
        if previous is not None and current is not None:
            return f"Check-in: {_percent(previous)}% → {_percent(current)}%"
        return "Check-in"
    if action == ActivityAction.REJECTED:
        reason = str(meta.get("reason") or "").strip()
        return f"Yêu cầu chỉnh sửa: {reason}" if reason else "Yêu cầu chỉnh sửa"
    if action == ActivityAction.APPROVED and meta.get("approved_key_result_count") is not None:
        return f"Đã duyệt cùng {meta['approved_key_result_count']} Key Result"
    if action == ActivityAction.CYCLE_STATUS_CHANGED:
        before = _STATUS_LABELS.get(meta.get("from"), meta.get("from"))
        after = _STATUS_LABELS.get(meta.get("to"), meta.get("to"))
        return f"Trạng thái chu kỳ: {before} → {after}"
    if action == ActivityAction.CYCLE_CLONED:
        return f"Đã sao chép {meta.get('objective_count', 0)} Objective từ chu kỳ khác"
    labels = {
        ActivityAction.CREATED: "Đã tạo",
        ActivityAction.UPDATED: "Đã cập nhật",
        ActivityAction.DELETED: "Đã xóa",
        ActivityAction.SUBMITTED: "Đã gửi duyệt",
        ActivityAction.APPROVED: "Đã duyệt",
        ActivityAction.COMMENTED: "Đã bình luận",
        ActivityAction.EVIDENCE_ADDED: "Đã thêm bằng chứng",
    }
    return labels.get(action, action)
