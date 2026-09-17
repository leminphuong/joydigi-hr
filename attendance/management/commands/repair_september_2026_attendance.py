"""Safely preview or apply the September 2026 attendance repair."""

from django.core.management.base import BaseCommand, CommandError

from attendance.methods.september_2026_repair import repair_september_2026


class Command(BaseCommand):
    help = (
        "Preview or apply the company-wide September 2026 attendance repair "
        "and the 2027 National Day holiday correction."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the reviewed repair to the database (default: dry run).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        report = repair_september_2026(apply=apply)

        heading = "APPLY" if apply else "DRY RUN — no data was modified"
        self.stdout.write(self.style.MIGRATE_HEADING(heading))
        self.stdout.write(
            f"Quốc khánh 2027: create={report.holidays_created}, "
            f"update={report.holidays_updated}"
        )
        self.stdout.write(
            f"Full days: credit={report.full_days_credited}, "
            f"already correct={report.full_days_unchanged}, "
            f"skipped approved leave={report.full_days_skipped_leave}, "
            f"skipped non-working={report.full_days_skipped_non_working}"
        )
        if report.schedule_conflicts:
            self.stdout.write(
                self.style.WARNING("Missing work schedules (left unchanged):")
            )
            for conflict in report.schedule_conflicts:
                self.stdout.write(
                    f"  employee={conflict['employee_id']} "
                    f"badge={conflict['badge_id'] or '-'} "
                    f"date={conflict['date']} reason={conflict['reason']}"
                )
        self.stdout.write(
            f"OT requests approved for 05/09 and 12/09: "
            f"{report.ot_requests_approved}"
        )
        self.stdout.write(
            f"Attendance rows moved 14/09 -> 19/09: "
            f"{report.attendance_rows_moved}"
        )

        if report.move_conflicts:
            self.stdout.write(self.style.WARNING("Move conflicts (left unchanged):"))
            for conflict in report.move_conflicts:
                self.stdout.write(
                    f"  employee={conflict['employee_id']} "
                    f"badge={conflict['badge_id'] or '-'} "
                    f"reason={conflict['reason']}"
                )

        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "Attendance below 5 hours without a permission request "
                f"({len(report.short_attendance)})"
            )
        )
        if not report.short_attendance:
            self.stdout.write("  None")
        for row in report.short_attendance:
            self.stdout.write(
                f"  attendance={row['attendance_id']} "
                f"badge={row['badge_id'] or '-'} "
                f"employee={row['employee_name']} date={row['date']} "
                f"worked={row['worked_hours']} "
                f"in={row['clock_in'] or '-'} out={row['clock_out'] or '-'}"
            )

        if apply and report.move_conflicts:
            raise CommandError(
                "Repair applied partially; move conflicts were left unchanged."
            )
        if apply:
            self.stdout.write(self.style.SUCCESS("Repair applied successfully."))
