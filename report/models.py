from django.db import models

from base.joydigi_company_manager import JoydigiCompanyManager
from base.models import Company
from joydigi import joydigi_middlewares
from joydigi.models import JoydigiModel


class ReportTemplate(JoydigiModel):
    """
    A saved field arrangement (Rows/Columns/renderer/aggregator) for a
    report's pivot table, so an employee can reload their own preferred
    layout later instead of rebuilding it from scratch each time.
    """

    report_slug = models.CharField(max_length=100, verbose_name="Report")
    name = models.CharField(max_length=100, verbose_name="Template Name")
    config = models.JSONField(verbose_name="Field Arrangement")
    company_id = models.ForeignKey(
        Company, null=True, editable=False, on_delete=models.PROTECT
    )

    objects = JoydigiCompanyManager(related_company_field="company_id")

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("report_slug", "name", "created_by", "company_id")

    def __str__(self):
        return f"{self.name} ({self.report_slug})"

    def save(self, *args, **kwargs):
        request = getattr(joydigi_middlewares._thread_locals, "request", None)
        selected_company = request.session.get("selected_company") if request else None
        if (
            not self.id
            and not self.company_id
            and selected_company
            and selected_company != "all"
        ):
            self.company_id = Company.find(selected_company)
        super().save(*args, **kwargs)
