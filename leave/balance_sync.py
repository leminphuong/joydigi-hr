"""Opening the leave balances that are missing — one place for every caller.

2026-11-11. Every active employee must hold a balance of every active leave
type they are eligible for, without HR assigning it person by person. Events
(a type created or re-activated, an employee hired, transferred or
re-activated) already open balances through ``leave/signals.py``; what this
module adds is the part events can not do: finding and closing the gaps left
by data from before the automatic sync, safely and repeatably.

Callers:

* ``manage.py backfill_available_leave`` — HR-approved runs, with a dry run,
  a scope, explicit amounts (``--grant``) and an expected row count;
* ``run_scheduled_sync`` — the periodic job in ``leave/scheduler.py``. It is
  OFF unless ``LEAVE_AUTO_SYNC_ENABLED`` is true, because it seeds the
  configured ``total_days``: switch it on only once every leave type's
  configured days are the approved ones.

Rules, the same for both:

* only creates: an existing balance — used, edited or switched off — is
  never read back, recomputed or overwritten, and nothing is added to it;
* company scope and the type's conditions are respected; inactive employees
  and inactive or compensatory types are left out;
* a balance HR deleted (a ``-`` record in its history) is never re-opened;
  when the history can not be read, nothing is opened at all;
* the configured days are used only when ``leave_type_quota_problem`` accepts
  them; an explicit amount is used as given;
* all-or-nothing: any error rolls every row of the run back;
* the database's ``unique_together (leave_type_id, employee_id)`` stops two
  concurrent runs from opening the same balance twice.
"""

import logging
import os
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction

from leave.services import (
    ensure_available_leave,
    evaluate_leave_type_conditions,
    leave_type_matches_company,
    leave_type_quota_problem,
    revoked_pairs,
)

logger = logging.getLogger("leave.sync")

#: Rows one scheduled run may open before it stops and asks for a manual run.
DEFAULT_SCHEDULED_MAX_ROWS = 50


class SyncScopeError(ValueError):
    """The requested scope or amounts name something outside the scope."""


class SyncRefused(Exception):
    """A run was refused; nothing was written."""


@dataclass
class PlannedBalance:
    employee: object
    leave_type: object
    days: float = None
    source: str = "none"
    problem: str = None


@dataclass
class SyncPlan:
    employee_ids: list
    leave_type_ids: list
    company_ids: list
    employees: list = field(default_factory=list)
    leave_types: list = field(default_factory=list)
    skipped_compensatory_types: list = field(default_factory=list)
    skipped_shared_types: list = field(default_factory=list)
    existing_pairs: set = field(default_factory=set)
    revocation_checked: bool = True
    missing: list = field(default_factory=list)
    skipped_company: int = 0
    skipped_condition: int = 0
    skipped_revoked: list = field(default_factory=list)
    ignored_grants: list = field(default_factory=list)

    @property
    def without_amount(self):
        return [row for row in self.missing if row.days is None]


def auto_sync_enabled():
    """``LEAVE_AUTO_SYNC_ENABLED`` from settings, else from the environment."""
    value = getattr(settings, "LEAVE_AUTO_SYNC_ENABLED", None)
    if value is None:
        value = os.environ.get("LEAVE_AUTO_SYNC_ENABLED", "")
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def scheduled_max_rows():
    value = getattr(settings, "LEAVE_AUTO_SYNC_MAX_ROWS", None)
    if value is None:
        value = os.environ.get("LEAVE_AUTO_SYNC_MAX_ROWS", "")
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return DEFAULT_SCHEDULED_MAX_ROWS


def plan_missing_balances(
    *,
    employee_ids=(),
    leave_type_ids=(),
    company_ids=(),
    grant_by_type=None,
    grant_by_pair=None,
    use_type_total=False,
    include_compensatory=False,
):
    """What a run would open, and why everything else is left alone. Reads only."""
    from employee.models import Employee
    from leave.models import AvailableLeave, LeaveType

    grant_by_type = dict(grant_by_type or {})
    grant_by_pair = dict(grant_by_pair or {})
    plan = SyncPlan(list(employee_ids), list(leave_type_ids), list(company_ids))

    # Base managers: the default ones filter by an HR web session's selected
    # company, which the job and the command never have but a caller might.
    employees = Employee._base_manager.filter(is_active=True).select_related("employee_work_info__company_id")
    if employee_ids:
        employees = employees.filter(pk__in=employee_ids)
    if company_ids:
        employees = employees.filter(employee_work_info__company_id__in=company_ids)
    plan.employees = list(employees.order_by("pk"))

    leave_types = LeaveType._base_manager.filter(is_active=True)
    if leave_type_ids:
        leave_types = leave_types.filter(pk__in=leave_type_ids)
    # A type drawing on another type's fund has no balance of its own: only
    # the fund's type is opened (2026-11-11) — never 12 + 12 days.
    plan.skipped_shared_types = list(
        leave_types.filter(shared_balance_with__isnull=False).values_list("pk", "shared_balance_with")
    )
    leave_types = leave_types.filter(shared_balance_with__isnull=True)
    if not include_compensatory:
        plan.skipped_compensatory_types = list(
            leave_types.filter(is_compensatory_leave=True).values_list("pk", flat=True)
        )
        leave_types = leave_types.exclude(is_compensatory_leave=True)
    plan.leave_types = list(leave_types.order_by("pk"))

    shared = dict(plan.skipped_shared_types)
    for type_id in sorted(set(grant_by_type) | {t for _e, t in grant_by_pair}):
        if type_id in shared:
            raise SyncScopeError(
                f"--grant: leave type {type_id} shares the fund of leave type {shared[type_id]} — "
                f"grant leave type {shared[type_id]} instead (one fund, never two)."
            )
    _check_grants(grant_by_type, grant_by_pair, plan.employees, plan.leave_types, company_ids)

    employee_pks = [e.pk for e in plan.employees]
    type_pks = [t.pk for t in plan.leave_types]
    plan.existing_pairs = set(
        AvailableLeave._base_manager.filter(employee_id__in=employee_pks, leave_type_id__in=type_pks).values_list(
            "employee_id_id", "leave_type_id_id"
        )
    )
    revoked = revoked_pairs(employee_pks, type_pks)
    plan.revocation_checked = revoked is not None
    revoked = revoked or {}

    for employee in plan.employees:
        for leave_type in plan.leave_types:
            pair = (employee.pk, leave_type.pk)
            if pair in plan.existing_pairs:
                if pair in grant_by_pair:
                    plan.ignored_grants.append(pair)
                continue
            if not leave_type_matches_company(leave_type, employee):
                plan.skipped_company += 1
                continue
            eligible, _reason = evaluate_leave_type_conditions(leave_type, employee)
            if not eligible:
                plan.skipped_condition += 1
                continue
            if pair in revoked:
                plan.skipped_revoked.append((employee, leave_type, revoked[pair]))
                continue
            plan.missing.append(_amount(employee, leave_type, grant_by_type, grant_by_pair, use_type_total))
    return plan


def apply_plan(plan, *, expect_count=None, reason):
    """Open every row of ``plan``, all or nothing. Returns the rows opened."""
    if not plan.revocation_checked:
        raise SyncRefused(
            "the balance history can not be read, so a revoked balance can not be told "
            "from one never opened — nothing written"
        )
    if plan.without_amount:
        types = sorted({(r.leave_type.pk, r.leave_type.name, r.leave_type.total_days, r.problem) for r in plan.without_amount})
        raise SyncRefused(
            "no amount for "
            + ", ".join(
                f"type {pk} ({name}, configured {_fmt(total)} days{'; ' + problem if problem else ''})"
                for pk, name, total, problem in types
            )
            + " — nothing written. Pass --grant TYPE_ID=DAYS, or --use-type-total to seed the configured total_days."
        )
    if expect_count is not None and expect_count != len(plan.missing):
        raise SyncRefused(f"--expect-count {expect_count} but {len(plan.missing)} row(s) would be created — nothing written.")

    opened = []
    with transaction.atomic():
        for row in plan.missing:
            _balance, created = ensure_available_leave(row.employee, row.leave_type, available_days=row.days, reason=reason)
            if not created:
                raise SyncRefused(
                    f"employee={row.employee.pk} leave_type={row.leave_type.pk}: the row appeared meanwhile "
                    "(or became ineligible) — rolled back, nothing written"
                )
            opened.append(row)
    return opened


def run_scheduled_sync():
    """The periodic job: open the gaps with the configured days.

    Returns a summary dict, or ``None`` when ``LEAVE_AUTO_SYNC_ENABLED`` is off.
    Types whose configured days are rejected are left out (and logged); a run
    that would open more than ``LEAVE_AUTO_SYNC_MAX_ROWS`` rows opens nothing
    and asks for an HR-approved ``backfill_available_leave`` run instead.
    """
    if not auto_sync_enabled():
        return None
    plan = plan_missing_balances(use_type_total=True)
    rejected = plan.without_amount
    plan.missing = [row for row in plan.missing if row.days is not None]
    summary = {
        "would_open": len(plan.missing),
        "rejected_config": len(rejected),
        "skipped_revoked": len(plan.skipped_revoked),
        "opened": 0,
    }
    for row in rejected:
        logger.warning(
            "scheduled leave sync: type %s (%s) left out: %s", row.leave_type.pk, row.leave_type.name, row.problem
        )
    limit = scheduled_max_rows()
    if len(plan.missing) > limit:
        logger.error(
            "scheduled leave sync: %s balances missing, more than LEAVE_AUTO_SYNC_MAX_ROWS=%s — nothing opened; "
            "review with `manage.py backfill_available_leave --dry-run`",
            len(plan.missing), limit,
        )
        summary["refused"] = "too many rows"
        return summary
    if not plan.missing:
        return summary
    try:
        opened = apply_plan(plan, reason="Tự động: đồng bộ định kỳ")
    except SyncRefused as exc:
        logger.warning("scheduled leave sync refused: %s", exc)
        summary["refused"] = str(exc)
        return summary
    summary["opened"] = len(opened)
    logger.info("scheduled leave sync: opened %s balance(s)", len(opened))
    return summary


def _amount(employee, leave_type, grant_by_type, grant_by_pair, use_type_total):
    pair = (employee.pk, leave_type.pk)
    if pair in grant_by_pair:
        return PlannedBalance(employee, leave_type, grant_by_pair[pair], "grant:employee")
    if leave_type.pk in grant_by_type:
        return PlannedBalance(employee, leave_type, grant_by_type[leave_type.pk], "grant:type")
    if use_type_total:
        problem = leave_type_quota_problem(leave_type)
        if problem:
            return PlannedBalance(employee, leave_type, None, "none", problem)
        return PlannedBalance(employee, leave_type, leave_type.total_days, "type_total")
    return PlannedBalance(employee, leave_type, None, "none")


def _check_grants(grant_by_type, grant_by_pair, employees, leave_types, company_ids):
    """Every explicit amount must name something inside the scope — a typo is an error."""
    types = {t.pk: t for t in leave_types}
    employee_pks = {e.pk for e in employees}
    for type_id in sorted(set(grant_by_type) | {t for _e, t in grant_by_pair}):
        leave_type = types.get(type_id)
        if leave_type is None:
            raise SyncScopeError(
                f"--grant: leave type {type_id} is not in the scope "
                "(unknown, inactive, compensatory, or left out by --leave-type)."
            )
        if company_ids and leave_type.company_id_id is not None and leave_type.company_id_id not in company_ids:
            raise SyncScopeError(
                f"--grant: leave type {type_id} belongs to company {leave_type.company_id_id}, not {list(company_ids)}."
            )
    for employee_id, _type_id in sorted(grant_by_pair):
        if employee_id not in employee_pks:
            raise SyncScopeError(
                f"--grant: employee {employee_id} is not in the scope "
                "(unknown, inactive, another company, or left out by --employee)."
            )


def _fmt(days):
    return f"{days:g}" if days is not None else "?"
