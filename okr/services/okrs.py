"""Objective and key result workflow (JOYOKR ``JoyOKR_OKR_Service`` and the
create/update/delete rules of ``JoyOKR_OKR_Controller``).

Checks run in the same order as JOYOKR so a request that breaks several
rules gets the same error. Each write and its activity entries and
notifications commit together.
"""

from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from base.models import Department
from okr.models import OKRCheckin, OKRCycle, OKREvidence, OKRKeyResult, OKRObjective, OKRTeam
from okr.models.base import department_allows_company, employee_company_id
from okr.models.choices import (
    ActivityAction,
    ApprovalStatus,
    CycleStatus,
    Priority,
    RecordStatus,
    ReferenceType,
    Scope,
)
from okr.services import activity, calculator, errors, notifications
from okr.services.access import EDITABLE_CYCLE_STATUSES, LOCKED_STATUSES, OKRViewer, ScopeRef, scope_of
from okr import pilot

SCOPE_LEVELS = {Scope.COMPANY: 0, Scope.DEPARTMENT: 1, Scope.TEAM: 2, Scope.PERSONAL: 3}
METRIC_LIMIT = Decimal("100000000000000")  # DECIMAL(18,4) holds |x| < 10^14
FOUR_PLACES = Decimal("0.0001")


# -- lookups -------------------------------------------------------------------


def objectives_for(company_id: int):
    return OKRObjective.objects.for_company(company_id).select_related(
        "cycle", "owner", "department", "team", "parent"
    )


def key_results_for(company_id: int):
    return (
        OKRKeyResult.objects.for_company(company_id)
        .filter(objective__deleted_at__isnull=True)
        .select_related("objective__cycle", "objective__department", "objective__team", "owner")
    )


def get_objective(viewer: OKRViewer, objective_id) -> OKRObjective:
    objective = objectives_for(viewer.company_id).filter(pk=objective_id).first() if objective_id else None
    if objective is None:
        if objective_id and OKRObjective.all_objects.for_company(viewer.company_id).filter(
            pk=objective_id, deleted_at__isnull=False
        ).exists():
            raise errors.OKRError("deleted", "Objective này đã bị xóa.", 404)
        raise errors.not_found("objective")
    return objective


def get_key_result(viewer: OKRViewer, key_result_id) -> OKRKeyResult:
    key_result = key_results_for(viewer.company_id).filter(pk=key_result_id).first() if key_result_id else None
    if key_result is None:
        if key_result_id and OKRKeyResult.all_objects.for_company(viewer.company_id).filter(pk=key_result_id).exists():
            raise errors.OKRError(
                "deleted", "Key Result này đã bị xóa hoặc Objective cha không còn tồn tại.", 404
            )
        raise errors.not_found("key_result")
    return key_result


def visible_objective(viewer: OKRViewer, objective_id) -> OKRObjective:
    """An objective the viewer may see. One they may not see is reported as
    not found (JOYOKR said 403), so its existence does not leak."""
    objective = get_objective(viewer, objective_id)
    if not viewer.can_view_scope(scope_of(objective)):
        raise errors.not_found("objective")
    return objective


def visible_key_result(viewer: OKRViewer, key_result_id) -> OKRKeyResult:
    key_result = get_key_result(viewer, key_result_id)
    if not viewer.can_view_scope(scope_of(key_result)):
        raise errors.not_found("key_result")
    return key_result


def alive_key_results(objective: OKRObjective):
    return list(objective.key_results.order_by("id"))


def weight_total(objective: OKRObjective) -> Decimal:
    """SQL SUM of the live key results' weights (JOYOKR ``key_result_weight_total``)."""
    return objective.key_results.aggregate(total=Sum("weight"))["total"] or Decimal("0")


# -- progress -----------------------------------------------------------------


def recalculate_objective(objective: OKRObjective) -> OKRObjective:
    """Approved objectives count approved key results only; others count all."""
    key_results = alive_key_results(objective)
    if objective.approval_status == ApprovalStatus.APPROVED:
        key_results = [kr for kr in key_results if kr.approval_status == ApprovalStatus.APPROVED]
    objective.progress = calculator.weighted_progress((kr.progress, kr.weight) for kr in key_results)
    objective.save(update_fields=["progress", "updated_at"])
    return objective


# -- approval workflow ----------------------------------------------------------


def _count_ok(count: int) -> bool:
    return 2 <= count <= 4


def _aligned(objective: OKRObjective) -> bool:
    return objective.scope == Scope.COMPANY or objective.parent_id is not None


def _key_result_reviewable(key_result: OKRKeyResult, cycle: OKRCycle) -> bool:
    if not (key_result.unit or "").strip():
        return False
    if float(key_result.target_value) == float(key_result.start_value):
        return False
    due = key_result.due_date
    return due is None or cycle.start_date <= due <= cycle.end_date


def approval_checks(objective: OKRObjective, key_results=None) -> dict:
    """What the approve step will test, for the approval queue."""
    key_results = alive_key_results(objective) if key_results is None else key_results
    total = weight_total(objective)
    return {
        "key_result_count_ok": _count_ok(len(key_results)),
        "weight_total": total,
        "weight_ok": calculator.weight_total_ok(total),
        "alignment_ok": _aligned(objective),
        "key_results_ok": all(_key_result_reviewable(kr, objective.cycle) for kr in key_results),
    }


def submit_objective(viewer: OKRViewer, objective_id) -> dict:
    with transaction.atomic():
        objective = get_objective(viewer, objective_id)
        objective = OKRObjective.objects.select_for_update().select_related("cycle").get(pk=objective.pk)
        ref = scope_of(objective)
        if not viewer.can_edit_okr(ref):
            raise errors.forbidden()
        if not _count_ok(len(alive_key_results(objective))):
            raise errors.rule(
                "invalid_kr_count", "Mỗi Objective cần từ 2 đến 4 Key Result trước khi gửi duyệt."
            )
        if not calculator.weight_total_ok(weight_total(objective)):
            raise errors.rule("invalid_weight", "Tổng trọng số Key Result phải bằng 100%.")
        if not _aligned(objective):
            raise errors.rule(
                "alignment_required",
                "Objective cấp dưới phải được liên kết (Aligned với) một Objective cấp trên trước khi gửi duyệt.",
            )
        if objective.approval_status not in (ApprovalStatus.DRAFT, ApprovalStatus.REJECTED):
            raise errors.conflict("invalid_transition", "Nội dung không ở trạng thái có thể gửi duyệt.")
        objective.approval_status = ApprovalStatus.SUBMITTED
        objective.save(update_fields=["approval_status", "updated_at"])
        activity.log(viewer.employee, ActivityAction.SUBMITTED, objective=objective)
        sent = _notify_approvers(objective, viewer)
    message = (
        f"Đã gửi duyệt và lưu {len(sent)} thông báo vào dữ liệu."
        if sent
        else "Đã gửi duyệt nhưng chưa tìm thấy người có quyền nhận thông báo."
    )
    return {"objective": objective, "notification_count": len(sent), "message": message}


def approver_candidates(objective: OKRObjective) -> list:
    """Owner's reporting manager, department OKR manager, team manager, then
    every OKR admin of the company (JOYOKR ``notify_approvers``)."""
    from employee.models import Employee, EmployeeWorkInformation

    from okr.models import OKRDepartmentRole, OKRRole
    from okr.models.choices import OKRRoleType

    ids = []
    if objective.owner_id:
        manager_id = (
            EmployeeWorkInformation._base_manager.filter(employee_id=objective.owner_id)
            .values_list("reporting_manager_id", flat=True)
            .first()
        )
        if manager_id:
            ids.append(manager_id)
    if objective.department_id:
        ids.extend(
            OKRDepartmentRole.objects.for_company(objective.company_id)
            .filter(department_id=objective.department_id, department__is_active=True)
            .values_list("manager_id", flat=True)
        )
    if objective.team_id:
        team_manager = (
            OKRTeam.objects.for_company(objective.company_id)
            .filter(pk=objective.team_id, status=RecordStatus.ACTIVE)
            .values_list("manager_id", flat=True)
            .first()
        )
        if team_manager:
            ids.append(team_manager)
    ids.extend(
        OKRRole.objects.for_company(objective.company_id)
        .filter(role=OKRRoleType.ADMIN)
        .order_by("employee_id")
        .values_list("employee_id", flat=True)
    )
    unique_ids = list(dict.fromkeys(i for i in ids if i))
    people = Employee._base_manager.select_related("employee_user_id").in_bulk(unique_ids)
    return [people[i] for i in unique_ids if i in people]


def _notify_approvers(objective: OKRObjective, actor: OKRViewer) -> list:
    ref = scope_of(objective)
    sent = []
    for candidate in approver_candidates(objective):
        approver = OKRViewer.for_employee(candidate)
        if approver is None or approver.company_id != objective.company_id:
            continue
        if candidate.pk in (actor.employee_id, objective.owner_id) and not approver.is_admin:
            continue
        if not approver.can_review(ref):
            continue
        notification = notifications.send(
            candidate,
            company_id=objective.company_id,
            notification_type=notifications.APPROVAL_REQUESTED,
            title=f"Chờ duyệt: {objective.title}",
            message="Có OKR mới được gửi. Mở Duyệt OKR để xử lý.",
            reference_type=ReferenceType.OBJECTIVE,
            reference_id=objective.pk,
            dedupe_key=approval_dedupe_key(objective, candidate.pk),
            actor=actor.employee,
        )
        if notification is not None:
            sent.append(notification)
    return sent


def approval_dedupe_key(objective: OKRObjective, recipient_id: int) -> str:
    return f"approval:objective:{objective.pk}:user:{recipient_id}:{notifications.stamp(objective.updated_at)}"


def _review_guard(viewer: OKRViewer, objective: OKRObjective, verb: str):
    """The checks approve and reject share, in JOYOKR's order."""
    ref = scope_of(objective)
    if not viewer.can_review(ref):
        raise errors.forbidden()
    if objective.cycle.status not in EDITABLE_CYCLE_STATUSES:
        raise errors.conflict("cycle_closed", f"Không thể {verb} OKR trong chu kỳ đã đóng.")
    if objective.owner_id == viewer.employee_id and not viewer.is_admin:
        raise errors.OKRError(
            "self_approval", "Manager không thể tự duyệt hoặc từ chối OKR của chính mình.", 403
        )


def approve_objective(viewer: OKRViewer, objective_id) -> dict:
    """Approve an objective and all its key results as one unit."""
    with transaction.atomic():
        objective = get_objective(viewer, objective_id)
        objective = OKRObjective.objects.select_for_update().select_related("cycle").get(pk=objective.pk)
        _review_guard(viewer, objective, "duyệt")
        if objective.approval_status != ApprovalStatus.SUBMITTED:
            raise errors.conflict("invalid_transition", "Chỉ nội dung đã gửi duyệt mới có thể được duyệt.")
        if not _aligned(objective):
            raise errors.rule(
                "alignment_required", "Hãy liên kết Objective cấp dưới với Objective cấp trên trước khi duyệt."
            )
        key_results = alive_key_results(objective)
        if not _count_ok(len(key_results)) or not calculator.weight_total_ok(weight_total(objective)):
            raise errors.rule(
                "invalid_weight", "Objective cần 2–4 Key Result và tổng trọng số bằng 100% trước khi duyệt."
            )
        if not all(_key_result_reviewable(kr, objective.cycle) for kr in key_results):
            raise errors.rule(
                "invalid_key_result", "KR cần đơn vị rõ ràng, mục tiêu khác bắt đầu và hạn nằm trong chu kỳ."
            )
        progress = calculator.weighted_progress((kr.progress, kr.weight) for kr in key_results)
        OKRKeyResult.objects.filter(pk__in=[kr.pk for kr in key_results]).update(
            approval_status=ApprovalStatus.APPROVED, updated_at=timezone.now()
        )
        objective.approval_status = ApprovalStatus.APPROVED
        objective.progress = progress
        objective.save(update_fields=["approval_status", "progress", "updated_at"])
        for key_result in key_results:
            activity.log(
                viewer.employee,
                ActivityAction.APPROVED,
                key_result=key_result,
                metadata={"via_objective_id": objective.pk},
            )
            notifications.mark_reference_read(
                objective.company_id, ReferenceType.KEY_RESULT, key_result.pk, notifications.APPROVAL_REQUESTED
            )
        activity.log(
            viewer.employee,
            ActivityAction.APPROVED,
            objective=objective,
            metadata={"approved_key_result_count": len(key_results)},
        )
        sent = _notify_owner(objective, ApprovalStatus.APPROVED, "", viewer.employee)
    return {
        "objective": objective,
        "notification_count": 1 if sent else 0,
        "message": "Đã duyệt OKR và lưu thông báo cho owner.",
    }


def reject_objective(viewer: OKRViewer, objective_id, reason: str) -> dict:
    reason = (reason or "").strip()
    with transaction.atomic():
        objective = get_objective(viewer, objective_id)
        objective = OKRObjective.objects.select_for_update().select_related("cycle").get(pk=objective.pk)
        _review_guard(viewer, objective, "từ chối")
        if objective.approval_status != ApprovalStatus.SUBMITTED or not reason:
            raise errors.OKRError(
                "invalid_rejection",
                "Chỉ nội dung đã gửi duyệt và có lý do mới có thể bị từ chối.",
                400,
                {"reason": "Vui lòng nhập lý do."} if not reason else {},
            )
        objective.approval_status = ApprovalStatus.REJECTED
        objective.save(update_fields=["approval_status", "updated_at"])
        activity.log(viewer.employee, ActivityAction.REJECTED, objective=objective, metadata={"reason": reason})
        sent = _notify_owner(objective, ApprovalStatus.REJECTED, reason, viewer.employee)
    return {
        "objective": objective,
        "notification_count": 1 if sent else 0,
        "message": "Đã gửi yêu cầu chỉnh sửa và lưu thông báo cho owner.",
    }


def _notify_owner(objective: OKRObjective, status: str, reason: str, actor=None):
    notifications.mark_reference_read(
        objective.company_id, ReferenceType.OBJECTIVE, objective.pk, notifications.APPROVAL_REQUESTED
    )
    if objective.owner_id is None:
        return None
    return notifications.send(
        objective.owner,
        company_id=objective.company_id,
        notification_type=status,
        title=objective.title,
        message=reason,
        reference_type=ReferenceType.OBJECTIVE,
        reference_id=objective.pk,
        dedupe_key=(
            f"{status}:objective:{objective.pk}:user:{objective.owner_id}:"
            f"{notifications.stamp(objective.updated_at)}"
        ),
        actor=actor,
    )


def rejection_reason(objective: OKRObjective) -> Optional[str]:
    """Reason of the latest rejection, while the objective is still rejected."""
    if objective.approval_status != ApprovalStatus.REJECTED:
        return None
    entry = objective.activities.filter(action=ActivityAction.REJECTED).order_by("-created_at", "-id").first()
    return (entry.metadata or {}).get("reason") if entry else None


def submitted_at(objective: OKRObjective):
    entry = objective.activities.filter(action=ActivityAction.SUBMITTED).order_by("-created_at", "-id").first()
    return entry.created_at if entry else None


# -- check-in -----------------------------------------------------------------


def parse_metric(value, field: str, problems: dict) -> Optional[Decimal]:
    """A DECIMAL(18,4) number from user input, or a message in ``problems``."""
    if value is None or (isinstance(value, str) and not value.strip()) or isinstance(value, bool):
        problems[field] = "Vui lòng nhập một số hợp lệ."
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        problems[field] = "Vui lòng nhập một số hợp lệ."
        return None
    if not number.is_finite() or abs(number) >= METRIC_LIMIT:
        problems[field] = "Vui lòng nhập một số hợp lệ."
        return None
    if number != number.quantize(FOUR_PLACES):
        problems[field] = "Tối đa 4 chữ số thập phân."
        return None
    return number.quantize(FOUR_PLACES)


def checkin(
    viewer: OKRViewer,
    key_result_id,
    *,
    current_value,
    confidence,
    update_text: str,
    blockers: str = "",
    next_steps: str = "",
    client_request_id: Optional[str] = None,
) -> dict:
    """Record a check-in on a key result the viewer owns.

    Returns ``{"checkin", "key_result", "created"}``; ``created`` is False
    when ``client_request_id`` matched an earlier check-in by the same
    person, which is returned unchanged.
    """
    problems = {}
    try:
        confidence = int(confidence)
    except (TypeError, ValueError):
        confidence = 0
    if isinstance(confidence, bool) or not 1 <= confidence <= 5:
        problems["confidence"] = "Confidence phải từ 1 đến 5."
    value = parse_metric(current_value, "current_value", problems)
    update_text = (update_text or "").strip()
    if not update_text:
        problems["update_text"] = "Kết quả đã làm là bắt buộc."
    client_request_id = (client_request_id or "").strip() or None
    if client_request_id and len(client_request_id) > 64:
        problems["client_request_id"] = "Mã yêu cầu quá dài."
    if problems:
        raise errors.validation(problems)

    if client_request_id:
        replay = _replayed_checkin(viewer, client_request_id, key_result_id)
        if replay is not None:
            return replay

    key_result = get_key_result(viewer, key_result_id)
    if key_result.owner_id != viewer.employee_id:
        raise errors.forbidden()
    objective = key_result.objective
    if key_result.approval_status != ApprovalStatus.APPROVED or objective.approval_status != ApprovalStatus.APPROVED:
        raise errors.conflict(
            "checkin_not_approved", "Objective và Key Result đều phải được duyệt trước khi check-in."
        )
    if objective.cycle.status != CycleStatus.ACTIVE:
        raise errors.conflict("cycle_not_active", "Chỉ được check-in trong chu kỳ đang hoạt động.")
    if not viewer.is_active:
        raise errors.forbidden()

    try:
        with transaction.atomic():
            key_result = OKRKeyResult.objects.select_for_update().get(pk=key_result.pk)
            current_progress = calculator.progress(key_result.start_value, key_result.target_value, value)
            entry = OKRCheckin.objects.create(
                company_id=key_result.company_id,
                key_result=key_result,
                author=viewer.employee,
                previous_value=key_result.current_value,
                current_value=value,
                previous_progress=key_result.progress,
                current_progress=current_progress,
                confidence=confidence,
                update_text=update_text,
                blockers=(blockers or "").strip(),
                next_steps=(next_steps or "").strip(),
                client_request_id=client_request_id,
            )
            previous_progress = key_result.progress
            key_result.current_value = value
            key_result.progress = current_progress
            key_result.save(update_fields=["current_value", "progress", "updated_at"])
            activity.log(
                viewer.employee,
                ActivityAction.CHECKED_IN,
                key_result=key_result,
                metadata={
                    "checkin_id": entry.pk,
                    "previous_progress": previous_progress,
                    "current_progress": current_progress,
                },
            )
            recalculate_objective(objective)
            _notify_manager_of_checkin(viewer, key_result, update_text)
    except IntegrityError:
        if client_request_id:
            replay = _replayed_checkin(viewer, client_request_id, key_result_id)
            if replay is not None:
                return replay
        raise
    key_result = get_key_result(viewer, key_result.pk)
    return {"checkin": entry, "key_result": key_result, "created": True}


def _replayed_checkin(viewer: OKRViewer, client_request_id: str, key_result_id) -> Optional[dict]:
    earlier = (
        OKRCheckin.objects.for_company(viewer.company_id)
        .filter(author_id=viewer.employee_id, client_request_id=client_request_id)
        .first()
    )
    if earlier is None:
        return None
    if str(earlier.key_result_id) != str(key_result_id):
        raise errors.conflict(
            "client_request_reused", "Mã yêu cầu này đã được dùng cho một check-in khác."
        )
    key_result = (
        OKRKeyResult.all_objects.select_related("objective__cycle", "owner").get(pk=earlier.key_result_id)
    )
    return {"checkin": earlier, "key_result": key_result, "created": False}


def _notify_manager_of_checkin(viewer: OKRViewer, key_result: OKRKeyResult, update_text: str):
    from employee.models import Employee, EmployeeWorkInformation

    manager_id = (
        EmployeeWorkInformation._base_manager.filter(employee_id=key_result.owner_id)
        .values_list("reporting_manager_id", flat=True)
        .first()
    )
    if not manager_id or manager_id == viewer.employee_id:
        return None
    manager = Employee._base_manager.filter(pk=manager_id).first()
    if manager is None or employee_company_id(manager) != key_result.company_id:
        return None
    return notifications.send(
        manager,
        company_id=key_result.company_id,
        notification_type="checkin",
        title=key_result.title,
        message=update_text,
        reference_type=ReferenceType.KEY_RESULT,
        reference_id=key_result.pk,
        actor=viewer.employee,
    )


# -- definitions: create / update / delete ------------------------------------------


def _int_or_none(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _owner_input(viewer: OKRViewer, data: dict):
    """The requested owner; the caller when none is named. An owner that HR
    detached (None) stays invalid instead of silently becoming the caller."""
    if "owner_id" not in data:
        return viewer.employee_id
    return _int_or_none(data["owner_id"])


def active_employee(company_id: int, employee_id):
    from employee.models import Employee

    if not employee_id:
        return None
    employee = Employee._base_manager.select_related("employee_user_id").filter(pk=employee_id).first()
    if employee is None or not employee.is_active or employee_company_id(employee) != company_id:
        return None
    user = employee.employee_user_id
    return employee if user is not None and user.is_active else None


def _objective_data(viewer: OKRViewer, data: dict, *, current: Optional[OKRObjective] = None) -> dict:
    company_id = viewer.company_id
    problems = {}
    title = str(data.get("title") or "").strip()
    cycle_id = _int_or_none(data.get("cycle_id"))
    owner_id = _owner_input(viewer, data)
    scope = data.get("scope") or Scope.PERSONAL
    if scope not in Scope.values:
        scope = Scope.PERSONAL
    department_id = _int_or_none(data.get("department_id"))
    team_id = _int_or_none(data.get("team_id"))

    if not title:
        problems["title"] = "Tên Objective là bắt buộc."
    cycle = OKRCycle.objects.for_company(company_id).filter(pk=cycle_id).first() if cycle_id else None
    if cycle is None:
        problems["cycle_id"] = "Chu kỳ không hợp lệ."
    owner = active_employee(company_id, owner_id)
    if owner is None:
        problems["owner_id"] = "Owner không hợp lệ."
    elif not pilot.allows(owner.pk):
        problems["owner_id"] = pilot.OWNER_NOT_IN_PILOT
    if scope == Scope.DEPARTMENT and not department_id:
        problems["department_id"] = "Vui lòng chọn phòng ban."
    if scope == Scope.TEAM and not team_id:
        problems["team_id"] = "Vui lòng chọn team."
    department = Department._base_manager.filter(pk=department_id).first() if department_id else None
    team = OKRTeam.objects.for_company(company_id).filter(pk=team_id).select_related("department").first() if team_id else None
    if department_id and (
        department is None or not department.is_active or not department_allows_company(department, company_id)
    ):
        problems["department_id"] = "Phòng ban không hợp lệ."
    team_department = team.department if team is not None else None
    if team_id and (
        team is None
        or team.status != RecordStatus.ACTIVE
        or team_department is None
        or not team_department.is_active
    ):
        problems["team_id"] = "Team không hợp lệ hoặc thuộc phòng ban đã ẩn."
    elif team_id and department_id and team.department_id != department_id:
        problems["team_id"] = "Team phải thuộc phòng ban đã chọn."
    if scope == Scope.TEAM and team is not None and "team_id" not in problems:
        department_id = team.department_id
    if scope == Scope.DEPARTMENT:
        team_id = None
    elif scope in (Scope.COMPANY, Scope.PERSONAL):
        department_id = team_id = None

    parent_id = _int_or_none(data.get("parent_id"))
    parent = None
    if parent_id:
        parent = objectives_for(company_id).filter(pk=parent_id).first()
        if (
            parent is None
            or parent.cycle_id != cycle_id
            or not viewer.can_view_scope(scope_of(parent))
            or SCOPE_LEVELS.get(parent.scope, 99) >= SCOPE_LEVELS[scope]
        ):
            problems["parent_id"] = (
                "Objective liên kết phải thuộc cùng chu kỳ, ở cấp cao hơn và nằm trong phạm vi được xem."
            )
        elif current is not None and parent.pk == current.pk:
            problems["parent_id"] = "Objective không thể liên kết với chính nó."
    if problems:
        raise errors.validation(problems)

    try:
        weight = Decimal(str(data.get("weight", "100")))
    except (InvalidOperation, ValueError):
        weight = Decimal("100")
    if not weight.is_finite():
        weight = Decimal("100")
    priority = data.get("priority") or Priority.MEDIUM
    return {
        "cycle": cycle,
        "parent": parent,
        "scope": scope,
        "department_id": department_id,
        "team_id": team_id,
        "owner": owner,
        "title": title[:255],
        "description": str(data.get("description") or "").strip(),
        "weight": min(Decimal("100"), max(Decimal("0"), weight)).quantize(Decimal("0.01")),
        "priority": priority if priority in Priority.values else Priority.MEDIUM,
    }


def _notify_assignment(viewer: OKRViewer, reference_type: str, item, owner):
    if owner is None or owner.pk == viewer.employee_id:
        return None
    return notifications.send(
        owner,
        company_id=item.company_id,
        notification_type="assigned",
        title=item.title or "OKR mới được giao",
        message="Bạn vừa được giao phụ trách một OKR.",
        reference_type=reference_type,
        reference_id=item.pk,
        actor=viewer.employee,
    )


def create_objective(viewer: OKRViewer, data: dict) -> OKRObjective:
    values = _objective_data(viewer, data)
    if not viewer.is_active or not viewer.can_create_objective(
        scope=values["scope"],
        owner_id=values["owner"].pk,
        department_id=values["department_id"],
        team_id=values["team_id"],
        cycle_status=values["cycle"].status,
    ):
        raise errors.forbidden()
    with transaction.atomic():
        objective = OKRObjective.objects.create(company_id=viewer.company_id, **values)
        activity.log(viewer.employee, ActivityAction.CREATED, objective=objective)
        _notify_assignment(viewer, ReferenceType.OBJECTIVE, objective, values["owner"])
    return objective


ASSOCIATION_FIELDS = ("cycle_id", "scope", "department_id", "team_id", "owner_id")


def update_objective(viewer: OKRViewer, objective_id, data: dict) -> OKRObjective:
    current = get_objective(viewer, objective_id)
    if not viewer.can_edit_okr(scope_of(current)):
        raise errors.forbidden()
    if not viewer.can_modify_definition(scope_of(current)):
        raise errors.conflict(
            "objective_locked",
            "Objective đang chờ duyệt hoặc đã duyệt nên không thể sửa. Nếu cần thay đổi, hãy ghi lý do ở "
            "Bình luận và liên hệ Manager/Admin.",
        )
    data = dict(data)
    if not viewer.is_admin:
        for field in ASSOCIATION_FIELDS:
            data[field] = getattr(current, field)
    values = _objective_data(viewer, data, current=current)
    proposed = ScopeRef(
        company_id=viewer.company_id,
        owner_id=values["owner"].pk,
        scope=values["scope"],
        department_id=values["department_id"],
        team_id=values["team_id"],
        approval_status=current.approval_status,
        objective_approval_status=None,
        cycle_status=values["cycle"].status,
    )
    if not viewer.can_edit_okr(proposed):
        raise errors.forbidden()
    previous_owner_id = current.owner_id
    with transaction.atomic():
        for field, value in values.items():
            setattr(current, field, value)
        current.save()
        metadata = {"fields": sorted(values)}
        moved = []
        if previous_owner_id != current.owner_id:
            # Reassignment. A key result the previous owner held moves with the
            # objective only while it has no history: no check-in and no evidence.
            # One with history stays with the person who did the work until an
            # admin hands it over explicitly (edit that key result's owner); its
            # check-ins and evidence are never touched. Key results given to
            # someone else keep their owner.
            held = list(OKRKeyResult.objects.filter(objective=current, owner_id=previous_owner_id).order_by("id"))
            with_history = set(
                OKRCheckin.objects.filter(key_result__in=held).values_list("key_result_id", flat=True)
            ) | set(OKREvidence.objects.filter(key_result__in=held).values_list("key_result_id", flat=True))
            moved = [kr for kr in held if kr.pk not in with_history]
            kept = [kr.pk for kr in held if kr.pk in with_history]
            if moved:
                OKRKeyResult.objects.filter(pk__in=[kr.pk for kr in moved]).update(
                    owner=current.owner, updated_at=timezone.now()
                )
            metadata["owner_changed"] = {
                "from": previous_owner_id,
                "to": current.owner_id,
                "key_results": [kr.pk for kr in moved],
                "kept_with_history": kept,
            }
        activity.log(viewer.employee, ActivityAction.UPDATED, objective=current, metadata=metadata)
        if previous_owner_id != current.owner_id:
            # One notice for the objective covers the key results that moved with it.
            _notify_assignment(viewer, ReferenceType.OBJECTIVE, current, values["owner"])
    return current


def delete_objective(viewer: OKRViewer, objective_id) -> None:
    current = get_objective(viewer, objective_id)
    if not viewer.can_edit_okr(scope_of(current)):
        raise errors.forbidden()
    if not viewer.can_modify_definition(scope_of(current)):
        raise errors.conflict("objective_locked", "Objective đang chờ duyệt hoặc đã duyệt nên không thể xóa.")
    if OKRCheckin.objects.filter(key_result__objective=current).exists():
        raise errors.conflict(
            "has_checkins",
            "Không thể xóa Objective đã có lịch sử check-in. Hãy ghi lý do ở Bình luận và giữ mục tiêu đến hết chu kỳ.",
        )
    with transaction.atomic():
        current.soft_delete()
        notifications.mark_reference_read(current.company_id, ReferenceType.OBJECTIVE, current.pk)
        activity.log(viewer.employee, ActivityAction.DELETED, objective=current)


def _key_result_data(viewer: OKRViewer, data: dict) -> dict:
    company_id = viewer.company_id
    problems = {}
    objective_id = _int_or_none(data.get("objective_id"))
    objective = objectives_for(company_id).filter(pk=objective_id).first() if objective_id else None
    title = str(data.get("title") or "").strip()
    start = parse_metric(data.get("start_value", "0"), "start_value", problems)
    target = parse_metric(data.get("target_value", "100"), "target_value", problems)
    current = parse_metric(data.get("current_value", data.get("start_value", "0")), "current_value", problems)
    try:
        weight = Decimal(str(data.get("weight", "0")))
        if not weight.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        weight = None
        problems["weight"] = "Vui lòng nhập một số hợp lệ."
    raw_due = str(data.get("due_date") or "").strip()
    due_date = None
    if raw_due:
        try:
            due_date = datetime.date.fromisoformat(raw_due)
        except ValueError:
            problems["due_date"] = "Ngày không hợp lệ."
    elif objective is not None:
        due_date = objective.cycle.end_date

    if objective is None:
        problems["objective_id"] = "Objective không hợp lệ."
    owner = active_employee(company_id, _owner_input(viewer, data))
    if owner is None:
        problems["owner_id"] = "Owner không hợp lệ."
    elif not pilot.allows(owner.pk):
        problems["owner_id"] = pilot.OWNER_NOT_IN_PILOT
    if not title:
        problems["title"] = "Tên Key Result là bắt buộc."
    if start is not None and target is not None and float(target) == float(start):
        problems["target_value"] = "Mục tiêu phải khác giá trị bắt đầu."
    if weight is not None and (weight <= 0 or weight > 100):
        problems["weight"] = "Trọng số phải lớn hơn 0 và không quá 100."
    if objective is not None and due_date and not (
        objective.cycle.start_date <= due_date <= objective.cycle.end_date
    ):
        problems["due_date"] = "Hạn Key Result phải nằm trong chu kỳ."
    unit = str(data.get("unit") or "").strip()
    if not unit:
        problems["unit"] = "Đơn vị là bắt buộc (ví dụ: %, game, phiên, USD)."
    if problems:
        raise errors.validation(problems)
    priority = data.get("priority") or Priority.MEDIUM
    return {
        "objective": objective,
        "owner": owner,
        "title": title[:255],
        "description": str(data.get("description") or "").strip(),
        "unit": unit[:40],
        "start_value": start,
        "current_value": current,
        "target_value": target,
        "weight": weight.quantize(Decimal("0.01")),
        "priority": priority if priority in Priority.values else Priority.MEDIUM,
        "due_date": due_date,
    }


def _with_owner(ref: ScopeRef, owner_id) -> ScopeRef:
    return ScopeRef(**{**ref.__dict__, "owner_id": owner_id})


def create_key_result(viewer: OKRViewer, data: dict) -> OKRKeyResult:
    values = _key_result_data(viewer, data)
    objective = values["objective"]
    ref = scope_of(objective)
    if not viewer.can_edit_okr(ref) or not viewer.can_edit_okr(_with_owner(ref, values["owner"].pk)):
        raise errors.forbidden()
    if objective.approval_status in LOCKED_STATUSES and not viewer.is_admin:
        raise errors.conflict(
            "objective_locked", "Không thể thêm Key Result khi Objective đang chờ duyệt hoặc đã duyệt."
        )
    with transaction.atomic():
        values["progress"] = calculator.progress(
            values["start_value"], values["target_value"], values["current_value"]
        )
        key_result = OKRKeyResult.objects.create(company_id=viewer.company_id, **values)
        recalculate_objective(objective)
        activity.log(viewer.employee, ActivityAction.CREATED, key_result=key_result)
        _notify_assignment(viewer, ReferenceType.KEY_RESULT, key_result, values["owner"])
    return key_result


def update_key_result(viewer: OKRViewer, key_result_id, data: dict) -> OKRKeyResult:
    current = get_key_result(viewer, key_result_id)
    ref = scope_of(current)
    if not viewer.can_edit_okr(ref):
        raise errors.forbidden()
    if not viewer.can_modify_definition(ref):
        raise errors.conflict(
            "key_result_locked",
            "Key Result đang chờ duyệt hoặc đã duyệt nên không thể sửa. Hãy dùng Check-in để cập nhật tiến độ.",
        )
    data = dict(data)
    if not viewer.is_admin:
        data["objective_id"] = current.objective_id
        data["owner_id"] = current.owner_id
    values = _key_result_data(viewer, data)
    destination = values["objective"]
    proposed = ScopeRef(
        **{
            **scope_of(destination).__dict__,
            "owner_id": values["owner"].pk,
            "approval_status": current.approval_status,
            "objective_approval_status": destination.approval_status,
        }
    )
    if not viewer.can_modify_definition(proposed):
        raise errors.forbidden()
    previous_objective = current.objective
    previous_owner_id = current.owner_id
    with transaction.atomic():
        for field, value in values.items():
            setattr(current, field, value)
        current.progress = calculator.progress(current.start_value, current.target_value, current.current_value)
        current.save()
        recalculate_objective(previous_objective)
        if previous_objective.pk != destination.pk:
            recalculate_objective(destination)
        activity.log(
            viewer.employee, ActivityAction.UPDATED, key_result=current, metadata={"fields": sorted(values)}
        )
        if previous_owner_id != current.owner_id:
            _notify_assignment(viewer, ReferenceType.KEY_RESULT, current, values["owner"])
    return current


def delete_key_result(viewer: OKRViewer, key_result_id) -> None:
    current = get_key_result(viewer, key_result_id)
    ref = scope_of(current)
    if not viewer.can_edit_okr(ref):
        raise errors.forbidden()
    if not viewer.can_modify_definition(ref):
        raise errors.conflict("key_result_locked", "Key Result đang chờ duyệt hoặc đã duyệt nên không thể xóa.")
    if current.checkins.exists():
        raise errors.conflict(
            "has_checkins", "Không thể xóa Key Result đã có lịch sử check-in. Hãy giữ lịch sử đến hết chu kỳ."
        )
    with transaction.atomic():
        current.soft_delete()
        notifications.mark_reference_read(current.company_id, ReferenceType.KEY_RESULT, current.pk)
        recalculate_objective(current.objective)
        activity.log(viewer.employee, ActivityAction.DELETED, key_result=current)


def normalize_weights(count: int) -> list:
    """Split 100 % into ``count`` two-decimal weights; leftover cents go to the first ones."""
    base, remainder = divmod(10000, count)
    return [Decimal(base + (1 if index < remainder else 0)) / 100 for index in range(count)]


def normalize_key_result_weights(viewer: OKRViewer, objective_id) -> OKRObjective:
    objective = get_objective(viewer, objective_id)
    if not viewer.can_modify_definition(scope_of(objective)) or objective.approval_status not in (
        ApprovalStatus.DRAFT,
        ApprovalStatus.REJECTED,
    ):
        raise errors.forbidden()
    key_results = alive_key_results(objective)
    if not _count_ok(len(key_results)):
        raise errors.validation({"weight": "Cần từ 2 đến 4 Key Result để chia đều trọng số."})
    with transaction.atomic():
        for key_result, weight in zip(key_results, normalize_weights(len(key_results))):
            key_result.weight = weight.quantize(Decimal("0.01"))
            key_result.save(update_fields=["weight", "updated_at"])
        recalculate_objective(objective)
    return objective


def sync_pending_approval_notifications(viewer: OKRViewer) -> int:
    """Make sure the viewer has an approval request for every submitted
    objective they may review (JOYOKR runs this whenever notifications are
    listed). Dedupe keys keep it idempotent; returns how many exist."""
    if not (viewer.is_active and viewer.is_manager):
        return 0
    ensured = 0
    pending = objectives_for(viewer.company_id).filter(
        viewer.objective_visibility(), approval_status=ApprovalStatus.SUBMITTED
    )
    for objective in pending:
        if objective.owner_id == viewer.employee_id and not viewer.is_admin:
            continue
        if not viewer.can_manage_scope(scope_of(objective)):
            continue
        notification = notifications.send(
            viewer.employee,
            company_id=viewer.company_id,
            notification_type=notifications.APPROVAL_REQUESTED,
            title=f"Chờ duyệt: {objective.title}",
            message="OKR này đang chờ duyệt. Mở Duyệt OKR để xử lý.",
            reference_type=ReferenceType.OBJECTIVE,
            reference_id=objective.pk,
            dedupe_key=approval_dedupe_key(objective, viewer.employee_id),
            actor=objective.owner,
        )
        ensured += notification is not None
    return ensured
