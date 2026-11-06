"""Cycle management (JOYOKR ``JoyOKR_Cycle_Controller``, ``JoyOKR_Cycle_Repository``
and ``JoyOKR_OKR_Service::clone_cycle``)."""

from __future__ import annotations

import datetime
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from okr.models import OKRCycle, OKRKeyResult, OKRObjective
from okr.models.choices import ActivityAction, ApprovalStatus, CycleStatus, CycleType
from okr.services import activity, errors
from okr.services.access import OKRViewer

# JOYOKR allows reopening a closed cycle; a cycle never goes back to draft.
TRANSITIONS = {
    CycleStatus.DRAFT: (CycleStatus.ACTIVE,),
    CycleStatus.ACTIVE: (CycleStatus.CLOSED,),
    CycleStatus.CLOSED: (CycleStatus.ACTIVE,),
}


def cycles_for(company_id: int):
    return OKRCycle.objects.for_company(company_id)


def get_cycle(viewer: OKRViewer, cycle_id) -> OKRCycle:
    cycle = cycles_for(viewer.company_id).filter(pk=cycle_id).first() if cycle_id else None
    if cycle is None:
        raise errors.not_found("cycle")
    return cycle


DEFAULT_CYCLE_RULE = (
    "Khi chưa chọn chu kỳ: dùng chu kỳ đang hoạt động có ngày hôm nay nằm trong khoảng thời gian "
    "(bắt đầu gần nhất); nếu không có, dùng chu kỳ đang hoạt động bắt đầu gần nhất."
)


def active_cycle(company_id: int, today: Optional[datetime.date] = None) -> Optional[OKRCycle]:
    """The default cycle when none is asked for.

    JOYOKR lets several cycles be active at once and simply took the newest
    one (``active()``), so an upcoming quarter activated early hid the
    current one. Rule here (``DEFAULT_CYCLE_RULE``): the active cycle that
    covers today, the latest-starting one if several do; else the newest
    active cycle."""
    today = today or timezone.localdate()
    active = cycles_for(company_id).filter(status=CycleStatus.ACTIVE)
    current = active.filter(start_date__lte=today, end_date__gte=today).order_by("-start_date", "-id").first()
    return current or active.order_by("-start_date", "-id").first()


def cycle_selection(viewer: OKRViewer, cycle: OKRCycle, requested_id=None) -> dict:
    """How the shown cycle was chosen, for screens to say so when several are active."""
    active = list(
        cycles_for(viewer.company_id)
        .filter(status=CycleStatus.ACTIVE)
        .order_by("-start_date", "-id")
        .values("id", "name", "start_date", "end_date")
    )
    return {
        "selected_id": cycle.pk,
        "selected_by": "request" if requested_id else "default",
        "rule": DEFAULT_CYCLE_RULE,
        "active_cycles": active,
    }


def latest_with_objectives(company_id: int, today: Optional[datetime.date] = None) -> Optional[OKRCycle]:
    """Fallback when nothing is active: the cycle covering today, else the
    newest one, among cycles that still hold objectives."""
    today = today or timezone.localdate()
    candidates = list(
        cycles_for(company_id)
        .filter(objectives__deleted_at__isnull=True, objectives__isnull=False)
        .distinct()
        .order_by("-start_date", "-id")
    )
    current = [cycle for cycle in candidates if cycle.start_date <= today <= cycle.end_date]
    return (current or candidates or [None])[0]


def resolve_cycle(viewer: OKRViewer, cycle_id=None, *, fallback_latest: bool = False) -> OKRCycle:
    """The requested cycle, else the active one; 404 when there is none.

    With ``fallback_latest`` a company with no active cycle still gets its
    newest cycle that has objectives (JOYOKR does this for read-only views
    such as the tree, so past OKRs do not vanish)."""
    if cycle_id:
        return get_cycle(viewer, cycle_id)
    cycle = active_cycle(viewer.company_id)
    if cycle is None and fallback_latest:
        cycle = latest_with_objectives(viewer.company_id)
    if cycle is None:
        raise errors.OKRError("no_cycle", "Chưa có chu kỳ OKR đang hoạt động.", 404)
    return cycle


def _require_admin(viewer: OKRViewer):
    if not (viewer.is_active and viewer.is_admin):
        raise errors.forbidden("Chỉ quản trị OKR mới quản lý được chu kỳ.")


def _parse_date(value):
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def _cycle_data(viewer: OKRViewer, data: dict, exclude_id: Optional[int] = None) -> dict:
    name = str(data.get("name") or "").strip()
    cycle_type = data.get("cycle_type") or CycleType.CUSTOM
    if cycle_type not in CycleType.values:
        cycle_type = CycleType.CUSTOM
    start, end = _parse_date(data.get("start_date")), _parse_date(data.get("end_date"))
    parent_id = data.get("parent_id") or None
    problems = {}
    if not name:
        problems["name"] = "Tên chu kỳ là bắt buộc."
    if start is None:
        problems["start_date"] = "Ngày bắt đầu không hợp lệ."
    if end is None or (start and end < start):
        problems["end_date"] = "Ngày kết thúc phải sau ngày bắt đầu."
    if start and end and end >= start:
        overlap = cycles_for(viewer.company_id).filter(
            cycle_type=cycle_type, start_date__lte=end, end_date__gte=start
        )
        if exclude_id:
            overlap = overlap.exclude(pk=exclude_id)
        if overlap.exists():
            problems["start_date"] = "Chu kỳ cùng loại đang bị trùng thời gian."
    parent = None
    if parent_id:
        parent = cycles_for(viewer.company_id).filter(pk=parent_id).first()
        if parent is None or parent.pk == exclude_id:
            problems["parent_id"] = "Chu kỳ cha không hợp lệ."
    if problems:
        raise errors.validation(problems)
    return {"name": name[:190], "cycle_type": cycle_type, "start_date": start, "end_date": end, "parent": parent}


def create_cycle(viewer: OKRViewer, data: dict) -> OKRCycle:
    _require_admin(viewer)
    values = _cycle_data(viewer, data)
    return OKRCycle.objects.create(company_id=viewer.company_id, status=CycleStatus.DRAFT, **values)


def update_cycle(viewer: OKRViewer, cycle_id, data: dict) -> OKRCycle:
    _require_admin(viewer)
    cycle = get_cycle(viewer, cycle_id)
    # Stricter than JOYOKR, which let a closed cycle be edited: changing its
    # dates would silently rewrite the expected progress of finished OKRs.
    # An admin reopens the cycle first, which is logged.
    if cycle.status == CycleStatus.CLOSED:
        raise errors.rule("cycle_closed", "Chu kỳ đã đóng. Hãy mở lại chu kỳ trước khi sửa.")
    for field, value in _cycle_data(viewer, data, exclude_id=cycle.pk).items():
        setattr(cycle, field, value)
    cycle.save()
    return cycle


def change_status(viewer: OKRViewer, cycle_id, status: str) -> OKRCycle:
    _require_admin(viewer)
    with transaction.atomic():
        cycle = cycles_for(viewer.company_id).select_for_update().filter(pk=cycle_id).first()
        if cycle is None:
            raise errors.not_found("cycle")
        if status not in TRANSITIONS.get(cycle.status, ()):
            raise errors.validation({"status": "Chuyển trạng thái chu kỳ không hợp lệ."})
        previous = cycle.status
        cycle.status = status
        cycle.save(update_fields=["status", "updated_at"])
        activity.log(
            viewer.employee,
            ActivityAction.CYCLE_STATUS_CHANGED,
            cycle=cycle,
            metadata={"from": previous, "to": status},
        )
    return cycle


def clone_cycle(viewer: OKRViewer, source_id, destination_id) -> dict:
    """Copy every objective and key result of one cycle into an empty draft
    cycle as fresh drafts: progress 0, current value back to start, due
    dates at the destination's end, alignment links remapped."""
    _require_admin(viewer)
    if not destination_id:
        raise errors.validation({"destination_cycle_id": "Vui lòng chọn chu kỳ đích."})
    source = cycles_for(viewer.company_id).filter(pk=source_id).first()
    destination = cycles_for(viewer.company_id).filter(pk=destination_id).first()
    if source is None or destination is None:
        raise errors.OKRError("cycle_not_found", "Không tìm thấy chu kỳ nguồn hoặc đích.", 404)
    if source.pk == destination.pk or destination.status != CycleStatus.DRAFT:
        raise errors.rule(
            "invalid_clone_target", "Chu kỳ đích phải khác chu kỳ nguồn và đang ở trạng thái nháp."
        )
    if OKRObjective.objects.for_company(viewer.company_id).filter(cycle=destination).exists():
        raise errors.conflict("clone_target_not_empty", "Chu kỳ đích đã có Objective.")

    objectives = list(
        OKRObjective.objects.for_company(viewer.company_id)
        .filter(cycle=source)
        .prefetch_related("key_results")
        .order_by("id")
    )
    id_map = {}
    try:
        with transaction.atomic():
            for objective in objectives:
                copy = OKRObjective.objects.create(
                    company_id=viewer.company_id,
                    cycle=destination,
                    scope=objective.scope,
                    department_id=objective.department_id,
                    team_id=objective.team_id,
                    owner_id=objective.owner_id,
                    title=objective.title,
                    description=objective.description,
                    weight=objective.weight,
                    priority=objective.priority,
                    approval_status=ApprovalStatus.DRAFT,
                )
                id_map[objective.pk] = copy
                for key_result in objective.key_results.all():
                    OKRKeyResult.objects.create(
                        company_id=viewer.company_id,
                        objective=copy,
                        owner_id=key_result.owner_id,
                        title=key_result.title,
                        description=key_result.description,
                        unit=key_result.unit,
                        start_value=key_result.start_value,
                        current_value=key_result.start_value,
                        target_value=key_result.target_value,
                        weight=key_result.weight,
                        priority=key_result.priority,
                        due_date=destination.end_date,
                        approval_status=ApprovalStatus.DRAFT,
                    )
            for objective in objectives:
                parent = id_map.get(objective.parent_id)
                if parent is not None:
                    copy = id_map[objective.pk]
                    copy.parent = parent
                    copy.save(update_fields=["parent", "updated_at"])
            activity.log(
                viewer.employee,
                ActivityAction.CYCLE_CLONED,
                cycle=destination,
                metadata={"source_cycle_id": source.pk, "objective_count": len(objectives)},
            )
    except ValidationError as exc:
        raise errors.rule("clone_failed", "Không thể clone OKR sang chu kỳ mới.") from exc
    return {"objective_count": len(objectives), "destination_cycle_id": destination.pk}
