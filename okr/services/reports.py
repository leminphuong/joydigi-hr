"""Dashboard, team overview and approval queue (JOYOKR ``JoyOKR_Report_Service``
and ``approval_queue``), shaped for the app contract.

JOYOKR has three slightly different "overdue check-in" tests (dashboard,
manager report, reminder cron). This module uses the manager-report one for
every screen, because it is the only one that also yields "days overdue":
the last check-in, or the key result's creation when there is none, is
older than the reminder interval (7, 14 or 30 days).
"""

from __future__ import annotations

import datetime
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.db.models import Max, Q
from django.utils import timezone

from okr.models import OKRKeyResult, OKRObjective, OKRSettings
from okr.models.choices import ApprovalStatus, ReminderFrequency
from okr.services import calculator, okrs
from okr.services.access import EDITABLE_CYCLE_STATUSES, OKRViewer, scope_of

REMINDER_DAYS = {ReminderFrequency.WEEKLY: 7, ReminderFrequency.BIWEEKLY: 14, ReminderFrequency.MONTHLY: 30}
SEVERITY = {calculator.OFF_TRACK: 3, calculator.AT_RISK: 2, calculator.ON_TRACK: 1, calculator.COMPLETED: 0}
FEATURED_LIMIT = 8


@dataclass(frozen=True)
class Policy:
    """Per-company settings the calculations need (JOYOKR options)."""

    warning_threshold: Decimal
    reminder_days: int
    rating_ranges: list

    @classmethod
    def for_company(cls, company_id: int) -> "Policy":
        settings = OKRSettings.objects.filter(company_id=company_id).first()
        if settings is None:
            from okr.models import default_rating_ranges

            return cls(Decimal("15"), 7, default_rating_ranges())
        return cls(
            settings.warning_threshold,
            REMINDER_DAYS.get(settings.reminder_frequency, 7),
            settings.rating_ranges,
        )


def expected_for(cycle, as_of: Optional[datetime.date] = None) -> Decimal:
    return calculator.expected_progress(cycle.start_date, cycle.end_date, as_of)


def objective_projection(objective: OKRObjective, expected, policy: Policy, key_results=None) -> dict:
    """Progress/health of one objective as every screen shows it."""
    if objective.approval_status != ApprovalStatus.APPROVED:
        key_results = okrs.alive_key_results(objective) if key_results is None else key_results
        pairs = [(kr.progress, kr.weight) for kr in key_results]
    else:
        pairs = None
    return calculator.objective_projection(
        objective.approval_status, objective.progress, pairs, expected, policy.warning_threshold
    )


def key_result_health(key_result: OKRKeyResult, expected, policy: Policy) -> Optional[str]:
    if key_result.approval_status != ApprovalStatus.APPROVED:
        return None
    return calculator.status(key_result.progress, expected, policy.warning_threshold)


def latest_checkins(key_result_ids) -> dict:
    from okr.models import OKRCheckin

    rows = (
        OKRCheckin.objects.filter(key_result_id__in=list(key_result_ids))
        .values("key_result_id")
        .annotate(latest=Max("created_at"))
    )
    return {row["key_result_id"]: row["latest"] for row in rows}


def _attention_item(key_result, latest, expected, policy: Policy, now, with_owner: bool) -> Optional[dict]:
    last_activity = latest or key_result.created_at
    age_days = max(0, math.floor((now - last_activity).total_seconds() / 86400))
    overdue = age_days > policy.reminder_days
    health = calculator.status(key_result.progress, expected, policy.warning_threshold)
    if not overdue and health != calculator.OFF_TRACK:
        return None
    item = {
        "key_result_id": key_result.pk,
        "key_result_title": key_result.title,
        "objective_title": key_result.objective.title,
        "reason": "stale" if overdue else "off_track",
        "progress": key_result.progress,
        "expected_progress": expected,
        "days_overdue": max(1, age_days - policy.reminder_days) if overdue else 0,
        "last_checkin_at": latest,
        "_health": health,
    }
    if with_owner:
        item["owner"] = key_result.owner
    return item


def attention_items(key_results, expected, policy: Policy, *, with_owner: bool = False, now=None) -> list:
    """Approved key results that are overdue for a check-in or off track,
    most urgent first (JOYOKR dashboard ordering)."""
    now = now or timezone.now()
    key_results = list(key_results)
    latest = latest_checkins(kr.pk for kr in key_results)
    items = [
        item
        for item in (
            _attention_item(kr, latest.get(kr.pk), expected, policy, now, with_owner) for kr in key_results
        )
        if item is not None
    ]
    items.sort(
        key=lambda item: (
            item["reason"] != "stale",
            -SEVERITY.get(item["_health"], 0),
            item["progress"],
        )
    )
    for item in items:
        item.pop("_health")
    return items


def _approved_key_results(company_id: int, cycle, owner_filter: Q):
    return (
        OKRKeyResult.objects.for_company(company_id)
        .filter(
            owner_filter,
            objective__cycle=cycle,
            objective__deleted_at__isnull=True,
            approval_status=ApprovalStatus.APPROVED,
            objective__approval_status=ApprovalStatus.APPROVED,
        )
        .select_related("objective", "owner")
        .order_by("id")
    )


def dashboard(viewer: OKRViewer, cycle) -> dict:
    """The viewer's own overview of one cycle (``GET /okr/dashboard/``).

    ``progress`` is the official one: Σ(progress × weight) / Σ weight over the
    viewer's approved objectives, so it stays 0 with no health until one
    is approved. Drafts still show their provisional progress in
    ``featured``.
    """
    policy = Policy.for_company(viewer.company_id)
    expected = expected_for(cycle)
    objectives = list(
        okrs.objectives_for(viewer.company_id)
        .filter(cycle=cycle, owner_id=viewer.employee_id)
        .prefetch_related("key_results")
        .order_by("-weight", "id")
    )
    approved = [o for o in objectives if o.approval_status == ApprovalStatus.APPROVED]
    progress = calculator.weighted_progress((o.progress, o.weight) for o in approved)
    key_results = list(_approved_key_results(viewer.company_id, cycle, Q(owner_id=viewer.employee_id)))
    attention = attention_items(key_results, expected, policy)
    return {
        "cycle": cycle,
        "progress": progress,
        "expected_progress": expected,
        "health": calculator.status(progress, expected, policy.warning_threshold) if approved else None,
        "objective_count": len(objectives),
        "key_results_completed": sum(1 for kr in key_results if kr.progress >= 100),
        "key_results_total": len(key_results),
        "attention_count": len(attention),
        "pending_approval_count": sum(1 for o in objectives if o.approval_status == ApprovalStatus.SUBMITTED),
        "attention": attention,
        "featured": objectives[:FEATURED_LIMIT],
        "policy": policy,
    }


def approval_queue(viewer: OKRViewer) -> list:
    """Submitted objectives the viewer may review, newest change first.

    Each item carries the approve-time checks so the app can say why an
    objective cannot be approved yet (``block_reason``)."""
    if not (viewer.is_active and viewer.is_manager):
        return []
    candidates = (
        okrs.objectives_for(viewer.company_id)
        .filter(viewer.objective_visibility(), approval_status=ApprovalStatus.SUBMITTED)
        .filter(cycle__status__in=EDITABLE_CYCLE_STATUSES)
        .prefetch_related("key_results")
        .order_by("-updated_at", "-id")
    )
    items = []
    for objective in candidates:
        ref = scope_of(objective)
        if objective.owner_id == viewer.employee_id and not viewer.is_admin:
            continue
        if not viewer.can_manage_scope(ref):
            continue
        key_results = sorted(objective.key_results.all(), key=lambda kr: kr.pk)
        checks = okrs.approval_checks(objective, key_results)
        items.append(
            {
                "objective": objective,
                "submitted_at": okrs.submitted_at(objective),
                "key_results": key_results,
                "checks": checks,
                "can_approve": viewer.can_approve_now(ref) and not block_reason(checks),
                "block_reason": block_reason(checks),
            }
        )
    return items


def block_reason(checks: dict) -> Optional[str]:
    """First approve-time rule an objective fails, in the order approve tests them."""
    if not checks["alignment_ok"]:
        return "Objective cần liên kết với một Objective cấp trên."
    if not checks["key_result_count_ok"]:
        return "Objective cần từ 2 đến 4 Key Result."
    if not checks["weight_ok"]:
        return "Tổng trọng số Key Result phải bằng 100%."
    if not checks["key_results_ok"]:
        return "KR cần đơn vị rõ ràng, mục tiêu khác bắt đầu và hạn nằm trong chu kỳ."
    return None


def team_report(viewer: OKRViewer, cycle) -> dict:
    """People the viewer manages and how their approved OKRs are doing.

    Each member's progress is the weighted average of their approved
    objectives in the cycle. ``completed`` members count as on track.
    """
    from employee.models import Employee

    policy = Policy.for_company(viewer.company_id)
    expected = expected_for(cycle)
    managed = viewer.managed_employee_ids()
    people = Employee._base_manager.filter(
        is_active=True, employee_work_info__company_id=viewer.company_id
    ).exclude(pk=viewer.employee_id)
    if managed is not None:
        people = people.filter(pk__in=managed)
    people = list(people.order_by("employee_first_name", "employee_last_name", "id"))
    person_ids = [person.pk for person in people]

    approved = list(
        OKRObjective.objects.for_company(viewer.company_id).filter(
            cycle=cycle, owner_id__in=person_ids, approval_status=ApprovalStatus.APPROVED
        )
    )
    by_owner = {}
    for objective in approved:
        by_owner.setdefault(objective.owner_id, []).append(objective)

    members = []
    buckets = {calculator.ON_TRACK: 0, calculator.AT_RISK: 0, calculator.OFF_TRACK: 0}
    for person in people:
        owned = by_owner.get(person.pk, [])
        if owned:
            progress = calculator.weighted_progress((o.progress, o.weight) for o in owned)
            health = calculator.status(progress, expected, policy.warning_threshold)
            buckets[calculator.ON_TRACK if health == calculator.COMPLETED else health] += 1
        else:
            progress, health = None, None
        members.append({"employee": person, "progress": progress, "health": health, "has_approved_okr": bool(owned)})

    key_results = _approved_key_results(viewer.company_id, cycle, Q(owner_id__in=person_ids))
    attention = attention_items(key_results, expected, policy, with_owner=True)
    return {
        "member_count": len(people),
        "on_track": buckets[calculator.ON_TRACK],
        "at_risk": buckets[calculator.AT_RISK],
        "off_track": buckets[calculator.OFF_TRACK],
        "pending_approval_count": len(approval_queue(viewer)),
        "expected_progress": expected,
        "attention": attention,
        "members": members,
    }


# -- company-wide views for the web (JOYOKR dashboard/report/tree) -----------------

SCOPE_RANK = {"company": 0, "department": 1, "team": 2, "personal": 3}
# JOYOKR embedded 20 check-ins per key result in the management tree; for a
# whole company that is megabytes (W4 measurement: 8 MB, 84 MB peak for 400
# people). The tree now carries the newest 5 plus ``checkin_total``; the key
# result page keeps the full history.
CHECKIN_HISTORY = 5
DASHBOARD_ATTENTION_LIMIT = 100


def visible_objectives(viewer: OKRViewer, cycle, *, approved_only: bool = False, filters: Optional[dict] = None) -> list:
    """Objectives of a cycle the viewer may see, each with ``live_key_results``.

    With ``approved_only`` only approved objectives count and each keeps
    only its approved key results (JOYOKR ``visible_objectives`` with the
    ``approved`` filter)."""
    rows = okrs.objectives_for(viewer.company_id).filter(viewer.objective_visibility(), cycle=cycle)
    if approved_only:
        rows = rows.filter(approval_status=ApprovalStatus.APPROVED)
    for field, value in (filters or {}).items():
        if value:
            rows = rows.filter(**{field: value})
    items = list(rows.prefetch_related("key_results__owner").order_by("id"))
    for objective in items:
        key_results = sorted(objective.key_results.all(), key=lambda kr: kr.pk)
        if approved_only:
            key_results = [kr for kr in key_results if kr.approval_status == ApprovalStatus.APPROVED]
        for kr in key_results:
            kr.objective = objective
        objective.live_key_results = key_results
    return items


def official_company_progress(objectives) -> dict:
    """The one company-wide progress figure (JOYOKR
    ``official_company_progress``): weighted over approved company-level
    objectives only."""
    company = [o for o in objectives if o.scope == "company" and o.approval_status == ApprovalStatus.APPROVED]
    return {
        "value": calculator.weighted_progress((o.progress, o.weight) for o in company),
        "objective_count": len(company),
        "total_weight": sum((max(Decimal("0"), o.weight) for o in company), Decimal("0")),
    }


def _health_counts(objectives, expected, policy: Policy) -> dict:
    stats = {"total": len(objectives), calculator.ON_TRACK: 0, calculator.AT_RISK: 0,
             calculator.OFF_TRACK: 0, calculator.COMPLETED: 0}
    for objective in objectives:
        objective.health = calculator.status(objective.progress, expected, policy.warning_threshold)
        stats[objective.health] += 1
    return stats


def _attention_of(objectives, expected, policy: Policy) -> list:
    key_results = [kr for objective in objectives for kr in objective.live_key_results]
    return attention_items(key_results, expected, policy, with_owner=True)


def selection_progress(objectives) -> Decimal:
    """Weighted progress of whatever a filter selected (Σ progress × weight / Σ weight).
    With no filter the screens keep showing ``official_company_progress``."""
    return calculator.weighted_progress((o.progress, o.weight) for o in objectives)


def pending_approval_count(viewer: OKRViewer, cycle, filters: Optional[dict] = None) -> int:
    rows = okrs.objectives_for(viewer.company_id).filter(
        viewer.objective_visibility(), cycle=cycle, approval_status=ApprovalStatus.SUBMITTED
    )
    for field, value in (filters or {}).items():
        if value:
            rows = rows.filter(**{field: value})
    return rows.count()


def company_dashboard(viewer: OKRViewer, cycle, filters: Optional[dict] = None) -> dict:
    """Company dashboard of one cycle (JOYOKR ``dashboard``): every approved
    objective the viewer may see. W4: the same scope/department/team/owner
    filters as the report narrow every figure except the official company
    progress, which is shown next to the selection's own progress."""
    from okr.models import OKRTeam

    policy = Policy.for_company(viewer.company_id)
    expected = expected_for(cycle)
    filters = {key: value for key, value in (filters or {}).items() if value}
    everything = visible_objectives(viewer, cycle, approved_only=True)
    objectives = visible_objectives(viewer, cycle, approved_only=True, filters=filters) if filters else everything
    stats = _health_counts(objectives, expected, policy)
    attention = _attention_of(objectives, expected, policy)
    teams = []
    for team in OKRTeam.objects.for_company(viewer.company_id).filter(status="active").order_by("name", "id"):
        own = [o for o in objectives if o.team_id == team.pk]
        if not own:
            continue
        progress = calculator.weighted_progress((o.progress, o.weight) for o in own)
        teams.append({"team": team, "objective_count": len(own), "progress": progress,
                      "health": calculator.status(progress, expected, policy.warning_threshold)})
    featured = sorted(objectives, key=lambda o: (-o.weight, o.pk))[:FEATURED_LIMIT]
    return {
        "cycle": cycle,
        "expected_progress": expected,
        "progress": official_company_progress(everything),
        "filters": filters,
        "selection_progress": selection_progress(objectives),
        "objective_stats": stats,
        "in_progress_count": stats["total"] - stats[calculator.COMPLETED],
        "pending_approval_count": pending_approval_count(viewer, cycle, filters),
        "key_result_count": sum(len(o.live_key_results) for o in objectives),
        "key_results_completed": sum(1 for o in objectives for kr in o.live_key_results if kr.progress >= 100),
        "stale_count": sum(1 for item in attention if item["reason"] == "stale"),
        # Most urgent first; the full list is the report's job.
        "attention": attention[:DASHBOARD_ATTENTION_LIMIT],
        "attention_total": len(attention),
        "teams": teams,
        "featured": featured,
    }


def _group_progress(objectives, label) -> list:
    groups = {}
    for objective in objectives:
        groups.setdefault(label(objective) or "Chưa xác định", []).append(objective)
    return [
        {"label": name, "count": len(items),
         "progress": calculator.weighted_progress((o.progress, o.weight) for o in items)}
        for name, items in groups.items()
    ]


def trend(objectives) -> list:
    """Daily progress rebuilt from check-ins (JOYOKR ``trend``): each key
    result starts at its progress before its first check-in, then takes
    every day's new values in order."""
    from okr.models import OKRCheckin

    key_results = {kr.pk: kr for o in objectives for kr in o.live_key_results}
    if not key_results:
        return []
    values = {pk: kr.progress for pk, kr in key_results.items()}
    rows = (
        OKRCheckin.objects.filter(key_result_id__in=list(key_results))
        .order_by("created_at", "id")
        .values("key_result_id", "previous_progress", "current_progress", "created_at")
    )
    seen, days = set(), {}
    for row in rows:
        if row["key_result_id"] not in seen:
            values[row["key_result_id"]] = row["previous_progress"]
            seen.add(row["key_result_id"])
        days.setdefault(timezone.localtime(row["created_at"]).date(), []).append(row)
    objective_of = {kr.pk: o for o in objectives for kr in o.live_key_results}

    def objective_value(objective):
        return calculator.weighted_progress((values[kr.pk], kr.weight) for kr in objective.live_key_results)

    # Same arithmetic as recomputing every objective every day, but only the
    # objectives a day's check-ins touched are recomputed (W4: 1,000+
    # objectives over a quarter took seconds).
    daily, points = None, []
    for day, updates in days.items():
        for row in updates:
            values[row["key_result_id"]] = row["current_progress"]
        if daily is None:
            daily = {o.pk: objective_value(o) for o in objectives}
        else:
            for objective in {objective_of[row["key_result_id"]].pk: objective_of[row["key_result_id"]]
                              for row in updates}.values():
                daily[objective.pk] = objective_value(objective)
        points.append({"date": day, "progress": calculator.weighted_progress((daily[o.pk], o.weight) for o in objectives)})
    return points


HISTORY_LIMIT = 50


def _by_rating(objectives, policy: Policy) -> list:
    """Objectives per completion band, using the company's rating ranges
    (score = progress / 100, JOYOKR ``rating``)."""
    bands = sorted(policy.rating_ranges, key=lambda band: Decimal(str(band["min"])))
    counts = {str(band["label"]): 0 for band in bands}
    for objective in objectives:
        label = calculator.rating(Decimal(objective.progress) / 100, bands)
        if label in counts:
            counts[label] += 1
    return [{"label": str(band["label"]), "min": band["min"], "max": band["max"], "count": counts[str(band["label"])]}
            for band in bands]


def _overdue_key_results(objectives, today: datetime.date) -> list:
    """Approved key results past their due date and not finished."""
    items = [
        kr for o in objectives for kr in o.live_key_results
        if kr.due_date is not None and kr.due_date < today and kr.progress < 100
    ]
    for kr in items:
        kr.days_late = (today - kr.due_date).days
    return sorted(items, key=lambda kr: (-kr.days_late, kr.pk))


def _checkin_history(objectives, limit: int = HISTORY_LIMIT) -> tuple:
    """Newest check-ins on the selected key results, and how many there are in all."""
    from okr.models import OKRCheckin

    key_results = {kr.pk: kr for o in objectives for kr in o.live_key_results}
    if not key_results:
        return [], 0
    rows = OKRCheckin.objects.filter(key_result_id__in=list(key_results)).select_related("author")
    history = list(rows.order_by("-created_at", "-id")[:limit])
    for checkin in history:
        checkin.key_result = key_results[checkin.key_result_id]
    return history, rows.count()


def report(viewer: OKRViewer, cycle, filters: dict, health: Optional[str] = None) -> dict:
    """Report page of one cycle (JOYOKR ``report``): approved objectives the
    viewer may see, optionally narrowed by scope, department, team, owner
    and (W4) health. The headline progress stays the official company
    figure; ``selection_progress`` is the narrowed one. Every group, the
    trend, the history and the CSV built from this payload use the same
    narrowed rows."""
    policy = Policy.for_company(viewer.company_id)
    expected = expected_for(cycle)
    everything = visible_objectives(viewer, cycle, approved_only=True)
    narrowed = any(filters.values()) if filters else False
    rows = visible_objectives(viewer, cycle, approved_only=True, filters=filters) if narrowed else list(everything)
    stats = _health_counts(rows, expected, policy)  # also sets each objective's health
    if health:
        rows = [o for o in rows if o.health == health]
        stats = _health_counts(rows, expected, policy)
    attention = _attention_of(rows, expected, policy)
    history, checkin_count = _checkin_history(rows)
    by_owner = {}
    for item in attention:
        owner = item.get("owner")
        group = by_owner.setdefault(
            owner.pk if owner is not None else 0,
            {"owner": owner, "overdue_count": 0, "off_track_count": 0, "items": []},
        )
        group["overdue_count"] += item["reason"] == "stale"
        group["off_track_count"] += item["reason"] == "off_track"
        group["items"].append(item)
    return {
        "cycle": cycle,
        "expected_progress": expected,
        "progress": official_company_progress(everything),
        "selection_progress": selection_progress(rows),
        "objective_stats": stats,
        "key_result_count": sum(len(o.live_key_results) for o in rows),
        "stale_count": sum(1 for item in attention if item["reason"] == "stale"),
        "rows": rows,
        # Grouped by the raw scope code, as JOYOKR does; clients label it.
        "by_scope": _group_progress(rows, lambda o: o.scope),
        "by_owner": _group_progress(rows, lambda o: o.owner.get_full_name() if o.owner_id else ""),
        "by_department": _group_progress(rows, lambda o: o.department.department if o.department_id else ""),
        "by_team": _group_progress(rows, lambda o: o.team.name if o.team_id else ""),
        "by_rating": _by_rating(rows, policy),
        "overdue_key_results": _overdue_key_results(rows, timezone.localdate()),
        "checkin_history": history,
        "checkin_count": checkin_count,
        "trend": trend(rows),
        "checkin_attention_by_owner": sorted(
            by_owner.values(), key=lambda group: -(group["overdue_count"] + group["off_track_count"])
        ),
    }


def _sort_nodes(nodes):
    nodes.sort(key=lambda objective: (SCOPE_RANK.get(objective.scope, 99), objective.pk))


def tree(viewer: OKRViewer, cycle) -> dict:
    """Alignment tree (JOYOKR ``tree``): company-level objectives and every
    descendant the viewer may see; whatever is not reachable from them comes
    back as ``unlinked_items`` roots. Visible drafts are included with
    provisional progress."""
    policy = Policy.for_company(viewer.company_id)
    expected = expected_for(cycle)
    items = visible_objectives(viewer, cycle)
    by_id = {o.pk: o for o in items}
    children = {}
    for objective in items:
        objective.projection = objective_projection(objective, expected, policy, objective.live_key_results)
        objective.children_nodes = []
        children.setdefault(objective.parent_id, []).append(objective)
    linked = set()
    queue = [o.pk for o in items if o.scope == "company"]
    linked.update(queue)
    while queue:
        for child in children.get(queue.pop(0), []):
            if child.pk not in linked:
                linked.add(child.pk)
                queue.append(child.pk)
    for objective in items:
        parent = by_id.get(objective.parent_id)
        if parent is not None and (objective.pk in linked) == (parent.pk in linked):
            parent.children_nodes.append(objective)
    for objective in items:
        _sort_nodes(objective.children_nodes)
    linked_roots = [o for o in items if o.pk in linked and o.scope == "company"]
    unlinked_roots = [
        o for o in items
        if o.pk not in linked and (o.parent_id not in by_id or o.parent_id in linked)
    ]
    _sort_nodes(linked_roots)
    _sort_nodes(unlinked_roots)
    return {"cycle": cycle, "expected_progress": expected, "items": linked_roots, "unlinked_items": unlinked_roots}


def checkin_queue(viewer: OKRViewer, cycle=None) -> list:
    """The viewer's key results that take a check-in now (JOYOKR
    ``checkin_queue``): approved, in an approved objective, cycle active."""
    rows = (
        okrs.key_results_for(viewer.company_id)
        .filter(
            owner_id=viewer.employee_id,
            approval_status=ApprovalStatus.APPROVED,
            objective__approval_status=ApprovalStatus.APPROVED,
            objective__cycle__status="active",
        )
        .order_by("objective__cycle__start_date", "objective_id", "id")
    )
    if cycle is not None:
        rows = rows.filter(objective__cycle=cycle)
    return list(rows)


CHECKIN_STATUSES = ("overdue", "due", "never", "ok")
DUE_WINDOW_DAYS = 2


def checkin_status(last_checkin_at, created_at, policy: Policy, now) -> str:
    """Where a key result stands against the company's check-in interval
    (the same 7/14/30-day rule as the reminders and ``attention_items``):

    - ``overdue``: the last check-in, or the creation when there is none, is
      older than the interval;
    - ``due``: it falls overdue within ``DUE_WINDOW_DAYS`` days;
    - ``never``: no check-in yet, not due;
    - ``ok``: checked in recently.
    """
    age_days = max(0, math.floor((now - (last_checkin_at or created_at)).total_seconds() / 86400))
    if age_days > policy.reminder_days:
        return "overdue"
    if age_days >= policy.reminder_days - DUE_WINDOW_DAYS:
        return "due"
    return "ok" if last_checkin_at else "never"


def checkin_management(viewer: OKRViewer, cycle, filters: Optional[dict] = None) -> dict:
    """Read-only check-in overview for managers and admins (JOYOKR
    ``checkin_management_tree``): each managed person once, under their
    department and team, with approved key results and recent check-ins.
    Other employees get ``can_manage`` false and nothing else.

    W4: ``filters`` narrows by ``department_id``, ``team_id``, ``owner_id``
    (within the people the viewer manages) and ``status``; each key result
    carries its ``checkin_status``."""
    from django.db.models import Count, F, Window
    from django.db.models.functions import RowNumber

    from employee.models import Employee

    from okr.models import OKRCheckin, OKRTeamMember

    filters = {key: value for key, value in (filters or {}).items() if value}
    empty = {"cycle": cycle, "can_manage": False, "scope": "self", "summary": {},
             "departments": [], "unassigned": [], "expected_progress": None, "filters": filters}
    managed = viewer.managed_employee_ids() if viewer.is_active else frozenset()
    if managed is not None and not managed:
        return empty
    people = Employee._base_manager.filter(
        is_active=True, employee_work_info__company_id=viewer.company_id
    ).select_related("employee_work_info__department_id")
    if managed is not None:
        people = people.filter(pk__in=managed)
    if filters.get("owner_id"):
        people = people.filter(pk=filters["owner_id"])
    people = {person.pk: person for person in people}
    teams = {}
    for membership in (
        OKRTeamMember.objects.for_company(viewer.company_id)
        .filter(employee_id__in=list(people), team__status="active")
        .select_related("team__department")
        .order_by("employee_id", "team__name", "team_id")
    ):
        teams.setdefault(membership.employee_id, membership.team)
    if filters.get("team_id"):
        members = set(
            OKRTeamMember.objects.for_company(viewer.company_id)
            .filter(team_id=filters["team_id"], employee_id__in=list(people))
            .values_list("employee_id", flat=True)
        )
        people = {pk: person for pk, person in people.items() if pk in members}

    def department_of(person):
        team = teams.get(person.pk)
        if team is not None and team.department_id:
            return team.department
        work = getattr(person, "employee_work_info", None)
        return work.department_id if work is not None else None

    if filters.get("department_id"):
        people = {
            pk: person for pk, person in people.items()
            if department_of(person) is not None and department_of(person).pk == filters["department_id"]
        }
    policy = Policy.for_company(viewer.company_id)
    expected = expected_for(cycle)
    key_results = list(
        okrs.key_results_for(viewer.company_id)
        .filter(
            owner_id__in=list(people),
            objective__cycle=cycle,
            approval_status=ApprovalStatus.APPROVED,
            objective__approval_status=ApprovalStatus.APPROVED,
        )
        .order_by("owner_id", "objective_id", "id")
    )
    key_result_ids = [kr.pk for kr in key_results]
    # Only the newest CHECKIN_HISTORY check-ins of each key result leave the
    # database (a window function); the totals are counted there too.
    histories = {}
    for checkin in (
        OKRCheckin.objects.filter(key_result_id__in=key_result_ids)
        .annotate(
            rank=Window(RowNumber(), partition_by=[F("key_result_id")],
                        order_by=[F("created_at").desc(), F("id").desc()])
        )
        .filter(rank__lte=CHECKIN_HISTORY)
        .select_related("author")
        .order_by("key_result_id", "-created_at", "-id")
    ):
        histories.setdefault(checkin.key_result_id, []).append(checkin)
    totals = dict(
        OKRCheckin.objects.filter(key_result_id__in=key_result_ids)
        .values("key_result_id")
        .annotate(total=Count("id"))
        .values_list("key_result_id", "total")
    )
    now = timezone.now()
    objectives_by_person = {}
    summary = {"employee_count": len(people), "key_result_count": 0, "never_checked_in": 0, "checkin_count": 0,
               "overdue_count": 0, "due_count": 0}
    for kr in key_results:
        kr.history = histories.get(kr.pk, [])
        kr.checkin_total = totals.get(kr.pk, 0)
        kr.health = calculator.status(kr.progress, expected, policy.warning_threshold)
        kr.checkin_status = checkin_status(kr.history[0].created_at if kr.history else None, kr.created_at,
                                           policy, now)
        if filters.get("status") and kr.checkin_status != filters["status"]:
            continue
        per_person = objectives_by_person.setdefault(kr.owner_id, {})
        per_person.setdefault(kr.objective_id, (kr.objective, []))[1].append(kr)
        summary["key_result_count"] += 1
        summary["never_checked_in"] += not kr.history
        summary["checkin_count"] += kr.checkin_total
        summary["overdue_count"] += kr.checkin_status == "overdue"
        summary["due_count"] += kr.checkin_status == "due"
    if filters.get("status"):
        # A status filter lists only the people who have such a key result.
        people = {pk: person for pk, person in people.items() if pk in objectives_by_person}
        summary["employee_count"] = len(people)
    departments, unassigned = {}, []
    for person in sorted(people.values(), key=lambda p: p.get_full_name().lower()):
        node = {
            "employee": person,
            "objectives": sorted(
                objectives_by_person.get(person.pk, {}).values(), key=lambda pair: pair[0].title.lower()
            ),
        }
        team = teams.get(person.pk)
        department = department_of(person)
        if department is None or not department.is_active:
            unassigned.append(node)
            continue
        department_node = departments.setdefault(department.pk, {"department": department, "teams": {}})
        team_node = department_node["teams"].setdefault(team.pk if team else 0, {"team": team, "people": []})
        team_node["people"].append(node)
    ordered = sorted(departments.values(), key=lambda node: node["department"].department.lower())
    for department_node in ordered:
        department_node["teams"] = sorted(
            department_node["teams"].values(),
            key=lambda node: (node["team"] is None, (node["team"].name if node["team"] else "").lower()),
        )
    return {"cycle": cycle, "can_manage": True, "scope": "company" if managed is None else "managed",
            "filters": filters,
            "summary": summary, "departments": ordered, "unassigned": unassigned,
            "expected_progress": expected}
