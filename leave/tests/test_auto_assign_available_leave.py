"""
Auto-assignment of AvailableLeave.

Covers the rule that a leave type reaches its company's employees without
anyone assigning it by hand, and the guards that stop it reaching people it
should not: another company's staff, inactive types, inactive employees, and
anyone a leave type's own conditions exclude.

Everything runs against the test database; no production data is touched.
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from base.models import Company, Department, JobPosition
from employee.models import Employee, EmployeeWorkInformation
from leave.models import AvailableLeave, LeaveType, LeaveTypeCondition
from leave.services import ensure_available_leave


class AutoAssignAvailableLeaveTests(TestCase):
    def setUp(self):
        self.company_a = Company.objects.create(company="Company A")
        self.company_b = Company.objects.create(company="Company B")

    # -- helpers ---------------------------------------------------------
    # The signals defer their work with `transaction.on_commit`, which never
    # runs under `TestCase` (each test lives inside a transaction that is
    # rolled back). `captureOnCommitCallbacks(execute=True)` runs them at the
    # point a real commit would, so these tests exercise the same code path
    # production does.
    def _employee(self, first, company, is_active=True):
        # Employee.save() opens a JoydigiUser from the email, so every
        # fixture needs a unique one.
        with self.captureOnCommitCallbacks(execute=True):
            employee = Employee.objects.create(
                employee_first_name=first,
                employee_last_name="Test",
                email=f"{first.lower()}@example.test",
                phone="0900000000",
                is_active=is_active,
            )
            work_info, _ = EmployeeWorkInformation.objects.get_or_create(
                employee_id=employee
            )
            work_info.company_id = company
            work_info.save()
        employee.refresh_from_db()
        return employee

    def _leave_type(self, name, company=None, total_days=12.0, is_active=True):
        with self.captureOnCommitCallbacks(execute=True):
            leave_type = LeaveType.objects.create(
                name=name,
                company_id=company,
                total_days=total_days,
                is_active=is_active,
                payment="paid",
            )
        return leave_type

    def _has(self, employee, leave_type):
        return AvailableLeave.objects.filter(
            employee_id=employee, leave_type_id=leave_type
        ).exists()

    # -- CASE A ----------------------------------------------------------
    def test_new_leave_type_reaches_every_employee_in_its_company(self):
        alice = self._employee("Alice", self.company_a)
        bob = self._employee("Bob", self.company_a)

        leave_type = self._leave_type("Annual", company=self.company_a)

        self.assertTrue(self._has(alice, leave_type))
        self.assertTrue(self._has(bob, leave_type))

    # -- CASE B ----------------------------------------------------------
    def test_new_employee_receives_every_active_type_of_their_company(self):
        annual = self._leave_type("Annual", company=self.company_a)
        sick = self._leave_type("Sick", company=self.company_a)

        carol = self._employee("Carol", self.company_a)

        self.assertTrue(self._has(carol, annual))
        self.assertTrue(self._has(carol, sick))

    # -- CASE C ----------------------------------------------------------
    def test_existing_assignment_is_never_duplicated_or_overwritten(self):
        dave = self._employee("Dave", self.company_a)
        leave_type = self._leave_type("Annual", company=self.company_a)

        row = AvailableLeave.objects.get(employee_id=dave, leave_type_id=leave_type)
        row.available_days = 3.5
        row.save()

        # Re-running the helper must not touch the spent-down balance.
        result, created = ensure_available_leave(dave, leave_type)
        self.assertFalse(created)
        self.assertEqual(result.pk, row.pk)
        self.assertEqual(
            AvailableLeave.objects.filter(
                employee_id=dave, leave_type_id=leave_type
            ).count(),
            1,
        )
        row.refresh_from_db()
        self.assertEqual(row.available_days, 3.5)

    # -- CASE F ----------------------------------------------------------
    def test_leave_type_never_crosses_into_another_company(self):
        erin = self._employee("Erin", self.company_b)
        type_a = self._leave_type("A-only", company=self.company_a)

        self.assertFalse(self._has(erin, type_a))

    def test_company_less_type_is_global(self):
        frank = self._employee("Frank", self.company_a)
        globally = self._leave_type("Global", company=None)

        self.assertTrue(self._has(frank, globally))

    # -- CASE G ----------------------------------------------------------
    def test_inactive_leave_type_is_not_assigned(self):
        inactive = self._leave_type("Retired", company=self.company_a, is_active=False)
        grace = self._employee("Grace", self.company_a)

        self.assertFalse(self._has(grace, inactive))

    def test_inactive_employee_is_not_assigned(self):
        heidi = self._employee("Heidi", self.company_a, is_active=False)
        leave_type = self._leave_type("Annual", company=self.company_a)

        self.assertFalse(self._has(heidi, leave_type))

    def test_leave_type_conditions_are_respected(self):
        # `LeaveType.conditions` is a M2M attached after the type exists, so
        # the condition has to be in place before the employee shows up —
        # otherwise auto-assign has already run against an unrestricted type.
        maternity = self._leave_type("Maternity", company=self.company_a)
        condition = LeaveTypeCondition.objects.create(
            condition_type="gender", value="female"
        )
        maternity.conditions.add(condition)

        ivan = self._employee("Ivan", self.company_a)
        ivan.gender = "male"
        ivan.save()

        self.assertFalse(self._has(ivan, maternity))

        _row, created = ensure_available_leave(ivan, maternity)
        self.assertFalse(created)
        self.assertFalse(self._has(ivan, maternity))

    # -- BALANCE ---------------------------------------------------------
    def test_auto_assigned_row_matches_the_manual_assign_shape(self):
        judy = self._employee("Judy", self.company_a)
        leave_type = self._leave_type("Annual", company=self.company_a, total_days=15.0)

        auto = AvailableLeave.objects.get(employee_id=judy, leave_type_id=leave_type)

        # What leave_assign()/leave_assign_one() build by hand.
        manual = AvailableLeave(
            leave_type_id=leave_type,
            employee_id=judy,
            available_days=leave_type.total_days,
        )
        manual.pre_save_processing()

        self.assertEqual(auto.available_days, manual.available_days)
        self.assertEqual(auto.carryforward_days, manual.carryforward_days)
        self.assertEqual(auto.total_leave_days, manual.total_leave_days)
        self.assertEqual(auto.available_days, 15.0)

    # -- CASE D / E : backfill -------------------------------------------
    def test_backfill_creates_missing_rows_then_is_a_no_op(self):
        ken = self._employee("Ken", self.company_a)
        leave_type = self._leave_type("Annual", company=self.company_a)

        # Simulate pre-existing data that never got an assignment.
        AvailableLeave.objects.filter(
            employee_id=ken, leave_type_id=leave_type
        ).delete()
        self.assertFalse(self._has(ken, leave_type))

        out = StringIO()
        call_command("backfill_available_leave", stdout=out)
        self.assertIn("CREATED            : 1", out.getvalue())
        self.assertTrue(self._has(ken, leave_type))

        out2 = StringIO()
        call_command("backfill_available_leave", stdout=out2)
        self.assertIn("CREATED            : 0", out2.getvalue())

    def test_backfill_dry_run_writes_nothing(self):
        liam = self._employee("Liam", self.company_a)
        leave_type = self._leave_type("Annual", company=self.company_a)
        AvailableLeave.objects.filter(
            employee_id=liam, leave_type_id=leave_type
        ).delete()

        out = StringIO()
        call_command("backfill_available_leave", "--dry-run", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertFalse(self._has(liam, leave_type))

    def test_backfill_leaves_existing_balances_untouched(self):
        mia = self._employee("Mia", self.company_a)
        leave_type = self._leave_type("Annual", company=self.company_a)
        row = AvailableLeave.objects.get(employee_id=mia, leave_type_id=leave_type)
        row.available_days = 2.0
        row.carryforward_days = 1.0
        row.save()

        call_command("backfill_available_leave", stdout=StringIO())

        row.refresh_from_db()
        self.assertEqual(row.available_days, 2.0)
        self.assertEqual(row.carryforward_days, 1.0)


class SubmitValidationStillGuardsTests(TestCase):
    """The submit-time gate stays exactly as it was — auto-assign feeds it,
    it is not bypassed."""

    def test_validation_source_is_unchanged(self):
        import inspect

        from joydigi_api.api_serializers.leave import serializers as leave_serializers

        source = inspect.getsource(leave_serializers.leave_Validations)
        self.assertIn("AvailableLeave.objects.filter(", source)
        self.assertIn("Employee is not assigned with leave type", source)
