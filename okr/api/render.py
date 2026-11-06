"""JSON shapes from ``appcheckin/docs/okr_api_contract.md``.

The app's parsers (``lib/features/okr/data/dto/okr_dto.dart``) read exactly
these keys; change both together.
"""

from __future__ import annotations

from django.conf import settings as django_settings
from django.db.models import Count

from okr.api.base import metric, percent, person
from okr.models import OKRComment, OKRObjective
from okr.models.choices import ApprovalStatus, CycleStatus
from okr.services import activity as activity_service
from okr.services import calculator, okrs, reports
from okr.services.access import scope_of


class Context:
    """Per-request caches: expected progress per cycle and company policy."""

    def __init__(self, viewer):
        self.viewer = viewer
        self.policy = reports.Policy.for_company(viewer.company_id)
        self._expected = {}

    def expected(self, cycle):
        if cycle.pk not in self._expected:
            self._expected[cycle.pk] = reports.expected_for(cycle)
        return self._expected[cycle.pk]


def cycle(item):
    return {
        "id": item.pk,
        "name": item.name,
        "cycle_type": item.cycle_type,
        "start_date": item.start_date,
        "end_date": item.end_date,
        "status": item.status,
        "parent_id": item.parent_id,
    }


def visible_child_counts(ctx: Context, objective_ids) -> dict:
    rows = (
        OKRObjective.objects.for_company(ctx.viewer.company_id)
        .filter(ctx.viewer.objective_visibility(), parent_id__in=list(objective_ids))
        .values("parent_id")
        .annotate(n=Count("id"))
    )
    return {row["parent_id"]: row["n"] for row in rows}


def objective_summary(ctx: Context, item: OKRObjective, *, child_count: int = 0, key_results=None) -> dict:
    key_results = okrs.alive_key_results(item) if key_results is None else key_results
    expected = ctx.expected(item.cycle)
    projection = reports.objective_projection(item, expected, ctx.policy, key_results)
    return {
        "id": item.pk,
        "title": item.title,
        "scope": item.scope,
        "approval_status": item.approval_status,
        "progress": percent(projection["progress"]),
        "expected_progress": percent(expected),
        "health": projection["health"],
        "provisional": projection["provisional"],
        "key_result_count": len(key_results),
        "weight": percent(item.weight),
        "owner": person(item.owner),
        "cycle": {"id": item.cycle_id, "name": item.cycle.name, "end_date": item.cycle.end_date},
        "department_name": item.department.department if item.department_id else None,
        "team_name": item.team.name if item.team_id else None,
        "parent_id": item.parent_id,
        "child_count": child_count,
    }


def objective_summaries(ctx: Context, items) -> list:
    items = list(items)
    counts = visible_child_counts(ctx, [item.pk for item in items])
    return [
        objective_summary(
            ctx,
            item,
            child_count=counts.get(item.pk, 0),
            key_results=sorted(item.key_results.all(), key=lambda kr: kr.pk),
        )
        for item in items
    ]


def can_checkin(viewer, item) -> bool:
    objective = item.objective
    return (
        viewer.is_active
        and item.owner_id == viewer.employee_id
        and item.approval_status == ApprovalStatus.APPROVED
        and objective.approval_status == ApprovalStatus.APPROVED
        and objective.cycle.status == CycleStatus.ACTIVE
    )


def key_result(ctx: Context, item, *, last_checkin_at=None) -> dict:
    expected = ctx.expected(item.objective.cycle)
    return {
        "id": item.pk,
        "objective_id": item.objective_id,
        "objective_title": item.objective.title,
        "title": item.title,
        "unit": item.unit,
        "start_value": metric(item.start_value),
        "current_value": metric(item.current_value),
        "target_value": metric(item.target_value),
        "weight": percent(item.weight),
        "progress": percent(item.progress),
        "expected_progress": percent(expected),
        "health": reports.key_result_health(item, expected, ctx.policy),
        "due_date": item.due_date,
        "approval_status": item.approval_status,
        "owner": person(item.owner),
        "last_checkin_at": last_checkin_at,
        "description": item.description,
        "priority": item.priority,
        "permissions": {
            "can_checkin": can_checkin(ctx.viewer, item),
            # Same rule as the evidence endpoint (JOYOKR can_edit_okr).
            "can_add_evidence": ctx.viewer.can_edit_okr(scope_of(item)),
            # Edit/delete the definition (JOYOKR can_modify_definition).
            "can_edit": ctx.viewer.can_modify_definition(scope_of(item)),
        },
    }


def key_results(ctx: Context, items) -> list:
    items = list(items)
    latest = reports.latest_checkins(item.pk for item in items)
    return [key_result(ctx, item, last_checkin_at=latest.get(item.pk)) for item in items]


def objective_detail(ctx: Context, item: OKRObjective) -> dict:
    viewer = ctx.viewer
    all_key_results = okrs.alive_key_results(item)
    visible = [
        kr
        for kr in okrs.key_results_for(viewer.company_id)
        .filter(viewer.key_result_visibility(), objective=item)
        .order_by("id")
    ]
    ref = scope_of(item)
    parent = item.parent
    show_parent = parent is not None and parent.deleted_at is None and viewer.can_view_scope(scope_of(parent))
    count = len(all_key_results)
    data = objective_summary(
        ctx, item, child_count=visible_child_counts(ctx, [item.pk]).get(item.pk, 0), key_results=all_key_results
    )
    data.update(
        {
            "description": item.description,
            "priority": item.priority,
            "department_id": item.department_id,
            "team_id": item.team_id,
            "parent": {"id": parent.pk, "title": parent.title} if show_parent else None,
            "key_results": key_results(ctx, visible),
            "permissions": {
                # JOYOKR's button test: alignment is left to the submit
                # call, which explains itself when it is missing.
                "can_submit": (
                    viewer.can_edit_okr(ref)
                    and item.approval_status in (ApprovalStatus.DRAFT, ApprovalStatus.REJECTED)
                    and 2 <= count <= 4
                    and calculator.weight_total_ok(okrs.weight_total(item))
                ),
                "can_comment": viewer.can_view_scope(ref),
                # Edit/delete the definition (JOYOKR can_modify_definition).
                "can_edit": viewer.can_modify_definition(ref),
                "can_approve": viewer.can_approve_now(ref),
            },
            "comment_count": OKRComment.objects.filter(objective=item).count(),
            "rejection_reason": okrs.rejection_reason(item),
        }
    )
    return data


def checkin(item) -> dict:
    return {
        "id": item.pk,
        "created_at": item.created_at,
        "author": person(item.author),
        "previous_value": metric(item.previous_value),
        "current_value": metric(item.current_value),
        "previous_progress": percent(item.previous_progress),
        "current_progress": percent(item.current_progress),
        "confidence": item.confidence,
        "update_text": item.update_text,
        "blockers": item.blockers,
        "next_steps": item.next_steps,
    }


def evidence(item) -> dict:
    data = {
        "id": item.pk,
        "evidence_type": item.evidence_type,
        "label": item.label,
        "created_at": item.created_at,
        "author": person(item.author),
        # F7: the check-in this evidence was attached to, if any.
        "checkin_id": item.checkin_id,
    }
    if item.evidence_type == "url":
        data["url"] = item.url
    else:
        data.update(
            {
                "file_name": item.original_name,
                "file_size": item.size,
                "download_path": f"/okr/evidence/{item.pk}/download/",
            }
        )
    return data


def comment(item) -> dict:
    return {
        "id": item.pk,
        "content": item.content,
        "created_at": item.created_at,
        "author": person(item.author),
        "parent_id": item.parent_id,
    }


def activity(item) -> dict:
    return {
        "id": item.pk,
        "action": item.action,
        "created_at": item.created_at,
        "actor": person(item.actor),
        "summary": activity_service.summary(item),
        "key_result": {"id": item.key_result_id, "title": item.key_result.title} if item.key_result_id else None,
    }


def attention(item) -> dict:
    data = {
        "key_result_id": item["key_result_id"],
        "key_result_title": item["key_result_title"],
        "objective_title": item["objective_title"],
        "reason": item["reason"],
        "progress": percent(item["progress"]),
        "expected_progress": percent(item["expected_progress"]),
        "days_overdue": item["days_overdue"],
        "last_checkin_at": item["last_checkin_at"],
    }
    if "owner" in item:
        data["owner"] = person(item["owner"])
    return data


def dashboard(ctx: Context, data: dict) -> dict:
    return {
        "cycle": cycle(data["cycle"]),
        "progress": percent(data["progress"]),
        "expected_progress": percent(data["expected_progress"]),
        "health": data["health"],
        "objective_count": data["objective_count"],
        "key_results_completed": data["key_results_completed"],
        "key_results_total": data["key_results_total"],
        "attention_count": data["attention_count"],
        "pending_approval_count": data["pending_approval_count"],
        "attention": [attention(item) for item in data["attention"]],
        "featured": objective_summaries(ctx, data["featured"]),
    }


def approval_item(ctx: Context, item: dict) -> dict:
    objective = item["objective"]
    checks = item["checks"]
    return {
        "objective": objective_summary(ctx, objective, key_results=item["key_results"]),
        "submitted_at": item["submitted_at"] or objective.updated_at,
        "key_results": [{"title": kr.title, "weight": percent(kr.weight)} for kr in item["key_results"]],
        "checks": {
            "key_result_count_ok": checks["key_result_count_ok"],
            "weight_total": percent(checks["weight_total"]),
            "weight_ok": checks["weight_ok"],
            "alignment_ok": checks["alignment_ok"],
        },
        "can_approve": item["can_approve"],
        "block_reason": item["block_reason"],
    }


def team_report(data: dict) -> dict:
    return {
        "member_count": data["member_count"],
        "on_track": data["on_track"],
        "at_risk": data["at_risk"],
        "off_track": data["off_track"],
        "pending_approval_count": data["pending_approval_count"],
        "expected_progress": percent(data["expected_progress"]),
        "attention": [attention(item) for item in data["attention"]],
        "members": [
            {
                "employee": person(member["employee"]),
                "progress": percent(member["progress"]),
                "health": member["health"],
                "has_approved_okr": member["has_approved_okr"],
            }
            for member in data["members"]
        ],
    }


def notification(item, available) -> dict:
    reference = None
    if item.reference_type and item.reference_id:
        reference = {"type": item.reference_type, "id": item.reference_id, "available": bool(available)}
    return {
        "id": item.pk,
        "type": item.notification_type,
        "title": item.title,
        "message": item.message,
        "is_read": item.is_read,
        "created_at": item.created_at,
        "reference": reference,
        # F7: who caused it; null for reminders and rows migrated from JOYOKR.
        "actor": person(item.actor) if item.actor_id else None,
    }


# -- web / administration shapes ----------------------------------------------------


def cycle_with_count(item):
    data = cycle(item)
    if hasattr(item, "objective_count"):
        data["objective_count"] = item.objective_count
    return data


def person_row(employee) -> dict:
    work = getattr(employee, "employee_work_info", None)
    department = getattr(work, "department_id", None) if work is not None else None
    return {
        "id": employee.pk,
        "name": employee.get_full_name(),
        "department_id": department.pk if department is not None else None,
        "department_name": department.department if department is not None else None,
        "manager_id": getattr(work, "reporting_manager_id_id", None) if work is not None else None,
    }


def department(item) -> dict:
    return {"id": item.pk, "name": item.department, "okr_manager": person(getattr(item, "okr_manager", None))}


def team(item, members=None) -> dict:
    data = {
        "id": item.pk,
        "name": item.name,
        "description": item.description,
        "department_id": item.department_id,
        "department_name": item.department.department if item.department_id else None,
        "manager": person(item.manager),
        "status": item.status,
        "member_count": getattr(item, "member_count", None),
    }
    if members is not None:
        data["members"] = [person(membership.employee) for membership in members]
    return data


def _user_person(user):
    """An HR user as ``{id, name}`` of their employee, or None (no e-mail or username)."""
    employee = getattr(user, "employee_get", None) if user is not None else None
    return person(employee) if employee is not None else None


def role(item) -> dict:
    return {
        "id": item.pk,
        "employee": person(item.employee),
        "role": item.role,
        "granted_at": item.created_at,
        "granted_by": _user_person(item.granted_by),
    }


def role_event(item) -> dict:
    return {
        "id": item.pk,
        "employee": person(item.employee),
        "role": item.role,
        "action": item.action,
        "department": {"id": item.department_id, "name": item.department.department} if item.department_id else None,
        "actor": _user_person(item.actor),
        "created_at": item.created_at,
    }


def settings(item) -> dict:
    return {
        "warning_threshold": percent(item.warning_threshold),
        "reminder_frequency": item.reminder_frequency,
        "email_reminders": item.email_reminders,
        "rating_ranges": item.rating_ranges,
        # What the server really does, apart from what is configured above:
        # reminders exist only as the ``okr_checkin_reminders`` command and
        # are in-app only. Nothing sends e-mail or Telegram.
        "reminder_schedule": {
            "scheduled": bool(getattr(django_settings, "OKR_REMINDERS_SCHEDULED", False)),
            "command": "okr_checkin_reminders",
            "channels": ["in_app"],
            "email_available": False,
        },
    }


def official_progress(data: dict) -> dict:
    return {
        "value": percent(data["value"]),
        "objective_count": data["objective_count"],
        "total_weight": percent(data["total_weight"]),
    }


def _objective_list(ctx: Context, objectives) -> list:
    counts = visible_child_counts(ctx, [o.pk for o in objectives])
    return [
        objective_summary(ctx, o, child_count=counts.get(o.pk, 0), key_results=o.live_key_results)
        for o in objectives
    ]


def _filters(data: dict) -> dict:
    """The filters the server applied, echoed so screens and exports can say so."""
    return {key: value for key, value in (data.get("filters") or {}).items() if value}


def company_dashboard(ctx: Context, data: dict) -> dict:
    return {
        "cycle": cycle(data["cycle"]),
        "cycle_selection": data.get("cycle_selection"),
        "filters": _filters(data),
        "progress": official_progress(data["progress"]),
        "selection_progress": percent(data["selection_progress"]),
        "expected_progress": percent(data["expected_progress"]),
        "objective_stats": data["objective_stats"],
        "in_progress_count": data["in_progress_count"],
        "pending_approval_count": data["pending_approval_count"],
        "key_result_count": data["key_result_count"],
        "key_results_completed": data["key_results_completed"],
        "stale_count": data["stale_count"],
        "attention": [attention(item) for item in data["attention"]],
        "attention_total": data["attention_total"],
        "teams": [
            {
                "id": row["team"].pk,
                "name": row["team"].name,
                "objective_count": row["objective_count"],
                "progress": percent(row["progress"]),
                "health": row["health"],
            }
            for row in data["teams"]
        ],
        "featured": _objective_list(ctx, data["featured"]),
    }


def report(ctx: Context, data: dict) -> dict:
    def groups(rows):
        return [{"label": r["label"], "count": r["count"], "progress": percent(r["progress"])} for r in rows]

    return {
        "cycle": cycle(data["cycle"]),
        "cycle_selection": data.get("cycle_selection"),
        "filters": _filters(data),
        "progress": official_progress(data["progress"]),
        "selection_progress": percent(data["selection_progress"]),
        "expected_progress": percent(data["expected_progress"]),
        "objective_stats": data["objective_stats"],
        "key_result_count": data["key_result_count"],
        "stale_count": data["stale_count"],
        "rows": _objective_list(ctx, data["rows"]),
        "by_scope": groups(data["by_scope"]),
        "by_owner": groups(data["by_owner"]),
        "by_department": groups(data["by_department"]),
        "by_team": groups(data["by_team"]),
        "by_rating": [
            {"label": band["label"], "min": str(band["min"]), "max": str(band["max"]), "count": band["count"]}
            for band in data["by_rating"]
        ],
        "overdue_key_results": [
            {
                "id": kr.pk,
                "title": kr.title,
                "objective_id": kr.objective_id,
                "objective_title": kr.objective.title,
                "owner": person(kr.owner),
                "due_date": kr.due_date,
                "days_late": kr.days_late,
                "progress": percent(kr.progress),
            }
            for kr in data["overdue_key_results"]
        ],
        "checkin_count": data["checkin_count"],
        "checkin_history": [
            {
                **checkin(entry),
                "key_result": {"id": entry.key_result.pk, "title": entry.key_result.title,
                               "unit": entry.key_result.unit},
                "objective_title": entry.key_result.objective.title,
                "owner": person(entry.key_result.owner),
            }
            for entry in data["checkin_history"]
        ],
        "trend": [{"date": point["date"], "progress": percent(point["progress"])} for point in data["trend"]],
        "checkin_attention_by_owner": [
            {
                "owner": person(group["owner"]),
                "overdue_count": group["overdue_count"],
                "off_track_count": group["off_track_count"],
                "items": [attention(item) for item in group["items"]],
            }
            for group in data["checkin_attention_by_owner"]
        ],
    }


def _tree_node(ctx: Context, item, expected) -> dict:
    data = objective_summary(ctx, item, child_count=len(item.children_nodes), key_results=item.live_key_results)
    data["key_results"] = [
        {
            "id": kr.pk,
            "title": kr.title,
            "weight": percent(kr.weight),
            "progress": percent(kr.progress),
            "health": reports.key_result_health(kr, expected, ctx.policy),
            "approval_status": kr.approval_status,
            "owner": person(kr.owner),
            # JOYOKR's tree shows each KR's deadline ("Hạn").
            "due_date": kr.due_date,
        }
        for kr in item.live_key_results
    ]
    data["children"] = [_tree_node(ctx, child, expected) for child in item.children_nodes]
    return data


def tree(ctx: Context, data: dict) -> dict:
    expected = data["expected_progress"]
    return {
        "cycle": cycle(data["cycle"]),
        "cycle_selection": data.get("cycle_selection"),
        "expected_progress": percent(expected),
        "items": [_tree_node(ctx, item, expected) for item in data["items"]],
        "unlinked_items": [_tree_node(ctx, item, expected) for item in data["unlinked_items"]],
    }


def checkin_queue_rows(ctx: Context, items) -> list:
    from django.db.models import F, Window
    from django.db.models.functions import RowNumber

    from okr.models import OKRCheckin

    # Only each key result's newest check-in leaves the database.
    latest = {
        entry.key_result_id: entry
        for entry in OKRCheckin.objects.filter(key_result__in=items)
        .annotate(rank=Window(RowNumber(), partition_by=[F("key_result_id")],
                              order_by=[F("created_at").desc(), F("id").desc()]))
        .filter(rank=1)
        .select_related("author")
    }
    rows = key_results(ctx, items)
    for row in rows:
        entry = latest.get(row["id"])
        row["latest_checkin"] = checkin(entry) if entry else None
    return rows


def _management_person(node: dict) -> dict:
    return {
        "employee": person(node["employee"]),
        "objectives": [
            {
                "id": objective.pk,
                "title": objective.title,
                "scope": objective.scope,
                "progress": percent(objective.progress),
                "key_results": [
                    {
                        "id": kr.pk,
                        "title": kr.title,
                        "unit": kr.unit,
                        "current_value": metric(kr.current_value),
                        "target_value": metric(kr.target_value),
                        "progress": percent(kr.progress),
                        "health": kr.health,
                        "due_date": kr.due_date,
                        "checkin_status": kr.checkin_status,
                        "checkin_total": kr.checkin_total,
                        "latest_checkin_at": kr.history[0].created_at if kr.history else None,
                        "checkins": [checkin(entry) for entry in kr.history],
                    }
                    for kr in key_result_list
                ],
            }
            for objective, key_result_list in node["objectives"]
        ],
    }


def checkin_management(data: dict) -> dict:
    return {
        "cycle": cycle(data["cycle"]),
        "cycle_selection": data.get("cycle_selection"),
        "filters": _filters(data),
        "can_manage": data["can_manage"],
        "scope": data["scope"],
        "summary": data["summary"],
        "expected_progress": percent(data["expected_progress"]),
        "departments": [
            {
                "id": node["department"].pk,
                "name": node["department"].department,
                "teams": [
                    {
                        "id": team_node["team"].pk if team_node["team"] else None,
                        "name": team_node["team"].name if team_node["team"] else "Chưa thuộc team",
                        "people": [_management_person(p) for p in team_node["people"]],
                    }
                    for team_node in node["teams"]
                ],
            }
            for node in data["departments"]
        ],
        "unassigned": [_management_person(p) for p in data["unassigned"]],
    }
