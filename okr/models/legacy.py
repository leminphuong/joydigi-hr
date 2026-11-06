"""Bookkeeping for the future JOYOKR → Django data migration.

Nothing here is read by the OKR features themselves; these tables record
how WordPress users were matched to employees and what each import run did,
so a run can be reconciled and rolled back.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from okr.models.base import TimeStampedModel, employee_company_id
from okr.models.choices import BatchMode, BatchStatus, IdentityMatch, IdentityStatus


class OKRMigrationBatch(TimeStampedModel):
    source = models.CharField(max_length=100, default="joyokr", verbose_name=_("Source"))
    source_version = models.CharField(max_length=40, blank=True, verbose_name=_("Source version"))
    source_checksum = models.CharField(max_length=64, blank=True, verbose_name=_("Source SHA-256"))
    mode = models.CharField(max_length=20, choices=BatchMode.choices, default=BatchMode.DRY_RUN)
    status = models.CharField(max_length=20, choices=BatchStatus.choices, default=BatchStatus.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    stats = models.JSONField(default=dict, blank=True)
    reconciliation = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "okr_migration_batch"
        verbose_name = _("OKR migration batch")
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.source} {self.mode} #{self.pk}"


class OKRIdentityMap(TimeStampedModel):
    """WordPress user → HR employee, with who confirmed the match.

    Matching never uses display names or e-mail aliases (the JOYOKR Workspace
    SSO did, see J-04). A row only counts once it is ``verified``.
    """

    wp_user_id = models.PositiveBigIntegerField(unique=True, verbose_name=_("WordPress user ID"))
    wp_email = models.EmailField(blank=True, verbose_name=_("WordPress e-mail"))
    wp_employee_code = models.CharField(max_length=64, blank=True, verbose_name=_("JOYOKR employee code"))
    # Named ``employee`` + SET_NULL: HR deletion clears the link, keeps the row.
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="okr_identity_maps",
        verbose_name=_("Employee"),
    )
    company = models.ForeignKey(
        "base.Company", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    matched_by = models.CharField(max_length=20, choices=IdentityMatch.choices, blank=True)
    status = models.CharField(
        max_length=20, choices=IdentityStatus.choices, default=IdentityStatus.PROPOSED
    )
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    batch = models.ForeignKey(
        OKRMigrationBatch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="identity_maps",
    )
    notes = models.TextField(blank=True)

    class Meta:
        db_table = "okr_identity_map"
        verbose_name = _("OKR identity mapping")
        indexes = [models.Index(fields=["status"], name="okr_identity_status")]

    def __str__(self):
        return f"wp:{self.wp_user_id} → {self.employee_id or '-'} ({self.status})"

    def clean(self):
        super().clean()
        errors = {}
        if self.status == IdentityStatus.VERIFIED:
            if self.employee_id is None:
                errors["employee"] = _("A verified mapping needs an employee.")
            if self.verified_at is None:
                errors["verified_at"] = _("A verified mapping needs a verification time.")
        if self.employee_id and self.company_id:
            if employee_company_id(self.employee) != self.company_id:
                errors["company"] = _("Employee does not work for this company.")
        if errors:
            raise ValidationError(errors)
