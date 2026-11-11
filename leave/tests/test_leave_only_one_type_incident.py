"""Incident 2026-11-10: an employee saw only one leave type in the app.

Reproduced with fake data in the same shape as production:

* "Nghỉ phép tháng"  — active, the employee's company, balance present;
* "Nghỉ phép năm" and "Nghỉ chế độ" — active, same company, no conditions,
  but NO balance for this employee (legacy rows never opened for 4 of 11
  employees; auto-sync only reacts to changes);
* "Compensatory Leave Type" — the employee holds a balance, but the type
  was switched off.

The API is right: it lists the one usable balance, puts the two eligible
types under ``unassigned`` (app 1.0.3 shows them greyed with "liên hệ HR";
1.0.1/1.0.2 ignore that key) and hides the inactive type. What fixes the
employee is data — a backfill limited to what HR approved — so the scoped
backfill is tested here too. Fake data only.
"""

from io import StringIO

from django.core.management import call_command

from leave.models import AvailableLeave
from leave.tests.test_leave_sync import ApiFixtures


class OnlyOneLeaveTypeIncidentTests(ApiFixtures):
    def setUp(self):
        super().setUp()
        self.colleague = self.employee(self.company_a)
        self.monthly = self.leave_type("NGHI PHEP THANG", self.company_a, total_days=2)
        self.annual = self.leave_type("Nghi Phep Nam", self.company_a, total_days=12)
        self.regime = self.leave_type("Nghi Che Do", self.company_a, total_days=30)
        self.comp = self.leave_type("Compensatory Leave Type", is_compensatory_leave=True, total_days=1)
        # Legacy gap: the two types were never opened for this employee (no
        # row and no history — a deleted row would count as revoked).
        AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id__in=[self.annual, self.regime]).delete()
        AvailableLeave.history.filter(employee_id=self.me.pk, leave_type_id__in=[self.annual.pk, self.regime.pk]).delete()
        # HR later switched the compensatory type off; balances stay as history.
        self.save(self.comp, is_active=False)

    def names(self, rows, key="leave_type_id"):
        return [row[key]["name"] if key else row["name"] for row in rows]

    def test_the_api_lists_one_usable_type_and_two_not_yet_granted(self):
        body = self.options()
        self.assertEqual(self.names(body["results"]), ["NGHI PHEP THANG"], "what app 1.0.1/1.0.2 show")
        self.assertEqual(sorted(self.names(body["unassigned"], key=None)), ["Nghi Che Do", "Nghi Phep Nam"])
        self.assertTrue(all(u["status"] == "not_assigned" and not u["requestable"] for u in body["unassigned"]))
        self.assertNotIn("Compensatory Leave Type", self.names(body["results"]), "inactive type is not offered")
        self.assertTrue(self.has(self.me, self.comp), "its balance is kept, not deleted")

    def test_a_request_for_a_type_not_granted_is_refused_in_vietnamese(self):
        response = self.request(self.annual)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("chưa được cấp", str(response.json()))

    def test_scoped_dry_run_lists_exactly_the_gap_and_writes_nothing(self):
        before = AvailableLeave._base_manager.count()
        out = StringIO()
        call_command(
            "backfill_available_leave", "--dry-run",
            "--company", str(self.company_a.pk), "--leave-type", f"{self.annual.pk},{self.regime.pk}",
            "--grant", f"{self.annual.pk}=12",
            stdout=out,
        )
        lines = [line.strip() for line in out.getvalue().splitlines() if "WOULD CREATE" in line]
        self.assertEqual(
            sorted(lines),
            sorted([
                f"WOULD CREATE  employee={self.me.pk} leave_type={self.annual.pk} (Nghi Phep Nam) days=12 source=grant:type",
                f"WOULD CREATE  employee={self.me.pk} leave_type={self.regime.pk} (Nghi Che Do) days=? source=none type_total=30",
            ]),
        )
        self.assertIn("A real run would REFUSE: 1 row(s) have no amount", out.getvalue())
        self.assertEqual(AvailableLeave._base_manager.count(), before)

    def test_scoped_backfill_makes_all_three_appear_once_and_touches_nothing_else(self):
        colleague_rows = list(
            AvailableLeave._base_manager.filter(employee_id=self.colleague).values_list(
                "leave_type_id", "available_days", "carryforward_days", "total_leave_days"
            ).order_by("leave_type_id")
        )
        args = [
            "--company", str(self.company_a.pk), "--leave-type", f"{self.annual.pk},{self.regime.pk}",
            "--grant", f"{self.annual.pk}=12", "--grant", f"{self.regime.pk}=30",
        ]
        call_command("backfill_available_leave", *args, stdout=StringIO())
        call_command("backfill_available_leave", *args, stdout=StringIO())  # idempotent
        body = self.options()
        self.assertEqual(self.names(body["results"]), ["NGHI PHEP THANG", "Nghi Che Do", "Nghi Phep Nam"])
        self.assertEqual(body["unassigned"], [])
        for leave_type in (self.annual, self.regime):
            self.assertEqual(
                AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id=leave_type).count(), 1
            )
        self.assertEqual(
            list(
                AvailableLeave._base_manager.filter(employee_id=self.colleague).values_list(
                    "leave_type_id", "available_days", "carryforward_days", "total_leave_days"
                ).order_by("leave_type_id")
            ),
            colleague_rows,
            "other people's balances are untouched",
        )

    def test_backfill_outside_the_approved_scope_grants_nothing(self):
        outsider = self.employee(self.company_a)
        AvailableLeave._base_manager.filter(employee_id=outsider, leave_type_id=self.annual).delete()  # removed by HR
        call_command("backfill_available_leave", "--employee", str(self.me.pk), "--use-type-total", stdout=StringIO())
        self.assertTrue(self.has(self.me, self.annual))
        self.assertFalse(self.has(outsider, self.annual), "a balance HR removed is not restored outside the scope")


class BackfillCompensatoryTests(ApiFixtures):
    def test_compensatory_types_are_skipped_unless_asked(self):
        comp = self.leave_type("Compensatory Leave Type", is_compensatory_leave=True, total_days=1)
        AvailableLeave._base_manager.filter(leave_type_id=comp).delete()
        AvailableLeave.history.filter(leave_type_id=comp.pk).delete()  # never opened, not revoked
        out = StringIO()
        call_command("backfill_available_leave", "--use-type-total", stdout=out)
        self.assertFalse(self.has(self.me, comp), "no free compensatory days")
        self.assertIn("compensatory types skipped", out.getvalue())
        call_command("backfill_available_leave", "--include-compensatory", "--use-type-total", stdout=StringIO())
        self.assertTrue(self.has(self.me, comp))

    def test_bad_scope_values_are_refused(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("backfill_available_leave", "--dry-run", "--leave-type", "abc", stdout=StringIO())
