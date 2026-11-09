"""leave_reconcile: the read-only report finds every class of problem and
writes nothing. Fake data only."""

from io import StringIO

from django.core.management import call_command

from base.models import Company
from employee.models import EmployeeWorkInformation
from leave.management.commands.leave_reconcile import reconcile
from leave.models import AvailableLeave, LeaveType
from leave.tests.test_leave_sync import SyncFixtures


class LeaveReconcileTests(SyncFixtures):
    def test_finds_each_problem_and_writes_nothing(self):
        a1, a2 = self.employee(self.company_a), self.employee(self.company_a)
        annual = self.leave_type("Nghi phep nam", self.company_a)
        AvailableLeave._base_manager.filter(employee_id=a2, leave_type_id=annual).delete()  # missing
        self.leave_type("Compensatory Leave Type", is_compensatory_leave=True)
        self.leave_type(" compensatory  leave type ", self.company_a, is_compensatory_leave=True)  # duplicate
        old = self.leave_type("Phep cu", self.company_a)
        self.save(old, is_active=False)  # balances of an inactive type
        only_b = self.leave_type("Phep B", self.company_b)
        AvailableLeave._base_manager.create(employee_id=a1, leave_type_id=only_b, available_days=3,
                                            carryforward_days=0, total_leave_days=3)  # wrong company
        AvailableLeave._base_manager.filter(employee_id=a1, leave_type_id=annual).update(
            available_days=2, carryforward_days=0, total_leave_days=9)  # does not add up
        nobody = self.employee(self.company_a)
        EmployeeWorkInformation._base_manager.filter(employee_id=nobody).delete()  # no work info

        before = AvailableLeave._base_manager.count(), LeaveType._base_manager.count()
        report = reconcile()
        out = StringIO()
        call_command("leave_reconcile", stdout=out)
        self.assertEqual((AvailableLeave._base_manager.count(), LeaveType._base_manager.count()), before, "read-only")

        missing = {(m["company_id"], m["leave_type_id"]): m["count"] for m in report["missing_balances"]}
        self.assertEqual(missing.get((self.company_a.pk, annual.pk)), 1)
        self.assertIn("compensatory leave type", report["duplicate_type_names"])
        self.assertEqual(len(report["compensatory_types"]), 2)
        self.assertEqual(report["duplicate_balance_rows"], 0, "the unique constraint holds")
        self.assertTrue(any(w["leave_type_id"] == only_b.pk for w in report["wrong_company_balances"]))
        self.assertTrue(any(i["leave_type_id"] == old.pk for i in report["inactive_type_balances"]))
        self.assertEqual(report["employees_without_work_info"], 1)
        self.assertTrue(any(i["leave_type_id"] == annual.pk and i["problem"] == "total_mismatch"
                            for i in report["inconsistent_balances"]))
        text = out.getvalue()
        self.assertIn("CHỈ ĐỌC", text)
        self.assertIn("⚠ chỉ được có 1", text)

    def test_clean_data_reports_nothing_to_fix(self):
        self.employee(self.company_a)
        self.leave_type("Nghi phep nam", self.company_a)
        report = reconcile()
        self.assertEqual(report["missing_total"], 0)
        self.assertEqual(report["duplicate_type_names"], {})
        self.assertEqual(report["wrong_company_balances"], [])
        self.assertEqual(report["inconsistent_balances"], [])

    def test_json_output(self):
        out = StringIO()
        call_command("leave_reconcile", "--json", stdout=out)
        self.assertIn('"missing_total"', out.getvalue())
