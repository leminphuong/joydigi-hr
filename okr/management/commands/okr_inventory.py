"""Read-only count of the OKR data already in this database.

    manage.py okr_inventory            # every company
    manage.py okr_inventory --json     # machine-readable

Used before a JOYOKR import to know whether Django already holds OKR rows
that must be kept (rows without ``legacy_wp_id`` were created in Django).
It only runs SELECT queries; it prints counts, never names or contents.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from okr.models import (
    OKRActivity,
    OKRCheckin,
    OKRComment,
    OKRCycle,
    OKRDepartmentRole,
    OKREvidence,
    OKRIdentityMap,
    OKRKeyResult,
    OKRMigrationBatch,
    OKRNotification,
    OKRObjective,
    OKRRole,
    OKRTeam,
)

LEGACY_MODELS = (
    ("cycles", OKRCycle),
    ("teams", OKRTeam),
    ("objectives", OKRObjective),
    ("key_results", OKRKeyResult),
    ("checkins", OKRCheckin),
    ("comments", OKRComment),
    ("evidence", OKREvidence),
    ("notifications", OKRNotification),
    ("activity", OKRActivity),
)


class Command(BaseCommand):
    help = "Count OKR rows per company (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        data = {"companies": {}, "roles": {}, "department_roles": 0, "identity_map": {}, "batches": []}
        for name, model in LEGACY_MODELS:
            rows = (
                model._base_manager.values("company_id")
                .annotate(total=Count("id"), legacy=Count("id", filter=Q(legacy_wp_id__isnull=False)))
                .order_by("company_id")
            )
            for row in rows:
                company = data["companies"].setdefault(str(row["company_id"]), {})
                company[name] = {"total": row["total"], "from_wordpress": row["legacy"],
                                 "created_in_django": row["total"] - row["legacy"]}
        for row in OKRRole.objects.values("company_id", "role").annotate(n=Count("id")).order_by():
            data["roles"].setdefault(str(row["company_id"]), {})[row["role"]] = row["n"]
        data["department_roles"] = OKRDepartmentRole.objects.count()
        for row in OKRIdentityMap.objects.values("status").annotate(n=Count("id")).order_by():
            data["identity_map"][row["status"]] = row["n"]
        for batch in OKRMigrationBatch.objects.order_by("-id")[:10]:
            data["batches"].append({"id": batch.pk, "mode": batch.mode, "status": batch.status,
                                    "started_at": batch.started_at.isoformat() if batch.started_at else None})
        if options["json"]:
            self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
            return
        if not data["companies"]:
            self.stdout.write("No OKR rows in any company.")
        for company, tables in sorted(data["companies"].items()):
            self.stdout.write(f"Company {company}:")
            for name, counts in tables.items():
                self.stdout.write(f"  {name:<14} total={counts['total']} from_wordpress={counts['from_wordpress']} "
                                  f"created_in_django={counts['created_in_django']}")
        self.stdout.write(f"Roles: {data['roles'] or 'none'}; department roles: {data['department_roles']}")
        self.stdout.write(f"Identity map: {data['identity_map'] or 'empty'}; batches: {len(data['batches'])}")
