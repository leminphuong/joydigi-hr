"""
backfill_available_leave

Opens the leave balances that should already exist but don't.

Auto-assignment (``leave/signals.py``) only covers changes — employees and
leave types that predate it can still be missing each other. This command
closes that gap for the data already in the database through
``leave.balance_sync``, the same planner the scheduled sync uses, which opens
rows with the same ``leave.services.ensure_available_leave`` the signals use:
the same company match, the same leave-type conditions.

It only ever *creates*. An existing ``AvailableLeave`` is left completely
alone — its ``available_days``, ``carryforward_days`` and ``total_leave_days``
are never read, recomputed or overwritten — so running this can not disturb a
balance an employee has already spent against. A row that is switched off
(``is_active=False``) still exists, so it is left alone too.

Idempotent: a second run creates nothing.

Scope (2026-11-10). Production fixes are reviewed by HR, so a run can be
limited to exactly what was approved; every option can be repeated or given a
comma-separated list:

    --employee 28 --employee 31      only these employee ids
    --leave-type 1,3                 only these leave type ids
    --company 1                      only employees whose work information is
                                     in this company

Amounts (2026-11-10). A leave type's ``total_days`` is a configuration value
that can be wrong (production "Nghỉ Phép Năm" says 1 day while HR grants 12),
so a real run never seeds it silently. Every row it creates needs an amount:

    --grant 1=12                     12 days of leave type 1, for everyone in scope
    --grant 28:1=6                   6 days of leave type 1 for employee 28 only
                                     (wins over the type-wide amount)
    --use-type-total                 seed the type's configured total_days
                                     where no --grant is given (refused for a
                                     limited type configured with 0 days or
                                     more than 366)

A real run without an amount for a row it would create refuses and writes
nothing. A dry run shows those rows with ``days=?``. ``--grant`` changes
neither the leave type nor any existing balance.

Revoked balances. A balance HR deleted leaves a deletion record in the
balance history; such a pair is never re-opened — that decision was HR's.
If the history can not be read, a real run refuses.

Safety for production writes:

    --expect-count 8                 a real run refuses unless it would create
                                     exactly 8 rows (what HR approved)

A real run is all-or-nothing: any error rolls every row back. Each row's
history carries the reason "backfill_available_leave (HR duyệt)".

Compensatory leave types are skipped unless ``--include-compensatory`` is
given: a compensatory balance is earned through approved compensatory
requests — a backfilled row would hand every employee free days.

    python manage.py backfill_available_leave --dry-run --company 1 --leave-type 1,3 --grant 1=12 --grant 3=30
    python manage.py backfill_available_leave --company 1 --employee 17,24,28,31 \\
        --leave-type 1,3 --grant 1=12 --grant 3=30 --expect-count 8
"""

from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from leave.balance_sync import SyncRefused, SyncScopeError, apply_plan, plan_missing_balances

MAX_GRANT_DAYS = Decimal("366")


def _one_id(part, name):
    part = part.strip()
    if not part.isdigit() or int(part) < 1:
        raise CommandError(f"--{name}: '{part}' is not an id.")
    return int(part)


def _ids(values, name):
    """Integers from repeated and/or comma-separated option values."""
    ids = []
    for value in values or []:
        for part in str(value).split(","):
            if part.strip():
                ids.append(_one_id(part, name))
    return sorted(set(ids))


def _days(text, spec):
    """A positive number of days, at most two decimals, at most a year."""
    try:
        days = Decimal(text.strip())
    except InvalidOperation:
        raise CommandError(f"--grant {spec}: '{text.strip()}' is not a number of days.")
    if not days.is_finite() or days <= 0 or days > MAX_GRANT_DAYS:
        raise CommandError(f"--grant {spec}: days must be more than 0 and at most {MAX_GRANT_DAYS}.")
    if days != days.quantize(Decimal("0.01")):
        raise CommandError(f"--grant {spec}: at most two decimals (use a dot: 12.5).")
    return float(days)


def _grants(values):
    """``({type_id: days}, {(employee_id, type_id): days})`` from ``--grant``."""
    by_type, by_pair = {}, {}
    for value in values or []:
        for spec in str(value).split(","):
            spec = spec.strip()
            if not spec:
                continue
            key, sep, days = spec.partition("=")
            if not sep:
                raise CommandError(f"--grant: '{spec}' must be TYPE_ID=DAYS or EMPLOYEE_ID:TYPE_ID=DAYS.")
            days = _days(days, spec)
            employee_part, colon, type_part = key.partition(":")
            if colon:
                target, grant_key = by_pair, (_one_id(employee_part, "grant"), _one_id(type_part, "grant"))
            else:
                target, grant_key = by_type, _one_id(employee_part, "grant")
            if grant_key in target and target[grant_key] != days:
                raise CommandError(f"--grant: two different amounts for {spec.split('=')[0]}.")
            target[grant_key] = days
    return by_type, by_pair


def _fmt(days):
    return f"{days:g}" if days is not None else "?"


class Command(BaseCommand):
    help = "Create the AvailableLeave rows that are missing for existing employees."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what is missing without writing anything.",
        )
        parser.add_argument("--employee", action="append", help="Only these employee ids (repeat or comma-separate).")
        parser.add_argument("--leave-type", action="append", help="Only these leave type ids (repeat or comma-separate).")
        parser.add_argument("--company", action="append", help="Only employees of these company ids.")
        parser.add_argument(
            "--grant",
            action="append",
            help="Days to open a row with: TYPE_ID=DAYS, or EMPLOYEE_ID:TYPE_ID=DAYS for one employee.",
        )
        parser.add_argument(
            "--use-type-total",
            action="store_true",
            help="Where no --grant is given, seed the leave type's configured total_days.",
        )
        parser.add_argument(
            "--expect-count",
            type=int,
            help="A real run refuses unless it would create exactly this many rows.",
        )
        parser.add_argument(
            "--include-compensatory",
            action="store_true",
            help="Also open compensatory leave types (free days!).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        employee_ids = _ids(options.get("employee"), "employee")
        leave_type_ids = _ids(options.get("leave_type"), "leave-type")
        company_ids = _ids(options.get("company"), "company")
        grant_by_type, grant_by_pair = _grants(options.get("grant"))
        use_type_total = options["use_type_total"]
        expect_count = options.get("expect_count")

        try:
            plan = plan_missing_balances(
                employee_ids=employee_ids,
                leave_type_ids=leave_type_ids,
                company_ids=company_ids,
                grant_by_type=grant_by_type,
                grant_by_pair=grant_by_pair,
                use_type_total=use_type_total,
                include_compensatory=options["include_compensatory"],
            )
        except SyncScopeError as exc:
            raise CommandError(str(exc)) from exc

        self._report(plan, employee_ids, leave_type_ids, company_ids, grant_by_type, grant_by_pair, use_type_total)

        if dry_run:
            for row in plan.missing:
                line = (
                    f"    WOULD CREATE  employee={row.employee.pk} leave_type={row.leave_type.pk} ({row.leave_type.name})"
                    f" days={_fmt(row.days)} source={row.source}"
                )
                if row.days is None:
                    line += f" type_total={_fmt(row.leave_type.total_days)}"
                self.stdout.write(line)
            if not plan.revocation_checked:
                self.stdout.write(self.style.WARNING("  A real run would REFUSE: the balance history can not be read."))
            if plan.without_amount:
                self.stdout.write(
                    self.style.WARNING(
                        f"  A real run would REFUSE: {len(plan.without_amount)} row(s) have no amount — "
                        "pass --grant TYPE_ID=DAYS (or --use-type-total to seed the configured total_days)."
                    )
                )
            if expect_count is not None and expect_count != len(plan.missing):
                self.stdout.write(
                    self.style.WARNING(
                        f"  A real run would REFUSE: --expect-count {expect_count}, found {len(plan.missing)}."
                    )
                )
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was written."))
            return

        try:
            opened = apply_plan(plan, expect_count=expect_count, reason="backfill_available_leave (HR duyệt)")
        except SyncRefused as exc:
            raise CommandError(f"Refused, nothing written: {exc}") from exc
        except Exception as exc:
            self.stderr.write(f"  ROLLED BACK — nothing was written: {exc}")
            raise CommandError(f"Backfill rolled back: {exc}") from exc

        for row in opened:
            self.stdout.write(
                f"    CREATED  employee={row.employee.pk} leave_type={row.leave_type.pk} ({row.leave_type.name})"
                f" days={_fmt(row.days)} source={row.source}"
            )
        self.stdout.write(f"  CREATED            : {len(opened)}")
        self.stdout.write(f"  SKIPPED EXISTING   : {len(plan.existing_pairs)}")
        self.stdout.write("  ERRORS             : 0")
        self.stdout.write(self.style.SUCCESS("Backfill complete."))

    def _report(self, plan, employee_ids, leave_type_ids, company_ids, grant_by_type, grant_by_pair, use_type_total):
        self.stdout.write("Backfill AvailableLeave")
        self.stdout.write(
            "  scope              : "
            f"employees={employee_ids or 'all'} leave_types={leave_type_ids or 'all'} companies={company_ids or 'all'}"
        )
        self.stdout.write(f"  active employees   : {len(plan.employees)}")
        self.stdout.write(f"  active leave types : {len(plan.leave_types)}")
        for leave_type in plan.leave_types:
            if leave_type.pk in grant_by_type:
                source = f"--grant {_fmt(grant_by_type[leave_type.pk])}"
            elif use_type_total:
                source = f"--use-type-total {_fmt(leave_type.total_days)}"
            else:
                source = "NO AMOUNT GIVEN"
            self.stdout.write(
                f"    type {leave_type.pk} ({leave_type.name}): configured total_days={_fmt(leave_type.total_days)}"
                f" -> {source}"
            )
        for (employee_id, type_id), days in sorted(grant_by_pair.items()):
            self.stdout.write(f"    employee {employee_id} type {type_id}: --grant {_fmt(days)}")
        if plan.skipped_shared_types:
            self.stdout.write(
                "  shared-fund types skipped (their fund's type is opened instead): "
                + ", ".join(f"{t}->{pool}" for t, pool in plan.skipped_shared_types)
            )
        if plan.skipped_compensatory_types:
            self.stdout.write(
                f"  compensatory types skipped (use --include-compensatory): {plan.skipped_compensatory_types}"
            )
        self.stdout.write(f"  existing rows      : {len(plan.existing_pairs)}")
        if not plan.revocation_checked:
            self.stdout.write(self.style.WARNING("  revoked balances   : history unavailable, not checked"))
        self.stdout.write(f"  missing pairs      : {len(plan.missing)}")
        self.stdout.write(f"  skipped (company)  : {plan.skipped_company}")
        self.stdout.write(f"  skipped (condition): {plan.skipped_condition}")
        self.stdout.write(f"  skipped (revoked)  : {len(plan.skipped_revoked)}")
        for employee, leave_type, deleted_at in plan.skipped_revoked:
            self.stdout.write(
                f"    REVOKED BEFORE  employee={employee.pk} leave_type={leave_type.pk} ({leave_type.name})"
                f" deleted={deleted_at:%Y-%m-%d} — not re-opened, HR decides"
            )
        for employee_id, type_id in plan.ignored_grants:
            self.stdout.write(
                f"    GRANT IGNORED   employee={employee_id} leave_type={type_id}: a balance exists, it is kept as is"
            )
        for row in plan.without_amount:
            if row.problem:
                self.stdout.write(
                    f"    CONFIG REJECTED employee={row.employee.pk} leave_type={row.leave_type.pk}: {row.problem}"
                )
        self.stdout.write(f"  without an amount  : {len(plan.without_amount)}")
