"""Phase ATT-TIME-3A — DRY-RUN ONLY report on historical attendance times.

This command is read-only by construction: it has no `--apply`,
`--fix` or `--write` option, and the audit layer it calls
(`attendance.methods.attendance_time_audit`) performs no database
writes. Repairing the data is a separate, later phase.

Usage:

    python manage.py audit_attendance_times
    python manage.py audit_attendance_times --limit 500 --samples 50
"""

from django.core.management.base import BaseCommand

from attendance.methods.attendance_time_audit import (
    AMBIGUOUS,
    EXACT_90_MIN_ERROR,
    NO_ACTIVITY,
    OTHER_DIFFERENCE,
    audit_queryset,
)


class Command(BaseCommand):
    help = (
        "DRY-RUN: report historical Attendance rows whose stored wall clock "
        "disagrees with the true instant kept on AttendanceActivity. "
        "Writes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Audit at most this many of the most recent rows.",
        )
        parser.add_argument(
            "--samples",
            type=int,
            default=20,
            help="How many example rows to print (default 20).",
        )

    def handle(self, *args, **options):
        results, summary = audit_queryset(limit=options["limit"])

        self.stdout.write(self.style.MIGRATE_HEADING("DRY RUN — no data is modified"))
        self.stdout.write("")
        for label, key in (
            ("Total Attendance inspected", "total"),
            ("SAFE matches", "safe"),
            ("Ambiguous", "ambiguous"),
            ("No activity", "no_activity"),
            ("Already correct", "already_correct"),
            ("Exact 90-minute errors", "exact_90_min_error"),
            ("Other differences", "other_difference"),
            ("Missing value", "missing_value"),
        ):
            self.stdout.write(f"{label:<30}: {summary[key]}")

        interesting = [
            r
            for r in results
            if EXACT_90_MIN_ERROR
            in {r["clock_in_status"], r["clock_out_status"]}
            or OTHER_DIFFERENCE in {r["clock_in_status"], r["clock_out_status"]}
            or r["match"] in (AMBIGUOUS, NO_ACTIVITY)
        ]

        samples = interesting[: options["samples"]]
        if not samples:
            self.stdout.write("")
            self.stdout.write(self.style.SUCCESS("No discrepancies found."))
            return

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"Samples ({len(samples)})"))
        self.stdout.write(
            f"{'id':>6} {'emp':>5} {'date':<11} "
            f"{'in':<9}->{'exp_in':<9}{'d':>6}  "
            f"{'out':<9}->{'exp_out':<9}{'d':>6}  match"
        )
        for r in samples:
            self.stdout.write(
                f"{r['attendance_id']:>6} {str(r['employee_id']):>5} "
                f"{str(r['attendance_date']):<11} "
                f"{str(r['current_clock_in'] or '-'):<9}->"
                f"{str(r['expected_clock_in'] or '-'):<9}"
                f"{str(r['clock_in_difference_minutes'] if r['clock_in_difference_minutes'] is not None else '-'):>6}  "
                f"{str(r['current_clock_out'] or '-'):<9}->"
                f"{str(r['expected_clock_out'] or '-'):<9}"
                f"{str(r['clock_out_difference_minutes'] if r['clock_out_difference_minutes'] is not None else '-'):>6}  "
                f"{r['match']}"
            )

        self.stdout.write("")
        self.stdout.write(
            "Nothing was changed. Repair is a separate phase; ambiguous rows "
            "must be resolved by hand rather than assumed."
        )
