from django.db import models
from django.db.models import F, Q
from django.utils.translation import gettext_lazy as _

from okr.models.base import CompanyScopedModel, OKRQuerySet, TimeStampedModel
from okr.models.choices import CycleStatus, CycleType


class OKRCycle(CompanyScopedModel, TimeStampedModel):
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
        verbose_name=_("Parent cycle"),
    )
    name = models.CharField(max_length=190, verbose_name=_("Name"))
    cycle_type = models.CharField(
        max_length=20, choices=CycleType.choices, default=CycleType.CUSTOM
    )
    start_date = models.DateField(verbose_name=_("Start date"))
    end_date = models.DateField(verbose_name=_("End date"))
    status = models.CharField(
        max_length=20, choices=CycleStatus.choices, default=CycleStatus.DRAFT
    )
    legacy_wp_id = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_cycle"
        verbose_name = _("OKR cycle")
        ordering = ["-start_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")), name="okr_cycle_end_after_start"
            ),
            models.CheckConstraint(
                condition=Q(parent__isnull=True) | ~Q(parent=F("pk")),
                name="okr_cycle_not_own_parent",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status"], name="okr_cycle_company_status"),
            models.Index(
                fields=["company", "start_date", "end_date"], name="okr_cycle_company_dates"
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_read_only(self) -> bool:
        return self.status == CycleStatus.CLOSED

    def company_references(self):
        yield "parent", self.parent.company_id if self.parent_id else None
