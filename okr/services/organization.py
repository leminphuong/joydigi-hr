"""People, departments, teams, OKR roles and settings for the OKR web.

HR owns employees and departments, so here they are read only: nobody
creates an OKR-only account or department (JOYOKR's users and departments
admin is replaced by HR). OKR keeps its own teams and team members, the OKR
manager of each HR department, the OKR roles and the per-company settings
(JOYOKR ``JoyOKR_Organization_Controller`` and the settings endpoints).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count

from base.models import Department
from okr import pilot
from okr.models import (
    OKRDepartmentRole,
    OKRRole,
    OKRRoleEvent,
    OKRSettings,
    OKRTeam,
    OKRTeamMember,
    validate_rating_ranges,
)
from okr.models.base import company_department_filter, department_allows_company
from okr.models.choices import (
    OKRRoleType,
    RecordStatus,
    ReminderFrequency,
    RoleEventAction,
    RoleEventRole,
)
from okr.services import errors
from okr.services import search as text_search
from okr.services.access import OKRViewer
from okr.services.okrs import active_employee


def require_admin(viewer: OKRViewer, message: str = "Chỉ quản trị OKR mới thực hiện được thao tác này."):
    if not (viewer.is_active and viewer.is_admin):
        raise errors.forbidden(message)


# -- people and departments (read from HR) -------------------------------------


def people(viewer: OKRViewer, *, search: str = "", department_id=None):
    """Active employees of the viewer's company, for owner/member pickers.
    Only ids, names and the organisational links are exposed."""
    from employee.models import Employee

    rows = (
        Employee._base_manager.filter(
            is_active=True,
            employee_user_id__is_active=True,
            employee_work_info__company_id=viewer.company_id,
        )
        .select_related("employee_work_info__department_id")
        .order_by("employee_first_name", "employee_last_name", "id")
    )
    if department_id:
        rows = rows.filter(employee_work_info__department_id=department_id)
    if pilot.is_active():
        # Pilot: pickers offer only people who can actually open OKR.
        rows = rows.filter(pk__in=pilot.pilot_employee_ids())
    # Vietnamese-aware and independent of the database collation.
    return text_search.search(rows, search, ["employee_first_name", "employee_last_name"])


def _company_departments(company_id: int) -> list:
    """Active HR departments the company uses (``company_department_filter``:
    linked to it, shared, or staffed by its active employees), in one query.
    Read through the base manager: HR's default manager filters by the web
    session's selected company, while OKR always works in the employee's own
    company."""
    return list(
        Department._base_manager.filter(is_active=True)
        .filter(company_department_filter(company_id))
        .distinct()
        .order_by("department", "id")
    )


def departments(viewer: OKRViewer) -> list:
    """HR departments usable by the company, each with its OKR manager."""
    rows = _company_departments(viewer.company_id)
    managers = {
        role.department_id: role.manager
        for role in OKRDepartmentRole.objects.for_company(viewer.company_id).select_related("manager")
    }
    for department in rows:
        department.okr_manager = managers.get(department.pk)
    return rows


def _department(viewer: OKRViewer, department_id):
    department = Department._base_manager.filter(pk=department_id, is_active=True).first() if department_id else None
    if department is None or not department_allows_company(department, viewer.company_id):
        return None
    return department


def _log_role_event(viewer: OKRViewer, employee, role: str, action: str, department=None) -> None:
    OKRRoleEvent.objects.create(
        company_id=viewer.company_id,
        employee=employee,
        role=role,
        action=action,
        department=department,
        actor=viewer.user,
    )


def set_department_manager(viewer: OKRViewer, department_id, manager_id) -> Department:
    """Name (or clear, with ``None``) the OKR manager of an HR department."""
    require_admin(viewer)
    department = _department(viewer, department_id)
    if department is None:
        raise errors.OKRError("not_found", "Không tìm thấy phòng ban.", 404)
    manager = None
    if manager_id:
        manager = active_employee(viewer.company_id, manager_id)
        if manager is None:
            raise errors.validation({"manager_id": "Người quản lý không hợp lệ."})
        if not pilot.allows(manager.pk):
            raise errors.validation({"manager_id": pilot.NOT_IN_PILOT})
    with transaction.atomic():
        role = (
            OKRDepartmentRole.objects.for_company(viewer.company_id)
            .select_for_update()
            .filter(department=department)
            .select_related("manager")
            .first()
        )
        previous = role.manager if role is not None else None
        if previous is not None and (manager is None or previous.pk != manager.pk):
            _log_role_event(viewer, previous, RoleEventRole.DEPARTMENT_MANAGER, RoleEventAction.REVOKED, department)
        if manager is None:
            if role is not None:
                role.delete()
        else:
            if role is None:
                role = OKRDepartmentRole(company_id=viewer.company_id, department=department)
            role.manager = manager
            role.save()
            if previous is None or previous.pk != manager.pk:
                _log_role_event(viewer, manager, RoleEventRole.DEPARTMENT_MANAGER, RoleEventAction.GRANTED, department)
    department.okr_manager = manager
    return department


# -- teams ------------------------------------------------------------------------


def teams(viewer: OKRViewer, status: str = ""):
    rows = (
        OKRTeam.objects.for_company(viewer.company_id)
        .select_related("department", "manager")
        .annotate(member_count=Count("memberships"))
        .order_by("name", "id")
    )
    if status:
        if status not in RecordStatus.values:
            raise errors.validation({"status": "Giá trị không hợp lệ."})
        rows = rows.filter(status=status)
    return rows


def get_team(viewer: OKRViewer, team_id) -> OKRTeam:
    team = teams(viewer).filter(pk=team_id).first() if team_id else None
    if team is None:
        raise errors.OKRError("not_found", "Không tìm thấy team.", 404)
    return team


def member_order():
    """Team memberships with their employee, in name order."""
    return OKRTeamMember.objects.select_related("employee").order_by(
        "employee__employee_first_name", "employee__employee_last_name", "employee_id"
    )


def team_members(team: OKRTeam):
    return member_order().filter(team=team)


def save_team(viewer: OKRViewer, data: dict, team_id=None) -> OKRTeam:
    """Create a team, or update one when ``team_id`` is given."""
    require_admin(viewer)
    team = get_team(viewer, team_id) if team_id else OKRTeam(company_id=viewer.company_id)
    problems = {}
    name = str(data.get("name") or "").strip()
    if not name:
        problems["name"] = "Tên team là bắt buộc."
    department = None
    if data.get("department_id"):
        department = _department(viewer, data.get("department_id"))
        if department is None:
            problems["department_id"] = "Phòng ban không hợp lệ."
    manager = None
    if data.get("manager_id"):
        manager = active_employee(viewer.company_id, data.get("manager_id"))
        if manager is None:
            problems["manager_id"] = "Người quản lý không hợp lệ."
    if problems:
        raise errors.validation(problems)
    team.name = name[:190]
    team.description = str(data.get("description") or "").strip()
    team.department = department
    team.manager = manager
    team.save()
    return get_team(viewer, team.pk)


def set_team_status(viewer: OKRViewer, team_id, status: str) -> OKRTeam:
    """Hide (inactive) or restore (active) a team; its OKRs stay as they are."""
    require_admin(viewer)
    if status not in RecordStatus.values:
        raise errors.validation({"status": "Giá trị không hợp lệ."})
    team = get_team(viewer, team_id)
    team.status = status
    team.save(update_fields=["status", "updated_at"])
    return get_team(viewer, team.pk)


def set_team_members(viewer: OKRViewer, team_id, employee_ids) -> OKRTeam:
    """Replace a team's member list."""
    require_admin(viewer)
    team = get_team(viewer, team_id)
    if not isinstance(employee_ids, (list, tuple)):
        raise errors.validation({"employee_ids": "Danh sách thành viên không hợp lệ."})
    wanted, invalid = {}, []
    for raw in employee_ids:
        employee = active_employee(viewer.company_id, raw)
        if employee is None or not pilot.allows(employee.pk):
            invalid.append(str(raw))
        else:
            wanted[employee.pk] = employee
    if invalid:
        raise errors.validation({"employee_ids": f"Nhân sự không hợp lệ: {', '.join(invalid[:5])}."})
    with transaction.atomic():
        team.memberships.exclude(employee_id__in=wanted).delete()
        existing = set(team.memberships.values_list("employee_id", flat=True))
        for employee_id, employee in wanted.items():
            if employee_id not in existing:
                OKRTeamMember.objects.create(company_id=viewer.company_id, team=team, employee=employee)
    return get_team(viewer, team.pk)


# -- OKR roles ----------------------------------------------------------------------


def roles(viewer: OKRViewer):
    require_admin(viewer)
    return (
        OKRRole.objects.for_company(viewer.company_id)
        .select_related("employee", "granted_by__employee_get")
        .order_by("role", "employee__employee_first_name", "id")
    )


def grant_role(viewer: OKRViewer, employee_id, role: str) -> OKRRole:
    require_admin(viewer)
    problems = {}
    if role not in OKRRoleType.values:
        problems["role"] = "Vai trò không hợp lệ."
    employee = active_employee(viewer.company_id, employee_id)
    if employee is None:
        problems["employee_id"] = "Nhân sự không hợp lệ."
    elif not pilot.allows(employee.pk):
        problems["employee_id"] = pilot.NOT_IN_PILOT
    if problems:
        raise errors.validation(problems)
    try:
        with transaction.atomic():
            granted = OKRRole.objects.create(
                company_id=viewer.company_id, employee=employee, role=role, granted_by=viewer.user
            )
            _log_role_event(viewer, employee, role, RoleEventAction.GRANTED)
            return granted
    except IntegrityError:
        raise errors.conflict("role_exists", "Nhân sự này đã có vai trò đó.")


def revoke_role(viewer: OKRViewer, role_id) -> None:
    """Remove a role; the company's last OKR admin cannot be removed."""
    require_admin(viewer)
    with transaction.atomic():
        role = (
            OKRRole.objects.for_company(viewer.company_id)
            .select_for_update()
            .filter(pk=role_id)
            .select_related("employee")
            .first()
        )
        if role is None:
            raise errors.OKRError("not_found", "Không tìm thấy vai trò.", 404)
        if role.role == OKRRoleType.ADMIN and (
            OKRRole.objects.for_company(viewer.company_id).filter(role=OKRRoleType.ADMIN).count() <= 1
        ):
            raise errors.conflict("last_admin", "Không thể gỡ quản trị OKR cuối cùng của công ty.")
        _log_role_event(viewer, role.employee, role.role, RoleEventAction.REVOKED)
        role.delete()


def role_history(viewer: OKRViewer):
    """Every grant and revocation in the company, newest first (admins only)."""
    require_admin(viewer)
    return (
        OKRRoleEvent.objects.for_company(viewer.company_id)
        .select_related("employee", "department", "actor__employee_get")
        .order_by("-created_at", "-id")
    )


# -- settings -----------------------------------------------------------------------


def get_settings(company_id: int) -> OKRSettings:
    """Stored settings, or the defaults (unsaved) when the company has none."""
    return OKRSettings.objects.filter(company_id=company_id).first() or OKRSettings(company_id=company_id)


def update_settings(viewer: OKRViewer, data: dict) -> OKRSettings:
    """Threshold, reminder interval, e-mail reminders and rating bands.
    Telegram, SSO and chat secrets from JOYOKR are deliberately not settable."""
    require_admin(viewer)
    settings = get_settings(viewer.company_id)
    problems = {}
    if "warning_threshold" in data:
        try:
            threshold = Decimal(str(data["warning_threshold"]))
            if not threshold.is_finite() or not 0 <= threshold <= 100:
                raise InvalidOperation
            settings.warning_threshold = threshold.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            problems["warning_threshold"] = "Ngưỡng phải từ 0 đến 100."
    if "reminder_frequency" in data:
        if data["reminder_frequency"] not in ReminderFrequency.values:
            problems["reminder_frequency"] = "Tần suất nhắc không hợp lệ."
        else:
            settings.reminder_frequency = data["reminder_frequency"]
    if "email_reminders" in data:
        settings.email_reminders = data["email_reminders"] is True
    if "rating_ranges" in data:
        try:
            validate_rating_ranges(data["rating_ranges"])
            settings.rating_ranges = [
                {"min": str(band["min"]), "max": str(band["max"]), "label": str(band["label"]).strip()}
                for band in sorted(data["rating_ranges"], key=lambda band: Decimal(str(band["min"])))
            ]
        except ValidationError as exc:
            problems["rating_ranges"] = exc.messages[0]
    if problems:
        raise errors.validation(problems)
    settings.save()
    return settings
