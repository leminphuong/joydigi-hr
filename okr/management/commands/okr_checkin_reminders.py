"""Send in-app reminders for overdue OKR check-ins (JOYOKR's reminder cron).

Not scheduled anywhere yet; run it from cron or a task runner once that is
decided. Safe to rerun: a reminder is stored once per key result and
reminder interval.
"""

from django.core.management.base import BaseCommand

from okr.models import OKRCycle
from okr.models.choices import CycleStatus
from okr.services.notifications import send_checkin_reminders


class Command(BaseCommand):
    help = "Send in-app reminders for overdue OKR check-ins in every company with an active cycle."

    def handle(self, *args, **options):
        company_ids = (
            OKRCycle.objects.filter(status=CycleStatus.ACTIVE).values_list("company_id", flat=True).distinct()
        )
        for company_id in company_ids:
            summary = send_checkin_reminders(company_id)
            self.stdout.write(f"company {company_id}: checked {summary['checked']}, sent {summary['sent']}")
