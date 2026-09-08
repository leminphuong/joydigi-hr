"""
backfill_available_leave

Opens the leave balances that should already exist but don't.

Auto-assignment (``leave/signals.py``) only covers rows created from now on —
employees and leave types that predate it can still be missing each other. This
command closes that gap for the data already in the database, applying exactly
the same rules: the same company match, the same leave-type conditions, and the
same balance seeding, because it calls the same
``leave.services.ensure_available_leave`` the signals and the manual "Assign
Leave" screens use.

It only ever *creates*. An existing ``AvailableLeave`` is left completely
alone — its ``available_days``, ``carryforward_days`` and ``total_leave_days``
are never read, recomputed or overwritten — so running this can not disturb a
balance an employee has already spent against.

Idempotent: a second run creates nothing.

    python manage.py backfill_available_leave --dry-run
    python manage.py backfill_available_leave
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from employee.models import Employee
from leave.models import AvailableLeave, LeaveType
from leave.services import (
    ensure_available_leave,
    evaluate_leave_type_conditions,
    leave_type_matches_company,
)


class Command(BaseCommand):
    help = "Create the AvailableLeave rows that are missing for existing employees."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what is missing without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        employees = list(
            Employee.objects.filter(is_active=True).select_related(
                "employee_work_info__company_id"
            )
        )
        leave_types = list(LeaveType.objects.filter(is_active=True))

        existing_pairs = set(
            AvailableLeave.objects.values_list("employee_id_id", "leave_type_id_id")
        )

        self.stdout.write("Backfill AvailableLeave")
        self.stdout.write(f"  active employees   : {len(employees)}")
        self.stdout.write(f"  active leave types : {len(leave_types)}")
        self.stdout.write(f"  existing rows      : {len(existing_pairs)}")

        missing = []
        skipped_company = 0
        skipped_condition = 0

        for employee in employees:
            for leave_type in leave_types:
                if (employee.pk, leave_type.pk) in existing_pairs:
                    continue
                if not leave_type_matches_company(leave_type, employee):
                    skipped_company += 1
                    continue
                eligible, _reason = evaluate_leave_type_conditions(leave_type, employee)
                if not eligible:
                    skipped_condition += 1
                    continue
                missing.append((employee, leave_type))

        self.stdout.write(f"  missing pairs      : {len(missing)}")
        self.stdout.write(f"  skipped (company)  : {skipped_company}")
        self.stdout.write(f"  skipped (condition): {skipped_condition}")

        if dry_run:
            for employee, leave_type in missing:
                self.stdout.write(
                    f"    WOULD CREATE  employee={employee.pk} "
                    f"leave_type={leave_type.pk} ({leave_type.name})"
                )
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written."))
            return

        created = 0
        errors = 0
        with transaction.atomic():
            for employee, leave_type in missing:
                try:
                    _row, was_created = ensure_available_leave(employee, leave_type)
                    if was_created:
                        created += 1
                except Exception as exc:  # pragma: no cover - reported, not raised
                    errors += 1
                    self.stderr.write(
                        f"    ERROR employee={employee.pk} "
                        f"leave_type={leave_type.pk}: {exc}"
                    )

        self.stdout.write(f"  CREATED            : {created}")
        self.stdout.write(f"  SKIPPED EXISTING   : {len(existing_pairs)}")
        self.stdout.write(f"  ERRORS             : {errors}")
        self.stdout.write(self.style.SUCCESS("Backfill complete."))
