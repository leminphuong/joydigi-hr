"""
accrue_shared_leave — credit one month of the shared annual fund.

2026-11-11. JOYDIGI policy: 1 day per month, at most 12 days for the year.
Run by hand (or, once the policy is confirmed, from a schedule) for one month:

    python manage.py accrue_shared_leave --dry-run --pool 7 --company 1 --period 2026-12
    python manage.py accrue_shared_leave --pool 7 --company 1 --period 2026-12 --expect-count 11

Never credits a month twice (each credit leaves "[tích lũy 2026-12] +1" in the
fund row's history), never past ``--cap`` for the year, never an employee
without a fund row, one who joined after that month, or one without a joining
date unless ``--include-missing-joining-date`` (an HR decision — no date is
ever made up). All or nothing.
"""

from django.core.management.base import BaseCommand, CommandError

from leave.shared_fund import SharedFundError, apply_accrual, plan_accrual


class Command(BaseCommand):
    help = "Credit one month to the shared annual leave fund."

    def add_arguments(self, parser):
        parser.add_argument("--pool", type=int, required=True)
        parser.add_argument("--company", action="append")
        parser.add_argument("--employee", action="append")
        parser.add_argument("--period", required=True, help="YYYY-MM")
        parser.add_argument("--days", type=float, default=1.0)
        parser.add_argument("--cap", type=float, default=12.0)
        parser.add_argument("--include-missing-joining-date", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--expect-count", type=int)

    def handle(self, *args, **options):
        from leave.models import LeaveType

        period = options["period"]
        if len(period) != 7 or period[4] != "-" or not period.replace("-", "").isdigit() or not 1 <= int(period[5:]) <= 12:
            raise CommandError("--period must be YYYY-MM")
        year, month = int(period[:4]), int(period[5:])
        if not 0 < options["days"] <= 31 or not 0 < options["cap"] <= 366:
            raise CommandError("--days / --cap out of range")
        pool = LeaveType._base_manager.filter(pk=options["pool"]).first()
        if pool is None:
            raise CommandError("--pool: unknown leave type")

        def ids(name):
            return [int(p) for v in options.get(name) or [] for p in str(v).split(",") if p.strip().isdigit()]

        rows = plan_accrual(
            pool=pool, year=year, month=month, days=options["days"], cap=options["cap"],
            company_ids=ids("company"), employee_ids=ids("employee"),
            include_missing_joining_date=options["include_missing_joining_date"],
        )
        out = self.stdout.write
        out(f"Accrual {period} on fund #{pool.pk} {pool.name}: +{options['days']:g}/month, cap {options['cap']:g}")
        for r in rows:
            out(
                f"    ACCRUE  employee={r.employee.pk} accrued={r.accrued:g} credit={r.credit:g} "
                f"action={r.action}{(' (' + r.reason + ')') if r.reason else ''}"
            )
        credits = [r for r in rows if r.action == "credit"]
        out(f"  credits            : {len(credits)} | skipped: {len(rows) - len(credits)}")
        if options["dry_run"]:
            out(self.style.WARNING("DRY RUN — nothing was written."))
            return
        if options.get("expect_count") is None:
            raise CommandError("a real run needs --expect-count")
        try:
            apply_accrual(rows, year=year, month=month, expect_count=options["expect_count"])
        except SharedFundError as exc:
            raise CommandError(f"Refused: {exc}") from exc
        out(self.style.SUCCESS(f"Credited {len(credits)} fund(s)."))
