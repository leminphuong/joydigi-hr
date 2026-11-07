"""Who may see and change which OKRs (JOYOKR ``JoyOKR_Permissions``).

JOYOKR's WordPress capabilities map to people like this:

=====================================  =============================================
JOYOKR                                 Here
=====================================  =============================================
``joyokr_employee``: view, edit own,   every active employee who has a login and
check in, comment, view reports        a company
``joyokr_manager``: + edit managed,    ``OKRRole`` "manager" in that company
approve
``administrator``: everything,         ``OKRRole`` "admin" in that company
including cycles and settings
=====================================  =============================================

"Manages" covers the same three relations JOYOKR checks:

- the owner's ``joyokr_manager_id`` becomes HR's ``reporting_manager``
  within the same company;
- the department manager becomes ``OKRDepartmentRole`` on an active HR
  department;
- the team manager becomes ``OKRTeam.manager`` of an active team.

Every check is limited to the viewer's company. Lookups outside it report
"not found" rather than "forbidden", so ids from other companies leak
nothing.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from functools import cached_property
from typing import Optional

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from okr import pilot
from okr.models import (
    OKRDepartmentRole,
    OKRKeyResult,
    OKRRole,
    OKRTeam,
    OKRTeamMember,
)
from okr.models.base import employee_company_id
from okr.models.choices import (
    ApprovalStatus,
    CycleStatus,
    OKRRoleType,
    RecordStatus,
    Scope,
)
from okr.services.errors import OKRError

EDITABLE_CYCLE_STATUSES = (CycleStatus.DRAFT, CycleStatus.ACTIVE)
LOCKED_STATUSES = (ApprovalStatus.SUBMITTED, ApprovalStatus.APPROVED)


@dataclass(frozen=True)
class ScopeRef:
    """The fields JOYOKR's permission checks read from an objective or KR row."""

    company_id: int
    owner_id: Optional[int]
    scope: str
    department_id: Optional[int]
    team_id: Optional[int]
    approval_status: str
    objective_approval_status: Optional[str]
    cycle_status: str
    cycle_start: Optional[datetime.date] = None
    cycle_end: Optional[datetime.date] = None


def scope_of(item) -> ScopeRef:
    """ScopeRef of an objective, or of a key result (scope from its objective)."""
    if isinstance(item, OKRKeyResult):
        objective = item.objective
        cycle = objective.cycle
        return ScopeRef(
            company_id=item.company_id,
            owner_id=item.owner_id,
            scope=objective.scope,
            department_id=objective.department_id,
            team_id=objective.team_id,
            approval_status=item.approval_status,
            objective_approval_status=objective.approval_status,
            cycle_status=cycle.status,
            cycle_start=cycle.start_date,
            cycle_end=cycle.end_date,
        )
    cycle = item.cycle
    return ScopeRef(
        company_id=item.company_id,
        owner_id=item.owner_id,
        scope=item.scope,
        department_id=item.department_id,
        team_id=item.team_id,
        approval_status=item.approval_status,
        objective_approval_status=None,
        cycle_status=cycle.status,
        cycle_start=cycle.start_date,
        cycle_end=cycle.end_date,
    )


class OKRViewer:
    """One employee acting in their company's OKR space."""

    def __init__(self, employee, company_id: int, user=None):
        self.employee = employee
        self.company_id = company_id
        self.user = user

    @classmethod
    def for_user(cls, user) -> "OKRViewer":
        if user is None or not user.is_authenticated:
            raise OKRError("not_authenticated", "Vui lòng đăng nhập.", 401)
        try:
            employee = user.employee_get
        except ObjectDoesNotExist:
            employee = None
        company_id = employee_company_id(employee)
        if employee is None or company_id is None:
            raise OKRError(
                "no_employee",
                "Tài khoản chưa gắn với nhân viên của công ty nào nên chưa dùng được OKR.",
                403,
            )
        # Pilot gate (okr/pilot.py): every OKR entry point goes through here —
        # the REST API, the /okr/ web and the HR menu entry. Superusers and
        # OKR roles do not bypass it.
        if not pilot.allows(employee.pk):
            raise OKRError(pilot.NOT_ENABLED_CODE, pilot.NOT_ENABLED_MESSAGE, 403)
        return cls(employee, company_id, user)

    @classmethod
    def for_employee(cls, employee) -> Optional["OKRViewer"]:
        """Viewer for someone other than the caller (notification recipients)."""
        company_id = employee_company_id(employee)
        if company_id is None:
            return None
        return cls(employee, company_id, employee.employee_user_id)

    @property
    def employee_id(self) -> int:
        return self.employee.pk

    # -- capabilities -------------------------------------------------------

    @cached_property
    def roles(self) -> frozenset:
        return frozenset(
            OKRRole.objects.filter(company_id=self.company_id, employee_id=self.employee_id)
            .values_list("role", flat=True)
        )

    @property
    def is_admin(self) -> bool:
        """JOYOKR ``manage_settings`` / ``manage_cycles`` / ``manage_organization``."""
        return OKRRoleType.ADMIN in self.roles

    @property
    def is_manager(self) -> bool:
        """JOYOKR ``edit_managed`` + ``approve``."""
        return self.is_admin or OKRRoleType.MANAGER in self.roles

    def capabilities(self) -> list:
        """JOYOKR capability names (without the ``joyokr_`` prefix) the
        viewer holds, for clients that gate menus and buttons by them."""
        if not self.is_active:
            return []
        granted = ["view", "edit_own", "checkin", "comment", "view_reports"]
        if self.is_manager:
            granted += ["edit_managed", "approve"]
        if self.is_admin:
            granted += ["manage_organization", "manage_cycles", "manage_settings"]
        return granted

    @cached_property
    def is_active(self) -> bool:
        """JOYOKR ``is_active_user``: employed and able to log in."""
        return bool(
            self.employee.is_active and self.user is not None and self.user.is_active
        )

    # -- what the viewer manages -----------------------------------------------

    @cached_property
    def direct_report_ids(self) -> frozenset:
        from employee.models import EmployeeWorkInformation

        return frozenset(
            EmployeeWorkInformation._base_manager.filter(
                reporting_manager_id=self.employee_id, company_id=self.company_id
            ).values_list("employee_id", flat=True)
        )

    @cached_property
    def managed_department_ids(self) -> frozenset:
        return frozenset(
            OKRDepartmentRole.objects.for_company(self.company_id)
            .filter(manager_id=self.employee_id, department__is_active=True)
            .values_list("department_id", flat=True)
        )

    @cached_property
    def managed_team_ids(self) -> frozenset:
        return frozenset(
            OKRTeam.objects.for_company(self.company_id)
            .filter(manager_id=self.employee_id, status=RecordStatus.ACTIVE)
            .values_list("pk", flat=True)
        )

    def managed_employee_ids(self) -> Optional[frozenset]:
        """People in the viewer's management view (JOYOKR ``managed_user_ids``):
        direct reports, members of managed departments and of managed teams.
        ``None`` means everyone in the company (admins)."""
        if self.is_admin:
            return None
        if not self.is_manager:
            return frozenset()
        from employee.models import EmployeeWorkInformation

        ids = set(self.direct_report_ids)
        if self.managed_department_ids:
            ids.update(
                EmployeeWorkInformation._base_manager.filter(
                    company_id=self.company_id, department_id__in=self.managed_department_ids
                ).values_list("employee_id", flat=True)
            )
        if self.managed_team_ids:
            ids.update(
                OKRTeamMember.objects.for_company(self.company_id)
                .filter(team_id__in=self.managed_team_ids)
                .values_list("employee_id", flat=True)
            )
        ids.discard(None)
        return frozenset(ids)

    # -- JOYOKR permission checks -------------------------------------------------

    def _same_company(self, ref: ScopeRef) -> bool:
        return ref.company_id == self.company_id

    def can_manage_scope(self, ref: ScopeRef) -> bool:
        if not self._same_company(ref):
            return False
        if self.is_admin:
            return True
        if ref.owner_id is not None and ref.owner_id == self.employee_id:
            return True
        if not self.is_manager:
            return False
        if ref.owner_id is not None and ref.owner_id in self.direct_report_ids:
            return True
        if ref.department_id and ref.department_id in self.managed_department_ids:
            return True
        return bool(ref.team_id) and ref.team_id in self.managed_team_ids

    def can_view_scope(self, ref: ScopeRef) -> bool:
        if not self._same_company(ref) or not self.is_active:
            return False
        if self.is_admin:
            return True
        if ref.scope == Scope.COMPANY or ref.approval_status == ApprovalStatus.APPROVED:
            return True
        return self.can_manage_scope(ref)

    def can_edit_okr(self, ref: ScopeRef) -> bool:
        """May change this OKR while its cycle is open. Managers edit
        department and team OKRs they manage, not their reports' personal ones."""
        if not self._same_company(ref):
            return False
        if ref.cycle_status not in EDITABLE_CYCLE_STATUSES or not self.is_active:
            return False
        if self.is_admin:
            return True
        if ref.owner_id is not None and ref.owner_id == self.employee_id:
            return True
        if not self.is_manager:
            return False
        if ref.scope == Scope.DEPARTMENT and ref.department_id:
            return ref.department_id in self.managed_department_ids
        return ref.scope == Scope.TEAM and bool(ref.team_id) and ref.team_id in self.managed_team_ids

    def can_modify_definition(self, ref: ScopeRef) -> bool:
        """Editing the definition is locked once submitted or approved (admins excepted)."""
        if not self.can_edit_okr(ref):
            return False
        if self.is_admin:
            return True
        return (
            ref.approval_status not in LOCKED_STATUSES
            and ref.objective_approval_status not in LOCKED_STATUSES
        )

    def can_create_objective(
        self, *, scope: str, owner_id: int, department_id, team_id, cycle_status: str
    ) -> bool:
        if cycle_status == CycleStatus.CLOSED:
            return False
        if self.is_admin:
            return True
        if scope == Scope.PERSONAL:
            return owner_id == self.employee_id
        if scope == Scope.COMPANY:
            return False
        return self.can_manage_scope(
            ScopeRef(
                company_id=self.company_id,
                owner_id=None,
                scope=scope,
                department_id=department_id,
                team_id=team_id,
                approval_status=ApprovalStatus.DRAFT,
                objective_approval_status=None,
                cycle_status=cycle_status,
            )
        )

    def can_review(self, ref: ScopeRef) -> bool:
        """JOYOKR ``approve`` capability + ``can_manage_scope``."""
        return self.is_manager and self.can_manage_scope(ref)

    def can_approve_now(self, ref: ScopeRef) -> bool:
        """The approve button test (JOYOKR ``decorate_status`` ``can_approve``)."""
        return (
            ref.approval_status == ApprovalStatus.SUBMITTED
            and ref.cycle_status in EDITABLE_CYCLE_STATUSES
            and self.can_review(ref)
            and (ref.owner_id != self.employee_id or self.is_admin)
        )

    # -- list filters (JOYOKR ``visibility_clause``) -----------------------------

    def _visibility(self, prefix: str, owner: str, status: str) -> Q:
        if not self.is_active:
            return Q(pk__in=[])
        if self.is_admin:
            return Q()
        q = (
            Q(**{f"{prefix}scope": Scope.COMPANY})
            | Q(**{status: ApprovalStatus.APPROVED})
            | Q(**{owner: self.employee_id})
        )
        if self.is_manager:
            q |= (
                Q(**{f"{owner}__in": self.direct_report_ids})
                | Q(**{f"{prefix}department_id__in": self.managed_department_ids})
                | Q(**{f"{prefix}team_id__in": self.managed_team_ids})
            )
        return q

    def objective_visibility(self) -> Q:
        return self._visibility("", "owner_id", "approval_status")

    def key_result_visibility(self) -> Q:
        return self._visibility("objective__", "owner_id", "approval_status")
