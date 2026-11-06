"""Check-ins, comments, evidence and the activity timeline."""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from okr.models.base import (
    AppendOnlyModel,
    CompanyScopedModel,
    OKRQuerySet,
    SoftDeleteModel,
    TimeStampedModel,
)
from okr.models.choices import ActivityAction, EvidenceType, ReferenceType
from okr.models.objective import METRIC, PERCENT
from okr.storage import evidence_storage, evidence_upload_to


class OKRCheckin(AppendOnlyModel, CompanyScopedModel):
    """One progress update. Written once; later updates are new rows."""

    key_result = models.ForeignKey(
        "okr.OKRKeyResult", on_delete=models.PROTECT, related_name="checkins", verbose_name=_("Key result")
    )
    author = models.ForeignKey(
        "employee.Employee",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="okr_checkins",
        verbose_name=_("Author"),
    )
    previous_value = models.DecimalField(verbose_name=_("Previous value"), **METRIC)
    current_value = models.DecimalField(verbose_name=_("Current value"), **METRIC)
    previous_progress = models.DecimalField(verbose_name=_("Previous progress (%)"), **PERCENT)
    current_progress = models.DecimalField(verbose_name=_("Current progress (%)"), **PERCENT)
    confidence = models.PositiveSmallIntegerField(default=3, verbose_name=_("Confidence (1-5)"))
    update_text = models.TextField(verbose_name=_("Update"))
    blockers = models.TextField(blank=True, verbose_name=_("Blockers"))
    next_steps = models.TextField(blank=True, verbose_name=_("Next steps"))
    # Chosen by the app per submission; a retry with the same value returns
    # the first check-in instead of recording a second one.
    client_request_id = models.CharField(max_length=64, null=True, blank=True)
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        db_table = "okr_checkin"
        verbose_name = _("Check-in")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(confidence__gte=1) & Q(confidence__lte=5), name="okr_checkin_confidence_1_5"
            ),
            models.CheckConstraint(
                condition=Q(previous_progress__gte=0) & Q(previous_progress__lte=100)
                & Q(current_progress__gte=0) & Q(current_progress__lte=100),
                name="okr_checkin_progress_0_100",
            ),
            models.UniqueConstraint(
                fields=["author", "client_request_id"],
                condition=Q(client_request_id__isnull=False),
                name="okr_checkin_client_request_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["key_result", "created_at"], name="okr_checkin_kr_created"),
            models.Index(fields=["company", "created_at"], name="okr_checkin_company_created"),
        ]

    def inherit_company(self):
        return self.key_result.company_id if self.key_result_id else None

    def company_references(self):
        yield "key_result", self.key_result.company_id if self.key_result_id else None

    def employee_references(self):
        yield "author", self.author


class OKRComment(SoftDeleteModel, CompanyScopedModel, TimeStampedModel):
    """Comment on exactly one objective or one key result (JOYOKR's
    polymorphic ``object_type``/``object_id`` split into two real FKs)."""

    objective = models.ForeignKey(
        "okr.OKRObjective",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="comments",
        verbose_name=_("Objective"),
    )
    key_result = models.ForeignKey(
        "okr.OKRKeyResult",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="comments",
        verbose_name=_("Key result"),
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="replies",
        verbose_name=_("Reply to"),
    )
    author = models.ForeignKey(
        "employee.Employee",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="okr_comments",
        verbose_name=_("Author"),
    )
    content = models.TextField(verbose_name=_("Content"))
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        db_table = "okr_comment"
        verbose_name = _("OKR comment")
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(objective__isnull=False, key_result__isnull=True)
                | Q(objective__isnull=True, key_result__isnull=False),
                name="okr_comment_one_target",
            ),
        ]
        indexes = [
            models.Index(fields=["objective", "created_at"], name="okr_comment_obj_created"),
            models.Index(fields=["key_result", "created_at"], name="okr_comment_kr_created"),
        ]

    def inherit_company(self):
        target = self.objective if self.objective_id else self.key_result if self.key_result_id else None
        return target.company_id if target is not None else None

    def company_references(self):
        yield "objective", self.objective.company_id if self.objective_id else None
        yield "key_result", self.key_result.company_id if self.key_result_id else None
        yield "parent", self.parent.company_id if self.parent_id else None

    def employee_references(self):
        yield "author", self.author


class OKREvidence(CompanyScopedModel, TimeStampedModel):
    key_result = models.ForeignKey(
        "okr.OKRKeyResult", on_delete=models.PROTECT, related_name="evidence", verbose_name=_("Key result")
    )
    checkin = models.ForeignKey(
        OKRCheckin,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="evidence",
        verbose_name=_("Check-in"),
    )
    author = models.ForeignKey(
        "employee.Employee",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="okr_evidence",
        verbose_name=_("Author"),
    )
    evidence_type = models.CharField(max_length=20, choices=EvidenceType.choices)
    file = models.FileField(
        storage=evidence_storage,
        upload_to=evidence_upload_to,
        max_length=255,
        blank=True,
        verbose_name=_("File"),
    )
    original_name = models.CharField(max_length=255, blank=True, verbose_name=_("Original file name"))
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    url = models.URLField(max_length=2000, blank=True, verbose_name=_("Link"))
    label = models.CharField(max_length=255, verbose_name=_("Label"))
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    legacy_attachment_id = models.PositiveBigIntegerField(null=True, blank=True)

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_evidence"
        verbose_name = _("Evidence")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(evidence_type=EvidenceType.FILE) & ~Q(file=""))
                | (Q(evidence_type=EvidenceType.URL) & ~Q(url="")),
                name="okr_evidence_has_payload",
            ),
        ]
        indexes = [models.Index(fields=["key_result", "created_at"], name="okr_evidence_kr_created")]

    def __str__(self):
        return self.label

    def clean(self):
        super().clean()
        if self.checkin_id and self.key_result_id and self.checkin.key_result_id != self.key_result_id:
            raise ValidationError({"checkin": _("Check-in belongs to another key result.")})

    def inherit_company(self):
        return self.key_result.company_id if self.key_result_id else None

    def company_references(self):
        yield "key_result", self.key_result.company_id if self.key_result_id else None
        yield "checkin", self.checkin.company_id if self.checkin_id else None

    def employee_references(self):
        yield "author", self.author


class OKRActivity(AppendOnlyModel, CompanyScopedModel):
    """Business timeline shown next to an objective, key result or cycle."""

    actor = models.ForeignKey(
        "employee.Employee",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="okr_activities",
        verbose_name=_("Actor"),
    )
    action = models.CharField(max_length=60, choices=ActivityAction.choices)
    cycle = models.ForeignKey(
        "okr.OKRCycle", on_delete=models.PROTECT, null=True, blank=True, related_name="activities"
    )
    objective = models.ForeignKey(
        "okr.OKRObjective", on_delete=models.PROTECT, null=True, blank=True, related_name="activities"
    )
    key_result = models.ForeignKey(
        "okr.OKRKeyResult", on_delete=models.PROTECT, null=True, blank=True, related_name="activities"
    )
    metadata = models.JSONField(default=dict, blank=True)
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        db_table = "okr_activity"
        verbose_name = _("OKR activity")
        verbose_name_plural = _("OKR activity")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(cycle__isnull=False) | Q(objective__isnull=False) | Q(key_result__isnull=False),
                name="okr_activity_has_target",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "created_at"], name="okr_activity_company_created"),
            models.Index(fields=["objective", "created_at"], name="okr_activity_obj_created"),
            models.Index(fields=["key_result", "created_at"], name="okr_activity_kr_created"),
        ]

    def inherit_company(self):
        for target in (self.key_result, self.objective, self.cycle):
            if target is not None:
                return target.company_id
        return None

    def company_references(self):
        yield "cycle", self.cycle.company_id if self.cycle_id else None
        yield "objective", self.objective.company_id if self.objective_id else None
        yield "key_result", self.key_result.company_id if self.key_result_id else None

    def employee_references(self):
        yield "actor", self.actor


class OKRNotification(CompanyScopedModel):
    """In-app OKR notification (JOYOKR ``joyokr_notifications``).

    The reference is a plain type + id rather than a foreign key so a
    notification survives its objective or key result being deleted; the
    API then reports it as no longer available.
    """

    recipient = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        related_name="okr_notifications",
        verbose_name=_("Recipient"),
    )
    notification_type = models.CharField(max_length=40, verbose_name=_("Type"))
    # F7: who caused it (submitter, reviewer, commenter…). JOYOKR stored no
    # actor, so migrated rows and system reminders have none.
    actor = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Actor"),
    )
    title = models.CharField(max_length=255, verbose_name=_("Title"))
    message = models.TextField(blank=True, verbose_name=_("Message"))
    reference_type = models.CharField(max_length=20, choices=ReferenceType.choices, blank=True)
    reference_id = models.PositiveBigIntegerField(null=True, blank=True)
    # Same notification is never stored twice for one key (JOYOKR dedupe_key).
    dedupe_key = models.CharField(max_length=190, null=True, blank=True)
    is_read = models.BooleanField(default=False, verbose_name=_("Read"))
    created_at = models.DateTimeField(default=timezone.now, verbose_name=_("Created at"))
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_notification"
        verbose_name = _("OKR notification")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "dedupe_key"],
                condition=Q(dedupe_key__isnull=False),
                name="okr_notification_dedupe_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["recipient", "is_read"], name="okr_notif_recipient_read"),
            models.Index(fields=["reference_type", "reference_id"], name="okr_notif_reference"),
        ]

    def __str__(self):
        return self.title

    def employee_references(self):
        yield "recipient", self.recipient
        yield "actor", self.actor
