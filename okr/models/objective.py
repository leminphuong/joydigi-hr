from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from okr.models.base import CompanyScopedModel, SoftDeleteModel, TimeStampedModel
from okr.models.choices import ApprovalStatus, Priority, Scope

PERCENT = {"max_digits": 7, "decimal_places": 2}
METRIC = {"max_digits": 18, "decimal_places": 4}


def _between_0_and_100(field):
    return Q(**{f"{field}__gte": 0}) & Q(**{f"{field}__lte": 100})


class OKRObjective(SoftDeleteModel, CompanyScopedModel, TimeStampedModel):
    cycle = models.ForeignKey(
        "okr.OKRCycle", on_delete=models.PROTECT, related_name="objectives", verbose_name=_("Cycle")
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("Parent objective"),
    )
    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.PERSONAL)
    department = models.ForeignKey(
        "base.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="okr_objectives",
        verbose_name=_("Department"),
    )
    team = models.ForeignKey(
        "okr.OKRTeam",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="objectives",
        verbose_name=_("Team"),
    )
    # Nullable only so HR can detach a deleted employee; new objectives
    # must have an owner (see clean()).
    owner = models.ForeignKey(
        "employee.Employee",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="okr_objectives_owned",
        verbose_name=_("Owner"),
    )
    title = models.CharField(max_length=255, verbose_name=_("Title"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    weight = models.DecimalField(default=Decimal("100"), verbose_name=_("Weight"), **PERCENT)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    approval_status = models.CharField(
        max_length=20, choices=ApprovalStatus.choices, default=ApprovalStatus.DRAFT
    )
    progress = models.DecimalField(default=Decimal("0"), verbose_name=_("Progress (%)"), **PERCENT)
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        db_table = "okr_objective"
        verbose_name = _("Objective")
        constraints = [
            models.CheckConstraint(condition=_between_0_and_100("weight"), name="okr_objective_weight_0_100"),
            models.CheckConstraint(condition=_between_0_and_100("progress"), name="okr_objective_progress_0_100"),
            models.CheckConstraint(
                condition=(
                    Q(scope=Scope.DEPARTMENT, department__isnull=False, team__isnull=True)
                    | Q(scope=Scope.TEAM, team__isnull=False)
                    | Q(scope__in=[Scope.COMPANY, Scope.PERSONAL], department__isnull=True, team__isnull=True)
                ),
                name="okr_objective_scope_matches_target",
            ),
            models.CheckConstraint(
                condition=Q(parent__isnull=True) | ~Q(parent=F("pk")),
                name="okr_objective_not_own_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "cycle", "scope"], name="okr_obj_company_cycle_scope"),
            models.Index(fields=["company", "owner"], name="okr_obj_company_owner"),
            models.Index(fields=["company", "approval_status"], name="okr_obj_company_status"),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if self._state.adding and self.owner_id is None:
            raise ValidationError({"owner": _("An objective needs an owner.")})

    def company_references(self):
        yield "cycle", self.cycle.company_id if self.cycle_id else None
        yield "parent", self.parent.company_id if self.parent_id else None
        yield "team", self.team.company_id if self.team_id else None

    def employee_references(self):
        yield "owner", self.owner

    def department_references(self):
        yield "department", self.department

    def _on_soft_delete(self):
        self.approval_status = ApprovalStatus.DELETED
        return ["approval_status"]


class OKRKeyResult(SoftDeleteModel, CompanyScopedModel, TimeStampedModel):
    objective = models.ForeignKey(
        OKRObjective, on_delete=models.PROTECT, related_name="key_results", verbose_name=_("Objective")
    )
    owner = models.ForeignKey(
        "employee.Employee",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="okr_key_results_owned",
        verbose_name=_("Owner"),
    )
    title = models.CharField(max_length=255, verbose_name=_("Title"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    unit = models.CharField(max_length=40, default="%", verbose_name=_("Unit"))
    start_value = models.DecimalField(default=Decimal("0"), verbose_name=_("Start value"), **METRIC)
    current_value = models.DecimalField(default=Decimal("0"), verbose_name=_("Current value"), **METRIC)
    target_value = models.DecimalField(default=Decimal("100"), verbose_name=_("Target value"), **METRIC)
    weight = models.DecimalField(default=Decimal("100"), verbose_name=_("Weight"), **PERCENT)
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.MEDIUM)
    due_date = models.DateField(null=True, blank=True, verbose_name=_("Due date"))
    approval_status = models.CharField(
        max_length=20, choices=ApprovalStatus.choices, default=ApprovalStatus.DRAFT
    )
    progress = models.DecimalField(default=Decimal("0"), verbose_name=_("Progress (%)"), **PERCENT)
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        db_table = "okr_key_result"
        verbose_name = _("Key result")
        constraints = [
            models.CheckConstraint(condition=_between_0_and_100("weight"), name="okr_kr_weight_0_100"),
            models.CheckConstraint(condition=_between_0_and_100("progress"), name="okr_kr_progress_0_100"),
        ]
        indexes = [
            models.Index(fields=["company", "objective"], name="okr_kr_company_objective"),
            models.Index(fields=["company", "owner"], name="okr_kr_company_owner"),
            models.Index(fields=["company", "approval_status"], name="okr_kr_company_status"),
            models.Index(fields=["due_date"], name="okr_kr_due_date"),
        ]

    def __str__(self):
        return self.title

    def clean(self):
        super().clean()
        if self._state.adding and self.owner_id is None:
            raise ValidationError({"owner": _("A key result needs an owner.")})

    def inherit_company(self):
        return self.objective.company_id if self.objective_id else None

    def company_references(self):
        yield "objective", self.objective.company_id if self.objective_id else None

    def employee_references(self):
        yield "owner", self.owner

    def _on_soft_delete(self):
        self.approval_status = ApprovalStatus.DELETED
        return ["approval_status"]
