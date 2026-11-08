"""Shared building blocks for OKR models.

Company scoping is explicit. OKR rows carry their own ``company`` instead of
using ``JoydigiCompanyManager``: that manager filters on a request-local
company that is still unset when DRF authenticates a JWT (SEC-04), and it
shows rows with no company to every tenant. Every OKR query therefore has to
name its company, and every save checks that the rows it points at belong to
that same company.

Foreign keys to ``employee.Employee`` follow the contract of
``employee.services.account_deletion``: a ``PROTECT`` reference whose field is
not named ``employee``/``employee_id`` must be nullable, so deleting an
employee in HR detaches it (NULL) instead of refusing every deletion or
destroying other people's OKR history.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class AppendOnlyError(Exception):
    """Raised when code tries to edit or delete history that must stay as written."""


def employee_company_id(employee):
    """Company of an employee, read from work info without tenant filtering."""
    if employee is None:
        return None
    from employee.models import EmployeeWorkInformation

    return (
        EmployeeWorkInformation._base_manager.filter(employee_id=employee.pk)
        .values_list("company_id", flat=True)
        .first()
    )


def company_department_filter(company_id):
    """``Q`` on ``base.Department`` for the departments a company uses in OKR.

    A department belongs to the company when it is linked to it in HR, linked
    to no company (HR treats those as shared), or is the department of at
    least one active employee of the company. The last rule matters because
    HR's web assigns a new department to the company picked in the session
    (or to none, or to several), so a department can be linked to one company
    record while the people in it work for another; without it OKR hid such
    departments from its filters while the check-in tree still grouped those
    people under them (2026-11-08, app filter showing only "ADS"). A
    department only another company uses stays out: nothing crosses
    companies."""
    from django.db.models import Q

    from employee.models import EmployeeWorkInformation

    staffed = EmployeeWorkInformation._base_manager.filter(
        company_id=company_id, employee_id__is_active=True, department_id__isnull=False
    ).values("department_id")
    return Q(company_id=company_id) | Q(company_id__isnull=True) | Q(pk__in=staffed)


def department_allows_company(department, company_id) -> bool:
    """Whether ``department`` is one of the company's OKR departments
    (``company_department_filter``)."""
    if department is None or company_id is None:
        return False
    return type(department)._base_manager.filter(company_department_filter(company_id), pk=department.pk).exists()


class OKRQuerySet(models.QuerySet):
    def for_company(self, company):
        """The only supported way to list OKR rows: always for one company."""
        if company is None:
            raise ValueError("OKR queries must be scoped to a company.")
        return self.filter(company=company)


class SoftDeleteQuerySet(OKRQuerySet):
    def alive(self):
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        return self.filter(deleted_at__isnull=False)


class AliveManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager for soft-deletable models: hides deleted rows.

    Forward foreign keys use Django's plain base manager, so a check-in still
    resolves its key result after that key result is soft-deleted.
    """

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class AppendOnlyQuerySet(OKRQuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError(f"{self.model._meta.label} rows cannot be edited.")

    def delete(self):
        raise AppendOnlyError(f"{self.model._meta.label} rows cannot be deleted.")


class CompanyScopedModel(models.Model):
    """Abstract base: a mandatory company plus cross-company integrity checks."""

    company = models.ForeignKey(
        "base.Company",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("Company"),
    )

    class Meta:
        abstract = True

    def inherit_company(self):
        """Fill ``company`` from the parent row when the caller left it empty."""
        return None

    def company_references(self):
        """Yield ``(field_name, company_id)`` for every scoped row this one points at."""
        return ()

    def employee_references(self):
        """Yield ``(field_name, employee)`` for every employee this row names."""
        return ()

    def department_references(self):
        """Yield ``(field_name, department)`` for every HR department this row names."""
        return ()

    def validate_company_scope(self):
        if self.company_id is None:
            inherited = self.inherit_company()
            if inherited is not None:
                self.company_id = inherited
        if self.company_id is None:
            raise ValidationError({"company": _("A company is required.")})
        errors = {}
        for field, company_id in self.company_references():
            if company_id is not None and company_id != self.company_id:
                errors[field] = _("Belongs to a different company.")
        for field, employee in self.employee_references():
            if employee is not None and employee_company_id(employee) != self.company_id:
                errors[field] = _("Employee does not work for this company.")
        for field, department in self.department_references():
            if department is not None and not department_allows_company(
                department, self.company_id
            ):
                errors[field] = _("Department is not linked to this company.")
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        self.validate_company_scope()

    def save(self, *args, **kwargs):
        self.validate_company_scope()
        super().save(*args, **kwargs)


class TimeStampedModel(models.Model):
    # default rather than auto_now_add, so the legacy import can keep the
    # original JOYOKR creation time.
    created_at = models.DateTimeField(default=timezone.now, verbose_name=_("Created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("Updated at"))

    class Meta:
        abstract = True


class SoftDeleteModel(models.Model):
    deleted_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Deleted at"))

    objects = AliveManager()
    all_objects = OKRQuerySet.as_manager()

    class Meta:
        abstract = True

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self):
        if self.deleted_at is None:
            self.deleted_at = timezone.now()
            extra = self._on_soft_delete()
            self.save(update_fields=["deleted_at", "updated_at", *extra])

    def _on_soft_delete(self):
        """Hook for models that mark more than ``deleted_at``; returns the
        extra field names it changed."""
        return []


class AppendOnlyModel(models.Model):
    """History rows (check-ins, activity) are written once and never changed.

    The default manager refuses bulk ``update``/``delete``. HR's employee
    deletion detaches references through Django's plain base manager, which
    is the one sanctioned way these rows change after insert.
    """

    created_at = models.DateTimeField(default=timezone.now, verbose_name=_("Created at"))

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError(f"{self._meta.label} rows cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{self._meta.label} rows cannot be deleted.")
