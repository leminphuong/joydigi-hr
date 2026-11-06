"""Enumerations shared by the OKR models.

Values match the strings stored by JOYOKR v1.6.45 so legacy rows can be
imported without translation tables.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CycleType(models.TextChoices):
    MONTH = "month", _("Month")
    QUARTER = "quarter", _("Quarter")
    YEAR = "year", _("Year")
    CUSTOM = "custom", _("Custom")


class CycleStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ACTIVE = "active", _("Active")
    CLOSED = "closed", _("Closed")


class Scope(models.TextChoices):
    COMPANY = "company", _("Company")
    DEPARTMENT = "department", _("Department")
    TEAM = "team", _("Team")
    PERSONAL = "personal", _("Personal")


class Priority(models.TextChoices):
    LOW = "low", _("Low")
    MEDIUM = "medium", _("Medium")
    HIGH = "high", _("High")


class ApprovalStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    SUBMITTED = "submitted", _("Submitted")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")
    # JOYOKR marks soft-deleted rows with this status as well as deleted_at.
    DELETED = "deleted", _("Deleted")


class RecordStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    INACTIVE = "inactive", _("Inactive")


class DepartmentRoleType(models.TextChoices):
    MANAGER = "manager", _("OKR department manager")


class OKRRoleType(models.TextChoices):
    """JOYOKR's WordPress roles beyond ``joyokr_employee``."""

    MANAGER = "manager", _("OKR manager")
    ADMIN = "admin", _("OKR administrator")


class RoleEventRole(models.TextChoices):
    """What an ``OKRRoleEvent`` changed: a company role, or the OKR manager of a department."""

    MANAGER = "manager", _("OKR manager")
    ADMIN = "admin", _("OKR administrator")
    DEPARTMENT_MANAGER = "department_manager", _("OKR department manager")


class RoleEventAction(models.TextChoices):
    GRANTED = "granted", _("Granted")
    REVOKED = "revoked", _("Revoked")


class ReferenceType(models.TextChoices):
    OBJECTIVE = "objective", _("Objective")
    KEY_RESULT = "key_result", _("Key result")


class EvidenceType(models.TextChoices):
    URL = "url", _("Link")
    FILE = "file", _("File")


class ReminderFrequency(models.TextChoices):
    WEEKLY = "weekly", _("Weekly")
    BIWEEKLY = "biweekly", _("Every two weeks")
    MONTHLY = "monthly", _("Monthly")


class ActivityAction(models.TextChoices):
    CREATED = "created", _("Created")
    UPDATED = "updated", _("Updated")
    DELETED = "deleted", _("Deleted")
    SUBMITTED = "submitted", _("Submitted")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")
    CHECKED_IN = "checked_in", _("Checked in")
    COMMENTED = "commented", _("Commented")
    EVIDENCE_ADDED = "evidence_added", _("Evidence added")
    CYCLE_STATUS_CHANGED = "cycle_status_changed", _("Cycle status changed")
    CYCLE_CLONED = "cycle_cloned", _("Cycle cloned")


class IdentityMatch(models.TextChoices):
    EMAIL_AND_BADGE = "email_and_badge", _("Email and employee code")
    EMAIL = "email", _("Email only")
    BADGE = "badge", _("Employee code only")
    MANUAL = "manual", _("Manual")


class IdentityStatus(models.TextChoices):
    PROPOSED = "proposed", _("Proposed")
    VERIFIED = "verified", _("Verified")
    REJECTED = "rejected", _("Rejected")
    UNMATCHED = "unmatched", _("Unmatched")


class BatchMode(models.TextChoices):
    DRY_RUN = "dry_run", _("Dry run")
    APPLY = "apply", _("Apply")
    ROLLBACK = "rollback", _("Rollback")


class BatchStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    RUNNING = "running", _("Running")
    SUCCEEDED = "succeeded", _("Succeeded")
    FAILED = "failed", _("Failed")
    ROLLED_BACK = "rolled_back", _("Rolled back")
