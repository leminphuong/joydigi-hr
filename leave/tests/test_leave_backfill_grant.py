"""Backfill for HR-approved amounts (2026-11-10, 4 JOYDIGI employees).

Production "Nghỉ Phép Năm" is configured with 1 day while HR grants 12, so a
backfill that seeded the configured value would silently hand out 1 day. These
tests pin the guarded command HR will approve, in the shape production has:

* company A ("JOYDIGI") and company B ("JDG MEDIA");
* "NGHỈ PHÉP THÁNG" (2), "Nghỉ Phép Năm" (configured 1), "Nghỉ Chế Độ" (30),
  an active and an inactive compensatory type, an inactive ordinary type, and a
  company-B type;
* four employees missing "Năm" and "Chế Độ"; colleagues holding them.

One test per approval rule, numbered like the report. Fake data only.
"""

from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from leave.models import AvailableLeave, LeaveRequest, LeaveType
from leave.tests.test_leave_sync import SyncFixtures


def _table_counts():
    """Row count of every table Django manages."""
    return {
        model._meta.label: model._base_manager.count()
        for model in apps.get_models(include_auto_created=True)
        if model._meta.managed and not model._meta.proxy
    }


class GrantFixtures(SyncFixtures):
    def setUp(self):
        super().setUp()
        self.monthly = self.leave_type("NGHI PHEP THANG", self.company_a, total_days=2)
        self.annual = self.leave_type("Nghi Phep Nam", self.company_a, total_days=1)
        self.regime = self.leave_type("Nghi Che Do", self.company_a, total_days=30)
        self.comp_on = self.leave_type("Nghi Bu", self.company_a, is_compensatory_leave=True, total_days=1)
        self.comp_off = self.leave_type("Compensatory Leave Type", is_compensatory_leave=True, total_days=1)
        self.old_type = self.leave_type("Phep cu", self.company_a, total_days=5)
        self.b_type = self.leave_type("Phep JDG MEDIA", self.company_b, total_days=12)
        self.targets = [self.employee(self.company_a) for _ in range(4)]
        self.colleagues = [self.employee(self.company_a) for _ in range(2)]
        self.colleague_with_gap = self.employee(self.company_a)
        self.other_company = self.employee(self.company_b)
        # Colleagues got 12 / 30 by hand; the configuration says 1 for "Năm".
        for person in self.colleagues:
            AvailableLeave._base_manager.filter(employee_id=person, leave_type_id=self.annual).update(
                available_days=12, total_leave_days=12
            )
        # The legacy gap: never opened (no row, no history).
        self.never_opened(self.targets + [self.colleague_with_gap], [self.annual, self.regime])
        self.never_opened([self.other_company], [self.b_type])
        self.never_opened(self.targets, [self.comp_on, self.old_type])
        self.save(self.comp_off, is_active=False)
        self.save(self.old_type, is_active=False)

    def never_opened(self, people, types):
        employee_ids, type_ids = [p.pk for p in people], [t.pk for t in types]
        AvailableLeave._base_manager.filter(employee_id__in=employee_ids, leave_type_id__in=type_ids).delete()
        AvailableLeave.history.filter(employee_id__in=employee_ids, leave_type_id__in=type_ids).delete()

    def approved(self, *extra, expect=8):
        args = [
            "--company", str(self.company_a.pk),
            "--employee", ",".join(str(p.pk) for p in self.targets),
            "--leave-type", f"{self.annual.pk},{self.regime.pk}",
            "--grant", f"{self.annual.pk}=12",
            "--grant", f"{self.regime.pk}=30",
        ]
        if expect is not None:
            args += ["--expect-count", str(expect)]
        return args + list(extra)

    def run_command(self, *args):
        out = StringIO()
        call_command("backfill_available_leave", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def rows(self, people=None):
        query = AvailableLeave._base_manager.all()
        if people is not None:
            query = query.filter(employee_id__in=[p.pk for p in people])
        return sorted(
            query.values_list(
                "employee_id", "leave_type_id", "available_days", "carryforward_days", "total_leave_days", "is_active"
            )
        )


class ApprovedBackfillTests(GrantFixtures):
    def test_01_the_four_approved_employees_get_exactly_the_approved_rows(self):
        before = AvailableLeave._base_manager.count()
        out = self.run_command(*self.approved())
        self.assertIn("CREATED            : 8", out)
        self.assertEqual(AvailableLeave._base_manager.count(), before + 8)
        for person in self.targets:
            self.assertEqual(self.balance(person, self.annual).available_days, 12)
            self.assertEqual(self.balance(person, self.annual).total_leave_days, 12)
            self.assertEqual(self.balance(person, self.regime).available_days, 30)

    def test_02_nobody_outside_the_list_is_granted(self):
        others = self.colleagues + [self.colleague_with_gap, self.other_company]
        before = self.rows(others)
        self.run_command(*self.approved())
        self.assertEqual(self.rows(others), before)
        self.assertFalse(self.has(self.colleague_with_gap, self.annual), "same company, not approved: no row")

    def test_03_never_another_company(self):
        # An id from company B slipped into the list: --company keeps it out.
        args = self.approved(expect=8)
        args[args.index("--employee") + 1] += f",{self.other_company.pk}"
        before = self.rows([self.other_company])
        self.run_command(*args)
        self.assertEqual(self.rows([self.other_company]), before)
        self.assertFalse(self.has(self.other_company, self.annual))
        self.assertFalse(self.has(self.other_company, self.b_type))
        # A company-B type can't be granted inside a company-A run.
        with self.assertRaisesMessage(CommandError, "belongs to company"):
            self.run_command(*self.approved("--leave-type", str(self.b_type.pk), "--grant", f"{self.b_type.pk}=12"))

    def test_04_never_an_inactive_type(self):
        with self.assertRaisesMessage(CommandError, "not in the scope"):
            self.run_command(*self.approved("--leave-type", str(self.old_type.pk), "--grant", f"{self.old_type.pk}=5"))
        self.run_command(*self.approved("--leave-type", str(self.old_type.pk)))
        self.assertFalse(any(self.has(p, self.old_type) for p in self.targets))

    def test_05_never_compensatory_leave(self):
        with self.assertRaisesMessage(CommandError, "not in the scope"):
            self.run_command(*self.approved("--leave-type", str(self.comp_on.pk), "--grant", f"{self.comp_on.pk}=1"))
        out = self.run_command(*self.approved("--leave-type", f"{self.comp_on.pk},{self.comp_off.pk}"))
        self.assertIn("compensatory types skipped", out)
        self.assertFalse(any(self.has(p, self.comp_on) for p in self.targets))
        self.assertFalse(LeaveType._base_manager.get(pk=self.comp_off.pk).is_active, "stays off")

    def test_06_no_duplicates_and_a_second_run_is_refused_by_the_expected_count(self):
        self.run_command(*self.approved())
        with self.assertRaisesMessage(CommandError, "--expect-count 8 but 0 row(s)"):
            self.run_command(*self.approved())
        self.assertIn("CREATED            : 0", self.run_command(*self.approved(expect=0)))
        for person in self.targets:
            for leave_type in (self.annual, self.regime):
                self.assertEqual(
                    AvailableLeave._base_manager.filter(employee_id=person, leave_type_id=leave_type).count(), 1
                )

    def test_07_an_existing_balance_is_never_overwritten(self):
        spent = self.targets[0]
        with self.captureOnCommitCallbacks(execute=True):
            row = AvailableLeave._base_manager.create(
                employee_id=spent, leave_type_id=self.annual, available_days=3, carryforward_days=1
            )
        out = self.run_command(*self.approved(f"--grant={spent.pk}:{self.annual.pk}=12", expect=7))
        row.refresh_from_db()
        self.assertEqual((row.available_days, row.carryforward_days, row.total_leave_days), (3, 1, 4))
        self.assertIn(f"GRANT IGNORED   employee={spent.pk} leave_type={self.annual.pk}", out)

    def test_08_a_revoked_or_switched_off_balance_is_not_restored(self):
        revoked, switched_off = self.targets[0], self.targets[1]
        with self.captureOnCommitCallbacks(execute=True):
            AvailableLeave._base_manager.create(employee_id=revoked, leave_type_id=self.regime, available_days=30)
        AvailableLeave._base_manager.get(employee_id=revoked, leave_type_id=self.regime).delete()  # HR revokes
        with self.captureOnCommitCallbacks(execute=True):
            AvailableLeave._base_manager.create(
                employee_id=switched_off, leave_type_id=self.regime, available_days=0, is_active=False
            )
        out = self.run_command(*self.approved(expect=6))
        self.assertFalse(self.has(revoked, self.regime), "a revoked balance stays revoked")
        self.assertIn(f"REVOKED BEFORE  employee={revoked.pk} leave_type={self.regime.pk}", out)
        row = self.balance(switched_off, self.regime)
        self.assertEqual((row.available_days, row.is_active), (0, False), "a switched-off row is left alone")

    def test_09_never_the_wrong_number_of_days(self):
        before = self.rows()
        # No amount: refuse before writing — never the configured 1 day.
        no_grant = self.approved(expect=None)[:6]
        self.assertNotIn("--grant", no_grant)
        with self.assertRaisesMessage(CommandError, "no amount for type"):
            self.run_command(*no_grant)
        self.assertEqual(self.rows(), before)
        # Typos are errors, not a guess.
        for bad in ("1=0", "1=-2", "1=400", "1=abc", "1=12.345", "1", "x=12", f"{self.annual.pk}=12,5"):
            with self.assertRaises(CommandError, msg=bad):
                self.run_command(*no_grant, "--grant", bad)
        with self.assertRaisesMessage(CommandError, "two different amounts"):
            self.run_command(*no_grant, "--grant", f"{self.annual.pk}=12", "--grant", f"{self.annual.pk}=11")
        with self.assertRaisesMessage(CommandError, "employee"):
            self.run_command(*self.approved("--grant", f"{self.colleague_with_gap.pk}:{self.annual.pk}=12"))
        self.assertEqual(self.rows(), before)
        # One employee's own amount wins over the type-wide one, for them only.
        special = self.targets[2]
        self.run_command(*self.approved("--grant", f"{special.pk}:{self.annual.pk}=6.5"))
        self.assertEqual(self.balance(special, self.annual).available_days, 6.5)
        self.assertEqual([self.balance(p, self.annual).available_days for p in self.targets if p != special], [12] * 3)
        self.annual.refresh_from_db()
        self.assertEqual(self.annual.total_days, 1, "the leave type's configuration is unchanged")

    def test_10_a_dry_run_changes_nothing_anywhere(self):
        counts, rows = _table_counts(), self.rows()
        with CaptureQueriesContext(connection) as queries:
            out = self.run_command("--dry-run", *self.approved())
        writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.assertEqual(_table_counts(), counts)
        self.assertEqual(self.rows(), rows)
        self.assertEqual(out.count("WOULD CREATE"), 8)
        self.assertIn(f"leave_type={self.annual.pk} (Nghi Phep Nam) days=12 source=grant:type", out)
        self.assertIn("DRY RUN", out)


class RealRunImpactTests(GrantFixtures):
    def test_a_real_run_adds_eight_balances_and_their_history_nothing_else(self):
        counts = _table_counts()
        requests = sorted(LeaveRequest._base_manager.values_list("pk", "status"))
        types = sorted(LeaveType._base_manager.values_list("pk", "total_days", "is_active", "company_id"))
        self.run_command(*self.approved())
        after = _table_counts()
        changed = {label: after[label] - counts[label] for label in counts if after[label] != counts[label]}
        self.assertEqual(changed, {"leave.AvailableLeave": 8, "leave.HistoricalAvailableLeave": 8})
        self.assertEqual(sorted(LeaveRequest._base_manager.values_list("pk", "status")), requests)
        self.assertEqual(sorted(LeaveType._base_manager.values_list("pk", "total_days", "is_active", "company_id")), types)

    def test_an_error_midway_rolls_every_row_back(self):
        before = self.rows()
        from leave import services

        real = services.ensure_available_leave
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 5:
                raise RuntimeError("disk full")
            return real(*args, **kwargs)

        with patch("leave.balance_sync.ensure_available_leave", flaky):
            with self.assertRaisesMessage(CommandError, "rolled back"):
                self.run_command(*self.approved())
        self.assertEqual(self.rows(), before)
