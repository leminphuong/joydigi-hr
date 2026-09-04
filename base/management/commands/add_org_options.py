"""Phase EMPLOYEE-ORG-DATA-UPDATE-1 — add organisation options as data.

These are rows, not schema: ``Department``, ``JobPosition`` and ``Company`` are
ordinary tables the employee form reads its dropdowns from, so nothing here
needs a migration and nothing is hard-coded into a template.

Safe to re-run. Every write goes through ``get_or_create`` keyed on the natural
identity of the row, and the many-to-many company links use ``add()``, which is
a no-op for a link that already exists. Nothing existing is updated or removed.

Lookups deliberately use ``_base_manager``: ``Department`` and ``JobPosition``
are managed by ``JoydigiCompanyManager``, which filters by the *selected*
company. A management command has no request, so the selection is empty and the
filter would be inert — but relying on that would make this script behave
differently the day it is called from inside a request. The base manager sees
every row regardless, which is what a data seeder needs to avoid creating a
duplicate it simply could not see.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from base.models import Company, Department, JobPosition

#: New companies. ``Company`` is unique on (company, address), and ``hq`` stays
#: False so the existing headquarters is left as the only one.
NEW_COMPANIES = [
    {
        "company": "JDG MEDIA",
        "address": "JDG MEDIA",
        "country": "Việt Nam",
        "state": "Thành phố Hồ Chí Minh",
        "city": "Thành phố Hồ Chí Minh",
        "zip": "700000",
        "hq": False,
    },
]

NEW_DEPARTMENTS = ["SEO", "YOUTUBE"]

#: ``JobPosition.department_id`` is NOT NULL, and the employee form filters
#: positions by the chosen department (``/employee/get-job-positions-hx``), so
#: each new position has to name the department it belongs under.
NEW_JOB_POSITIONS = [
    ("Nhân viên SEO", "SEO"),
    ("Editor", "YOUTUBE"),
]


class Command(BaseCommand):
    help = (
        "Add the SEO/YOUTUBE departments, the Nhân viên SEO/Editor job "
        "positions and the JDG MEDIA company. Idempotent; existing data is "
        "never changed or removed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        created = {"companies": [], "departments": [], "positions": [], "links": 0}
        existing = {"companies": [], "departments": [], "positions": []}

        with transaction.atomic():
            for spec in NEW_COMPANIES:
                obj = Company.objects.filter(company=spec["company"]).first()
                if obj is not None:
                    existing["companies"].append(spec["company"])
                elif dry_run:
                    created["companies"].append(spec["company"])
                else:
                    obj = Company.objects.create(**spec)
                    created["companies"].append(spec["company"])

            # Every company, so the new options show up whichever company scope
            # the operator is working in — the existing departments are all
            # linked to the current company, and the employee form reads the
            # dropdown through that link.
            companies = list(Company.objects.all())

            departments = {}
            for name in NEW_DEPARTMENTS:
                obj = Department._base_manager.filter(department=name).first()
                if obj is not None:
                    existing["departments"].append(name)
                elif dry_run:
                    created["departments"].append(name)
                else:
                    obj = Department.objects.create(department=name)
                    created["departments"].append(name)
                departments[name] = obj
                if obj is not None and not dry_run:
                    before = obj.company_id.count()
                    obj.company_id.add(*companies)
                    created["links"] += obj.company_id.count() - before

            for position, department_name in NEW_JOB_POSITIONS:
                department = departments.get(department_name)
                if department is None:
                    department = Department._base_manager.filter(
                        department=department_name
                    ).first()
                if department is None:
                    if dry_run:
                        created["positions"].append(
                            f"{position} (cần phòng ban {department_name})"
                        )
                        continue
                    raise RuntimeError(
                        f"Không tìm thấy phòng ban {department_name!r} cho vị trí "
                        f"{position!r}"
                    )

                obj = JobPosition._base_manager.filter(
                    job_position=position, department_id=department
                ).first()
                if obj is not None:
                    existing["positions"].append(position)
                elif dry_run:
                    created["positions"].append(f"{position} -> {department_name}")
                else:
                    obj = JobPosition.objects.create(
                        job_position=position, department_id=department
                    )
                    created["positions"].append(f"{position} -> {department_name}")
                if obj is not None and not dry_run:
                    before = obj.company_id.count()
                    obj.company_id.add(*companies)
                    created["links"] += obj.company_id.count() - before

            if dry_run:
                transaction.set_rollback(True)

        prefix = "[DRY-RUN] " if dry_run else ""
        for label, key in (
            ("Công ty", "companies"),
            ("Phòng ban", "departments"),
            ("Vị trí công việc", "positions"),
        ):
            if created[key]:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{prefix}Đã thêm {label}: {', '.join(created[key])}"
                    )
                )
            if existing[key]:
                self.stdout.write(
                    f"{prefix}Đã có sẵn, bỏ qua {label}: {', '.join(existing[key])}"
                )
        self.stdout.write(
            self.style.SUCCESS(f"{prefix}Liên kết công ty được thêm: {created['links']}")
        )
