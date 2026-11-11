"""
convert_shared_annual_balance — turn two separate balances into ONE fund.

2026-11-11. JOYDIGI: "NGHỈ PHÉP THÁNG" and "Nghỉ Phép Năm" are two ways to
request days from one 12-day annual fund. This command, run by hand after HR
approval:

1. links ``--alias`` types to the ``--pool`` type (``shared_balance_with``):
   from then on their requests use the pool's balance;
2. sets each employee's fund to ``entitlement − days approved this year over
   all the fund's types`` (``--entitlement`` for everyone, ``--entitlement-for
   EMP=DAYS`` per person; anyone without one is refused, "Chưa xác định");
3. switches the aliases' own old balance rows off (kept, never deleted).

Never adds the two old balances together. Refuses, per employee, a revoked
fund, a carried-forward balance and a negative result. A real run needs
``--expect-count`` (rows changed), is all-or-nothing, and leaves a marker in
each row's history ("[quỹ chung 2026: tích lũy N ngày đến 2026-11]") that the
monthly accrual continues from.

    python manage.py convert_shared_annual_balance --dry-run --pool 7 --alias 1 \\
        --company 1 --year 2026 --as-of 2026-11 --entitlement 12
"""

from django.core.management.base import BaseCommand, CommandError

from leave.shared_fund import SharedFundError, apply_conversion, plan_conversion


def _ints(values, name):
    out = []
    for value in values or []:
        for part in str(value).split(","):
            if part.strip():
                if not part.strip().isdigit():
                    raise CommandError(f"--{name}: '{part}' is not an id.")
                out.append(int(part))
    return out


def _days(text, where):
    try:
        days = float(text)
    except ValueError:
        raise CommandError(f"{where}: '{text}' is not a number of days.")
    if not 0 <= days <= 366:
        raise CommandError(f"{where}: days must be between 0 and 366.")
    return days


class Command(BaseCommand):
    help = "Convert separate monthly/annual leave balances into one shared annual fund."

    def add_arguments(self, parser):
        parser.add_argument("--pool", type=int, required=True, help="Leave type id holding the fund.")
        parser.add_argument("--alias", action="append", required=True, help="Leave type id(s) drawing on it.")
        parser.add_argument("--company", action="append", help="Only employees of these company ids.")
        parser.add_argument("--employee", action="append", help="Only these employee ids.")
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--as-of", required=True, help="Last month the entitlement covers, YYYY-MM.")
        parser.add_argument("--entitlement", help="Days of the fund for everyone (HR's decision).")
        parser.add_argument("--entitlement-for", action="append", help="EMPLOYEE_ID=DAYS, overrides --entitlement.")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--expect-count", type=int)

    def handle(self, *args, **options):
        from leave.models import LeaveType

        try:
            pool = LeaveType._base_manager.get(pk=options["pool"])
            aliases = list(LeaveType._base_manager.filter(pk__in=_ints(options["alias"], "alias")))
        except LeaveType.DoesNotExist:
            raise CommandError("--pool: unknown leave type")
        period = options["as_of"]
        if len(period) != 7 or period[4] != "-" or not period.replace("-", "").isdigit() or int(period[:4]) != options["year"]:
            raise CommandError("--as-of must be YYYY-MM within --year")
        entitlement = _days(options["entitlement"], "--entitlement") if options.get("entitlement") else None
        per = {}
        for spec in options.get("entitlement_for") or []:
            emp, sep, days = spec.partition("=")
            if not sep or not emp.strip().isdigit():
                raise CommandError(f"--entitlement-for: '{spec}' must be EMPLOYEE_ID=DAYS")
            per[int(emp)] = _days(days, "--entitlement-for")
        try:
            plan = plan_conversion(
                pool=pool, aliases=aliases, year=options["year"], period=period, entitlement=entitlement,
                entitlement_for=per, company_ids=_ints(options.get("company"), "company"),
                employee_ids=_ints(options.get("employee"), "employee"),
            )
        except SharedFundError as exc:
            raise CommandError(str(exc)) from exc

        out = self.stdout.write
        out("Convert to one shared annual fund")
        out(f"  fund (pool)        : #{pool.pk} {pool.name} | shared by: " + ", ".join(f"#{a.pk} {a.name}" for a in aliases))
        out(f"  link now           : {[a.pk for a in plan.link_aliases] or 'already linked'}")
        out(f"  year / as of       : {plan.year} / {plan.period}")
        out(f"  employees          : {len(plan.rows)}")
        for r in plan.rows:
            approved = "+".join(f"#{t}:{d:g}" for t, d in sorted(r.approved.items())) or "0"
            pending = "+".join(f"#{t}:{d:g}" for t, d in sorted(r.pending.items())) or "0"
            olds = ",".join(f"#{a.leave_type_id_id}={a.available_days:g}{'' if a.is_active else '(off)'}" for a in r.alias_rows) or "-"
            out(
                f"    FUND  employee={r.employee.pk} entitlement={_fmt(r.entitlement)} approved={approved} "
                f"pending={pending} current={_fmt(r.current_remaining)} -> new={_fmt(r.new_remaining)} "
                f"old-alias-rows={olds} action={r.action}{(' (' + r.reason + ')') if r.reason else ''}"
            )
        out(f"  changes            : {len(plan.changes)} | refused: {len(plan.refused)}")
        if options["dry_run"]:
            out(self.style.WARNING("DRY RUN — nothing was written."))
            return
        if options.get("expect_count") is None:
            raise CommandError("a real run needs --expect-count")
        try:
            apply_conversion(plan, expect_count=options["expect_count"])
        except SharedFundError as exc:
            raise CommandError(f"Refused: {exc}") from exc
        out(self.style.SUCCESS(f"Converted {len(plan.changes)} fund(s)."))


def _fmt(value):
    return "?" if value is None else f"{value:g}"
