"""OKR-only organisation data: teams and OKR roles on HR departments.

HR has no team concept and its ``Department`` has no manager, so both live
here instead of in the HR schema.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from okr.models.base import AppendOnlyModel, CompanyScopedModel, OKRQuerySet, TimeStampedModel
from okr.models.choices import (
    DepartmentRoleType,
    OKRRoleType,
    RecordStatus,
    RoleEventAction,
    RoleEventRole,
)


class OKRTeam(CompanyScopedModel, TimeStampedModel):
    department = models.ForeignKey(
        "base.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="okr_teams",
        verbose_name=_("Department"),
    )
    name = models.CharField(max_length=190, verbose_name=_("Name"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    manager = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="okr_teams_managed",
        verbose_name=_("Manager"),
    )
    status = models.CharField(
        max_length=20, choices=RecordStatus.choices, default=RecordStatus.ACTIVE
    )
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_team"
        verbose_name = _("OKR team")
        indexes = [models.Index(fields=["company", "status"], name="okr_team_company_status")]

    def __str__(self):
        return self.name

    def employee_references(self):
        yield "manager", self.manager

    def department_references(self):
        yield "department", self.department


class OKRTeamMember(CompanyScopedModel):
    team = models.ForeignKey(
        OKRTeam, on_delete=models.CASCADE, related_name="memberships", verbose_name=_("Team")
    )
    # Named ``employee`` on purpose: HR's account deletion treats it as data
    # owned by the employee and removes the membership with them.
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        related_name="okr_team_memberships",
        verbose_name=_("Employee"),
    )
    joined_at = models.DateTimeField(default=timezone.now, verbose_name=_("Joined at"))
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True)

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_team_member"
        verbose_name = _("OKR team member")
        constraints = [
            models.UniqueConstraint(fields=["team", "employee"], name="okr_team_member_unique"),
        ]

    def inherit_company(self):
        return self.team.company_id if self.team_id else None

    def company_references(self):
        yield "team", self.team.company_id if self.team_id else None

    def employee_references(self):
        yield "employee", self.employee


class OKRDepartmentRole(CompanyScopedModel, TimeStampedModel):
    """Who manages OKRs for an HR department (JOYOKR ``departments.manager_user_id``)."""

    department = models.ForeignKey(
        "base.Department",
        on_delete=models.CASCADE,
        related_name="okr_roles",
        verbose_name=_("Department"),
    )
    manager = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        related_name="okr_department_roles",
        verbose_name=_("Manager"),
    )
    role = models.CharField(
        max_length=20, choices=DepartmentRoleType.choices, default=DepartmentRoleType.MANAGER
    )
    legacy_wp_code = models.CharField(max_length=40, blank=True, verbose_name=_("JOYOKR department code"))

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_department_role"
        verbose_name = _("OKR department role")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "department", "role"], name="okr_department_role_unique"
            ),
        ]

    def employee_references(self):
        yield "manager", self.manager

    def department_references(self):
        yield "department", self.department


class OKRRole(CompanyScopedModel, TimeStampedModel):
    """OKR rights beyond those every active employee has.

    JOYOKR granted them through the WordPress roles ``joyokr_manager``
    (approve, and edit what one manages) and ``administrator`` (everything,
    including cycles and settings). Here they are granted per company, one
    row per right. Being an HR admin or a Django superuser never implies an
    OKR role.
    """

    # Named ``employee`` + CASCADE: HR's account deletion removes the role
    # together with the employee.
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        related_name="okr_roles",
        verbose_name=_("Employee"),
    )
    role = models.CharField(max_length=20, choices=OKRRoleType.choices)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Granted by"),
    )

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_role"
        verbose_name = _("OKR role")
        constraints = [
            models.UniqueConstraint(fields=["company", "employee", "role"], name="okr_role_unique"),
        ]

    def __str__(self):
        return f"{self.employee} · {self.get_role_display()}"

    def employee_references(self):
        yield "employee", self.employee


class OKRRoleEvent(AppendOnlyModel, CompanyScopedModel):
    """Who granted or revoked which OKR right, and when (W3 role history).

    ``OKRRole`` and ``OKRDepartmentRole`` only hold the current state; their
    rows are deleted on revoke, so the history lives here, written in the
    same transaction as the change.
    """

    # Named ``employee`` + CASCADE: HR's account deletion treats the history
    # as the employee's own data and removes it with them.
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        related_name="okr_role_events",
        verbose_name=_("Employee"),
    )
    role = models.CharField(max_length=30, choices=RoleEventRole.choices)
    action = models.CharField(max_length=20, choices=RoleEventAction.choices)
    department = models.ForeignKey(
        "base.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Department"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Changed by"),
    )

    class Meta:
        db_table = "okr_role_event"
        verbose_name = _("OKR role change")
        verbose_name_plural = _("OKR role changes")
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["company", "created_at"], name="okr_role_event_company")]

    def employee_references(self):
        yield "employee", self.employee

    def department_references(self):
        yield "department", self.department
