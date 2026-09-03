"""Phase EMPLOYEE-SINGLE-DELETE-ALIGN-1 — the row action and the bulk action
must behave identically.

Before this phase there were two deletion implementations. The bulk one went
through ``account_deletion``; the single one called ``user.delete()`` directly,
so the same employee could be deleted from the toolbar and refused from their
own row — and when the row action did succeed it applied none of the account
protections and ran outside a transaction.

These tests assert the row action now enforces the same rules, and the last
group asserts that neither view carries deletion logic of its own, which is
what stops the two paths drifting apart again.
"""

from datetime import date, time

from django.contrib.auth.models import Permission
from django.db.models import ProtectedError
from django.test import Client, TestCase
from unittest import mock

from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceLateComeEarlyOut,
    AttendanceOverTime,
    OvertimeRequest,
)
from base.models import (
    CheckInLocation,
    CheckInPolicy,
    Company,
    Department,
    EmployeeShift,
    OfficeWifi,
)
from employee.models import Employee, EmployeeWorkInformation
from employee.services import account_deletion
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_auth.models import JoydigiUser

URL = "/employee/employee-delete/{}/"


class SingleDeleteBase(TestCase):
    def setUp(self):
        self.client = Client()
        self.company = make_company("Single Delete Co")
        self.admin_user = make_user("sd_admin", password="secret123")
        self.admin = make_employee(
            company=self.company,
            email="sd_admin@test.joydigi",
            user=self.admin_user,
        )
        self.admin_user.user_permissions.add(
            Permission.objects.get(
                codename="delete_employee", content_type__app_label="employee"
            )
        )
        self.client.login(username="sd_admin", password="secret123")
        session = self.client.session
        session["selected_company"] = str(self.company.id)
        session.save()

    def employee(self, slug, company=None, **user_kwargs):
        user = make_user(slug, password="secret123", **user_kwargs)
        return make_employee(
            company=company or self.company,
            email=f"{slug}@test.joydigi",
            user=user,
        )

    def delete(self, employee_id):
        return self.client.post(URL.format(employee_id), {"view": "list"})


def _attendance(employee, day, **extra):
    return Attendance.objects.create(
        employee_id=employee,
        attendance_date=day,
        attendance_clock_in_date=day,
        attendance_clock_in=time(9, 0),
        **extra,
    )


class DeleteSucceedsTests(SingleDeleteBase):
    """TESTS A, B, C, F — the row action can now delete an employee who has
    actually worked, which is the whole point."""

    def test_employee_with_no_history(self):
        """TEST A"""
        target = self.employee("sd_clean")
        user_pk = target.employee_user_id.pk

        response = self.delete(target.id)

        self.assertIn(response.status_code, (200, 302))
        self.assertFalse(Employee.objects.filter(pk=target.pk).exists())
        self.assertFalse(
            JoydigiUser.objects.filter(pk=user_pk).exists(),
            msg="a login-capable orphan user was left behind",
        )

    def test_employee_with_attendance(self):
        """TEST B — this is the case the old row action always refused."""
        target = self.employee("sd_worker")
        user_pk = target.employee_user_id.pk
        _attendance(target, date(2026, 1, 5))
        _attendance(target, date(2026, 1, 6))

        self.delete(target.id)

        self.assertFalse(Employee.objects.filter(pk=target.pk).exists())
        self.assertFalse(JoydigiUser.objects.filter(pk=user_pk).exists())
        self.assertFalse(
            Attendance.objects.filter(employee_id_id=target.id).exists()
        )

    def test_employee_with_requests_and_overtime(self):
        """TEST C"""
        target = self.employee("sd_ot")
        AttendanceActivity.objects.create(
            employee_id=target,
            attendance_date=date(2026, 2, 2),
            clock_in_date=date(2026, 2, 2),
            clock_in=time(8, 30),
        )
        AttendanceOverTime.objects.create(employee_id=target, month="february")
        OvertimeRequest.objects.create(
            employee_id=target,
            request_date=date(2026, 2, 3),
            start_time=time(18, 0),
            end_time=time(20, 0),
        )

        self.delete(target.id)

        self.assertFalse(Employee.objects.filter(pk=target.pk).exists())
        for model in (AttendanceActivity, AttendanceOverTime, OvertimeRequest):
            self.assertFalse(
                model.objects.filter(employee_id_id=target.id).exists(),
                msg=f"{model.__name__} rows survived",
            )

    def test_do_nothing_relation_leaves_no_dangling_fk(self):
        """TEST F"""
        target = self.employee("sd_late")
        attendance = _attendance(target, date(2026, 3, 1))
        # Not objects.create(): that model's save() replays force_insert. A
        # pre-existing defect in attendance.models, out of scope here.
        late = AttendanceLateComeEarlyOut(
            employee_id=target, attendance_id=attendance, type="late_come"
        )
        late.save()

        self.delete(target.id)

        live = set(Employee.objects.values_list("id", flat=True))
        orphans = [
            row
            for row in AttendanceLateComeEarlyOut.objects.values_list(
                "employee_id_id", flat=True
            )
            if row is not None and row not in live
        ]
        self.assertEqual(orphans, [], msg="dangling employee_id left behind")


class CrossEmployeeTests(SingleDeleteBase):
    """TESTS D, E — deleting one employee must not damage another."""

    def test_subordinate_survives_and_is_detached(self):
        """TEST D"""
        manager = self.employee("sd_manager")
        subordinate = self.employee("sd_report")
        EmployeeWorkInformation.objects.filter(
            employee_id=subordinate
        ).update(reporting_manager_id=manager)

        self.delete(manager.id)

        self.assertFalse(Employee.objects.filter(pk=manager.pk).exists())
        self.assertTrue(Employee.objects.filter(pk=subordinate.pk).exists())
        info = EmployeeWorkInformation.objects.get(employee_id=subordinate)
        self.assertIsNone(
            info.reporting_manager_id,
            msg="the subordinate must be detached, not deleted",
        )

    def test_another_employees_attendance_survives_its_approver(self):
        """TEST E — ``employee_id`` is ownership, ``approved_by`` is not."""
        approver = self.employee("sd_approver")
        worker = self.employee("sd_worked")
        record = _attendance(worker, date(2026, 4, 1), approved_by=approver)

        self.delete(approver.id)

        record.refresh_from_db()
        self.assertTrue(Employee.objects.filter(pk=worker.pk).exists())
        self.assertEqual(record.employee_id_id, worker.id)
        self.assertIsNone(record.approved_by)

    def test_the_subordinates_work_information_row_is_kept(self):
        """The row itself must survive, not just the employee — it carries
        their job position, department and shift."""
        manager = self.employee("sd_mgr2")
        subordinate = self.employee("sd_sub2")
        department = Department.objects.create(department="Kept Dept")
        EmployeeWorkInformation.objects.filter(
            employee_id=subordinate
        ).update(reporting_manager_id=manager, department_id=department)

        self.delete(manager.id)

        info = EmployeeWorkInformation.objects.filter(
            employee_id_id=subordinate.id
        ).first()
        self.assertIsNotNone(info, msg="the work-information row was deleted")
        self.assertEqual(info.department_id_id, department.id)


class ProtectionTests(SingleDeleteBase):
    """TESTS G, H, I, J — the account rules the row action previously had
    none of."""

    def test_deleting_yourself_is_refused(self):
        """TEST G"""
        response = self.delete(self.admin.id)

        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(Employee.objects.filter(pk=self.admin.pk).exists())
        self.assertTrue(
            JoydigiUser.objects.filter(pk=self.admin_user.pk).exists()
        )

    def test_deleting_a_superuser_is_refused(self):
        """TEST H"""
        target = self.employee("sd_root", is_superuser=True)

        self.delete(target.id)

        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())

    def test_an_employee_outside_company_scope_is_refused(self):
        """TEST I — the id comes from a URL and can be typed by hand."""
        other_company = make_company(
            "Other Single Co", address="9 Elsewhere", city="SF", zip="94105"
        )
        outsider = self.employee("sd_outsider", company=other_company)

        self.delete(outsider.id)

        self.assertTrue(Employee.objects.filter(pk=outsider.pk).exists())

    def test_a_user_without_the_permission_is_refused(self):
        """TEST J"""
        target = self.employee("sd_guarded")
        make_employee(
            company=self.company,
            email="sd_nopriv@test.joydigi",
            user=make_user("sd_nopriv", password="secret123"),
        )
        self.client.logout()
        self.client.login(username="sd_nopriv", password="secret123")

        response = self.delete(target.id)

        self.assertNotEqual(response.status_code, 200)
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())

    def test_get_cannot_delete(self):
        target = self.employee("sd_get")

        response = self.client.get(URL.format(target.id))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())


class TransactionTests(SingleDeleteBase):
    """TEST K — a failure part-way must leave nothing half-deleted."""

    def test_a_failure_during_account_removal_rolls_everything_back(self):
        target = self.employee("sd_rollback")
        user_pk = target.employee_user_id.pk
        _attendance(target, date(2026, 5, 1))
        AttendanceOverTime.objects.create(employee_id=target, month="may")

        # Fails at the very last step, after the owned rows have already been
        # deleted inside the transaction — the exact shape of partial damage
        # this phase has to rule out.
        with mock.patch.object(
            account_deletion,
            "_delete_accounts",
            side_effect=ProtectedError("boom", set()),
        ):
            self.delete(target.id)

        self.assertTrue(
            Employee.objects.filter(pk=target.pk).exists(),
            msg="employee was deleted despite the failure",
        )
        self.assertTrue(JoydigiUser.objects.filter(pk=user_pk).exists())
        self.assertEqual(
            Attendance.objects.filter(employee_id_id=target.id).count(),
            1,
            msg="owned rows were not restored by the rollback",
        )
        self.assertEqual(
            AttendanceOverTime.objects.filter(employee_id_id=target.id).count(),
            1,
        )

    def test_a_detach_failure_also_rolls_back(self):
        manager = self.employee("sd_tx_mgr")
        subordinate = self.employee("sd_tx_sub")
        EmployeeWorkInformation.objects.filter(
            employee_id=subordinate
        ).update(reporting_manager_id=manager)

        with mock.patch.object(
            account_deletion,
            "_delete_owned",
            side_effect=ProtectedError("boom", set()),
        ):
            self.delete(manager.id)

        self.assertTrue(Employee.objects.filter(pk=manager.pk).exists())
        info = EmployeeWorkInformation.objects.get(employee_id=subordinate)
        self.assertEqual(
            info.reporting_manager_id_id,
            manager.id,
            msg="the detach was not rolled back",
        )


class SharedDataTests(SingleDeleteBase):
    """TEST L"""

    def test_company_configuration_survives(self):
        department = Department.objects.create(department="Single Dept")
        shift = EmployeeShift.objects.create(employee_shift="Single Shift")
        location = CheckInLocation.objects.create(
            company_id=self.company, name="HQ", latitude=10, longitude=106
        )
        policy = CheckInPolicy.objects.create(company_id=self.company)
        wifi = OfficeWifi.objects.create(
            company_id=self.company, name="Office", ssid="joydigi"
        )
        target = self.employee("sd_shared")
        EmployeeWorkInformation.objects.filter(employee_id=target).update(
            department_id=department, shift_id=shift
        )
        _attendance(target, date(2026, 7, 1))

        self.delete(target.id)

        self.assertFalse(Employee.objects.filter(pk=target.pk).exists())
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())
        self.assertTrue(Department.objects.filter(pk=department.pk).exists())
        self.assertTrue(EmployeeShift.objects.filter(pk=shift.pk).exists())
        self.assertTrue(CheckInLocation.objects.filter(pk=location.pk).exists())
        self.assertTrue(CheckInPolicy.objects.filter(pk=policy.pk).exists())
        self.assertTrue(OfficeWifi.objects.filter(pk=wifi.pk).exists())


class OneImplementationTests(TestCase):
    """What actually keeps the two paths aligned: neither view may hold
    deletion logic of its own."""

    def setUp(self):
        # Executable code only. An earlier version of these tests read the raw
        # text and failed on the docstring that *describes* the old
        # ``user.delete()`` — the assertion has to be about what the view
        # does, not about what its prose mentions. `ast.unparse` drops
        # comments, and the docstring node is removed explicitly.
        import ast

        with open("employee/views.py", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        def code_of(func_name):
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.FunctionDef)
                    and node.name == func_name
                ):
                    body = list(node.body)
                    if (
                        body
                        and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)
                    ):
                        body = body[1:]
                    return "\n".join(ast.unparse(stmt) for stmt in body)
            raise AssertionError(f"{func_name} not found in employee/views.py")

        self.tree = tree
        self.single = code_of("employee_delete")
        self.bulk = code_of("employee_bulk_delete")

    def test_neither_view_deletes_a_user_or_employee_itself(self):
        for name, body in (("single", self.single), ("bulk", self.bulk)):
            for forbidden in (
                "user.delete()",
                "employee.delete()",
                ".contract_set",
            ):
                self.assertNotIn(
                    forbidden, body, msg=f"{forbidden} still in {name} view"
                )

    def test_both_views_go_through_the_service(self):
        for name, body in (("single", self.single), ("bulk", self.bulk)):
            self.assertIn("account_deletion.validate(", body, msg=name)
            self.assertIn("account_deletion.delete_employees(", body, msg=name)

    def test_no_relation_list_is_restated_in_the_deletion_views(self):
        """The classification is derived once, in the service.

        Scoped to the two deletion views on purpose. ``employee/views.py`` is
        a large module that legitimately mentions ``reporting_manager_id``
        (manager notifications) and imports ``RotatingShiftAssign`` for
        unrelated features — a module-wide ban would fail on code that has
        nothing to do with deletion. What must stay true is that *these two
        views* name no relation of their own.
        """
        for name, body in (("single", self.single), ("bulk", self.bulk)):
            for relation in (
                "AttendanceLateComeEarlyOut",
                "reporting_manager_id",
                "approved_by",
                "RotatingShiftAssign",
                "AttendanceOverTime",
                "PenaltyAccounts",
            ):
                self.assertNotIn(
                    relation,
                    body,
                    msg=f"{relation} is named by the {name} delete view",
                )

    def test_the_single_path_uses_the_same_transaction(self):
        """Atomicity comes from the service, so single delete cannot acquire
        a weaker guarantee than bulk delete by accident."""
        self.assertTrue(
            hasattr(account_deletion.delete_employees, "__wrapped__"),
            msg="delete_employees is no longer wrapped in transaction.atomic",
        )
