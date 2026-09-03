"""Phase EMPLOYEE-CBV-SAFE-DELETE-1 — the live employee page must not destroy
other employees' records.

The bug this file exists for: ``/employee/employee-view/`` did not use the safe
delete endpoint. Its trash button pointed at ``generic-delete``, whose
confirmation view deletes everything it collects — including the objects the
schema marks ``PROTECT``. For an ``Employee`` those protected objects belong to
*other people*: a subordinate's ``EmployeeWorkInformation`` and any
``Attendance`` the departing employee had merely approved. A scratch-database
probe deleted both.

``ManagerDeletionThroughTheActiveEndpointTests`` reproduces exactly that setup
and drives the endpoint the page now calls, rather than calling the service
directly — a test that bypassed the endpoint would have passed against the
broken wiring too.
"""

import json
import re
from datetime import date, time

from django.contrib.auth.models import Group, Permission
from django.db.models import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse
from unittest import mock

from attendance.models import Attendance, AttendanceOverTime
from base.models import (
    CheckInLocation,
    CheckInPolicy,
    Company,
    CompanyGroupAssignment,
    Department,
    EmployeeShift,
    OfficeWifi,
)
from base.roles import ADMIN_ROLE, ensure_standard_roles
from employee.models import Employee, EmployeeWorkInformation
from employee.services import account_deletion
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_auth.models import JoydigiUser

CONFIRM_URL = "/employee/employee-delete-confirmation/"


class CbvDeleteBase(TestCase):
    """An administrator who can actually reach the employee page.

    The page is gated by ``checkin_admin_required`` on top of the model
    permission, so the group assignment below is what a real administrator
    has — without it the page answers 403 and the wiring assertions would
    pass vacuously.
    """

    def setUp(self):
        self.client = Client()
        self.company = make_company("CBV Delete Co")
        self.admin_user = make_user("cbv_admin", password="secret123")
        self.admin = make_employee(
            company=self.company,
            email="cbv_admin@test.joydigi",
            user=self.admin_user,
        )
        for codename in ("delete_employee", "view_employee", "change_employee"):
            self.admin_user.user_permissions.add(
                Permission.objects.get(
                    codename=codename, content_type__app_label="employee"
                )
            )
        ensure_standard_roles()
        admin_group = Group.objects.get(name=ADMIN_ROLE)
        self.admin_user.groups.add(admin_group)
        CompanyGroupAssignment.objects.get_or_create(
            user=self.admin_user, company=self.company, group=admin_group
        )
        self.client.login(username="cbv_admin", password="secret123")
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

    def open_confirmation(self, employee_id):
        return self.client.get(CONFIRM_URL, {"pk": employee_id})

    def confirm_delete(self, employee_id):
        return self.client.post(
            f"{CONFIRM_URL}?pk={employee_id}", {"confirm": "on"}
        )


def _attendance(employee, day, **extra):
    return Attendance.objects.create(
        employee_id=employee,
        attendance_date=day,
        attendance_clock_in_date=day,
        attendance_clock_in=time(9, 0),
        **extra,
    )


class ManagerDeletionThroughTheActiveEndpointTests(CbvDeleteBase):
    """The mandatory regression test — the exact shape the probe destroyed."""

    def setUp(self):
        super().setUp()
        self.manager = self.employee("cbv_manager")
        self.manager_user_pk = self.manager.employee_user_id.pk
        self.subordinate = self.employee("cbv_subordinate")
        self.department = Department.objects.create(department="CBV Dept")
        EmployeeWorkInformation.objects.filter(
            employee_id=self.subordinate
        ).update(
            reporting_manager_id=self.manager, department_id=self.department
        )
        self.sub_attendance = _attendance(
            self.subordinate, date(2026, 3, 3), approved_by=self.manager
        )

    def test_deleting_the_manager_leaves_the_subordinate_intact(self):
        response = self.confirm_delete(self.manager.id)

        # Phase EMPLOYEE-DELETE-REDIRECT-FIX-1: a successful delete now sends
        # the caller to the list instead of answering in place, so the browser
        # cannot be left on the deleted employee's URL.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("employee-view"))

        # A: gone, account and all.
        self.assertFalse(Employee.objects.filter(pk=self.manager.pk).exists())
        self.assertFalse(
            JoydigiUser.objects.filter(pk=self.manager_user_pk).exists(),
            msg="a login-capable orphan user was left behind",
        )

        # B: everything of theirs survives, detached rather than destroyed.
        self.assertTrue(
            Employee.objects.filter(pk=self.subordinate.pk).exists()
        )
        info = EmployeeWorkInformation.objects.filter(
            employee_id_id=self.subordinate.id
        ).first()
        self.assertIsNotNone(
            info,
            msg="the subordinate's work information row was deleted — this is "
            "the exact defect this phase fixes",
        )
        self.assertIsNone(info.reporting_manager_id)
        self.assertEqual(
            info.department_id_id,
            self.department.id,
            msg="the subordinate lost their department",
        )

        attendance = Attendance.objects.filter(
            pk=self.sub_attendance.pk
        ).first()
        self.assertIsNotNone(
            attendance,
            msg="the subordinate's attendance was deleted because the manager "
            "had approved it",
        )
        self.assertIsNone(attendance.approved_by)
        self.assertEqual(attendance.employee_id_id, self.subordinate.id)

    def test_the_confirmation_body_warns_before_anything_is_deleted(self):
        response = self.open_confirmation(self.manager.id)
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Xóa vĩnh viễn tài khoản nhân viên này?", body)
        self.assertIn("sẽ bị xóa vĩnh viễn", body)
        self.assertIn("cấu hình dùng chung sẽ không bị xóa", body)
        # A GET must never delete.
        self.assertTrue(Employee.objects.filter(pk=self.manager.pk).exists())


class RedirectAfterDeleteTests(CbvDeleteBase):
    """Phase EMPLOYEE-DELETE-REDIRECT-FIX-1.

    Opening an employee pushes ``/employee/employee-view/<id>/`` into the
    address bar. Deleting from there used to refresh only the list fragment,
    so the URL kept naming the row that had just been removed and a plain F5
    answered 404. These tests follow that exact sequence.
    """

    def setUp(self):
        super().setUp()
        self.target = self.employee("cbv_redirect_a")
        self.bystander = self.employee("cbv_redirect_b")
        self.individual_url = f"/employee/employee-view/{self.target.id}/"
        self.list_url = reverse("employee-view")

    def test_the_individual_page_is_reachable_before_the_delete(self):
        """Establishes the starting state the bug depended on."""
        response = self.client.get(self.individual_url)
        self.assertEqual(response.status_code, 200)

    def test_an_htmx_delete_tells_the_browser_to_navigate_to_the_list(self):
        response = self.client.post(
            f"{CONFIRM_URL}?pk={self.target.id}",
            {"confirm": "on"},
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers.get("HX-Redirect"), self.list_url)
        self.assertNotIn(
            str(self.target.id),
            response.headers.get("HX-Redirect", ""),
            msg="the redirect still carries the deleted employee id",
        )

    def test_a_plain_post_redirects_to_the_list(self):
        response = self.client.post(
            f"{CONFIRM_URL}?pk={self.target.id}", {"confirm": "on"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.list_url)

    def test_refreshing_the_destination_returns_200_not_404(self):
        """The whole point: the URL the browser ends on must still work."""
        self.client.post(
            f"{CONFIRM_URL}?pk={self.target.id}",
            {"confirm": "on"},
            headers={"hx-request": "true"},
        )

        refreshed = self.client.get(self.list_url)

        self.assertEqual(refreshed.status_code, 200)
        self.assertFalse(Employee.objects.filter(pk=self.target.pk).exists())

    def test_the_old_individual_url_is_what_used_to_break(self):
        """Kept as a statement of the failure being avoided: that URL does
        404 after the delete, which is why the browser must not stay on it."""
        self.client.post(
            f"{CONFIRM_URL}?pk={self.target.id}",
            {"confirm": "on"},
            headers={"hx-request": "true"},
        )

        stale = self.client.get(self.individual_url)

        self.assertEqual(stale.status_code, 404)

    def test_the_list_after_deletion_drops_a_and_keeps_b(self):
        self.client.post(
            f"{CONFIRM_URL}?pk={self.target.id}",
            {"confirm": "on"},
            headers={"hx-request": "true"},
        )

        listing = self.client.get(
            "/employee/employees-list/", headers={"hx-request": "true"}
        )
        body = listing.content.decode()

        self.assertEqual(listing.status_code, 200)
        select_ids = re.search(r'data-select-ids="([^"]*)"', body)
        self.assertIsNotNone(select_ids)
        offered = json.loads(select_ids.group(1))
        self.assertNotIn(self.target.id, offered)
        self.assertIn(self.bystander.id, offered)

    def test_a_refused_delete_does_not_redirect(self):
        """Only a successful deletion navigates away; a refusal has to stay
        in the modal so the operator can read why."""
        response = self.client.post(
            f"{CONFIRM_URL}?pk={self.admin.id}",
            {"confirm": "on"},
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("HX-Redirect", response.headers)
        self.assertIn("Không thể tự xóa", response.content.decode())


class OwnedDataTests(CbvDeleteBase):
    def test_owned_history_goes_and_the_employee_can_be_deleted_at_all(self):
        target = self.employee("cbv_worker")
        user_pk = target.employee_user_id.pk
        _attendance(target, date(2026, 1, 5))
        AttendanceOverTime.objects.create(employee_id=target, month="january")

        self.confirm_delete(target.id)

        self.assertFalse(Employee.objects.filter(pk=target.pk).exists())
        self.assertFalse(JoydigiUser.objects.filter(pk=user_pk).exists())
        self.assertFalse(
            Attendance.objects.filter(employee_id_id=target.id).exists()
        )
        self.assertFalse(
            AttendanceOverTime.objects.filter(employee_id_id=target.id).exists()
        )

    def test_shared_configuration_survives(self):
        department = Department.objects.create(department="CBV Shared Dept")
        shift = EmployeeShift.objects.create(employee_shift="CBV Shift")
        location = CheckInLocation.objects.create(
            company_id=self.company, name="HQ", latitude=10, longitude=106
        )
        policy = CheckInPolicy.objects.create(company_id=self.company)
        wifi = OfficeWifi.objects.create(
            company_id=self.company, name="Office", ssid="joydigi"
        )
        target = self.employee("cbv_shared")
        EmployeeWorkInformation.objects.filter(employee_id=target).update(
            department_id=department, shift_id=shift
        )
        _attendance(target, date(2026, 7, 1))

        self.confirm_delete(target.id)

        self.assertFalse(Employee.objects.filter(pk=target.pk).exists())
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())
        self.assertTrue(Department.objects.filter(pk=department.pk).exists())
        self.assertTrue(EmployeeShift.objects.filter(pk=shift.pk).exists())
        self.assertTrue(CheckInLocation.objects.filter(pk=location.pk).exists())
        self.assertTrue(CheckInPolicy.objects.filter(pk=policy.pk).exists())
        self.assertTrue(OfficeWifi.objects.filter(pk=wifi.pk).exists())


class ProtectionTests(CbvDeleteBase):
    """The backend enforces these, not the template that hides buttons."""

    def test_cannot_delete_yourself(self):
        response = self.confirm_delete(self.admin.id)

        self.assertIn("Không thể tự xóa", response.content.decode())
        self.assertTrue(Employee.objects.filter(pk=self.admin.pk).exists())

    def test_cannot_delete_a_superuser(self):
        target = self.employee("cbv_root", is_superuser=True)

        response = self.confirm_delete(target.id)

        self.assertIn("quản trị cấp cao", response.content.decode())
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())

    def test_cannot_delete_outside_company_scope(self):
        other_company = make_company(
            "CBV Other Co", address="9 Elsewhere", city="SF", zip="94105"
        )
        outsider = self.employee("cbv_outsider", company=other_company)

        response = self.confirm_delete(outsider.id)

        self.assertIn("Không tìm thấy", response.content.decode())
        self.assertTrue(Employee.objects.filter(pk=outsider.pk).exists())

    def test_a_missing_pk_is_refused(self):
        response = self.client.get(CONFIRM_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Chưa chọn nhân viên nào", response.content.decode())

    def test_permission_is_required(self):
        target = self.employee("cbv_guarded")
        make_employee(
            company=self.company,
            email="cbv_nopriv@test.joydigi",
            user=make_user("cbv_nopriv", password="secret123"),
        )
        self.client.logout()
        self.client.login(username="cbv_nopriv", password="secret123")

        response = self.confirm_delete(target.id)

        self.assertNotEqual(response.status_code, 200)
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())


class TransactionTests(CbvDeleteBase):
    def test_a_failure_rolls_the_whole_deletion_back(self):
        manager = self.employee("cbv_tx_manager")
        subordinate = self.employee("cbv_tx_sub")
        EmployeeWorkInformation.objects.filter(
            employee_id=subordinate
        ).update(reporting_manager_id=manager)
        _attendance(manager, date(2026, 5, 1))

        with mock.patch.object(
            account_deletion,
            "_delete_accounts",
            side_effect=ProtectedError("boom", set()),
        ):
            response = self.confirm_delete(manager.id)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(Employee.objects.filter(pk=manager.pk).exists())
        self.assertEqual(
            Attendance.objects.filter(employee_id_id=manager.id).count(),
            1,
            msg="owned rows were not restored by the rollback",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=subordinate)
        self.assertEqual(
            info.reporting_manager_id_id,
            manager.id,
            msg="the detach was not rolled back",
        )


class UiWiringTests(CbvDeleteBase):
    """The wiring itself, rendered — not the source read as text.

    A source-level assertion would still pass if the template stopped being
    the one the page renders.
    """

    def setUp(self):
        super().setUp()
        self.target = self.employee("cbv_rendered")

    def test_the_employee_page_loads(self):
        response = self.client.get("/employee/employee-view/")
        self.assertEqual(response.status_code, 200)

    def test_the_rendered_row_actions_no_longer_point_at_generic_delete(self):
        response = self.client.get(
            "/employee/employees-list/", headers={"hx-request": "true"}
        )
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("model=employee.Employee", body)
        self.assertIn("employee-delete-confirmation", body)

    def test_the_row_action_template_targets_the_safe_endpoint(self):
        with open(
            "employee/templates/cbv/employees_view/employee_actions.html",
            encoding="utf-8",
        ) as handle:
            markup = handle.read()
        self.assertIn("employee-delete-confirmation", markup)
        self.assertNotIn("generic-delete", markup)

    def test_the_card_action_uses_the_employee_delete_url(self):
        """The card view builds its action from ``get_delete_url``."""
        self.assertEqual(
            str(self.target.get_delete_url()),
            reverse("employee-delete-confirmation"),
        )

    def test_no_employee_delete_control_references_the_generic_endpoint(self):
        import ast

        with open("employee/cbv/employees.py", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        for literal in literals:
            self.assertNotIn(
                "model=employee.Employee",
                literal,
                msg="a card/list action still dispatches Employee through "
                "the generic delete view",
            )


class GenericDeleteIsolationTests(CbvDeleteBase):
    """Section 12 — the fix must be Employee-only."""

    def test_the_generic_endpoint_still_exists(self):
        self.assertEqual(reverse("generic-delete"), "/generic-delete/")

    def test_an_unrelated_model_still_uses_generic_delete(self):
        department = Department.objects.create(department="Isolation Dept")
        self.assertEqual(
            str(department.get_delete_url()), reverse("generic-delete")
        )

    def test_other_models_templates_are_untouched(self):
        for path, marker in (
            ("leave/templates/cbv/leave_types/leave_type_list_actions.html",
             "model=leave.LeaveType"),
            ("base/templates/cbv/settings/job_role.html", "model=base.jobrole"),
            ("attendance/templates/cbv/settings/grace_time_default_action.html",
             "model=attendance.gracetime"),
        ):
            with open(path, encoding="utf-8") as handle:
                markup = handle.read()
            self.assertIn("generic-delete", markup, msg=path)
            self.assertIn(marker, markup, msg=path)

    def test_only_employee_was_moved_off_the_generic_url(self):
        """Every other model that declares ``get_delete_url`` keeps it.

        Unsaved instances on purpose: ``get_delete_url`` reads nothing from the
        row, and constructing real ones would drag in each model's required
        foreign keys for no gain.
        """
        from base.models import (
            Company as CompanyModel,
            EmployeeType,
            RotatingShift,
            RotatingWorkType,
            WorkType,
        )
        from employee.models import Actiontype
        from leave.models import LeaveType

        for model_cls in (
            CompanyModel,
            Department,
            WorkType,
            RotatingWorkType,
            RotatingShift,
            EmployeeType,
            EmployeeShift,
            LeaveType,
            Actiontype,
        ):
            self.assertEqual(
                str(model_cls().get_delete_url()),
                reverse("generic-delete"),
                msg=f"{model_cls.__name__} was moved off generic-delete",
            )

    def test_employee_is_the_only_model_pointed_at_the_new_endpoint(self):
        self.assertEqual(
            str(Employee().get_delete_url()),
            reverse("employee-delete-confirmation"),
        )


class OneImplementationTests(TestCase):
    """No third deletion engine was introduced by the adapter."""

    def setUp(self):
        import ast

        with open("employee/views.py", encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "employee_delete_confirmation"
            ):
                body = list(node.body)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                ):
                    body = body[1:]
                self.code = "\n".join(ast.unparse(stmt) for stmt in body)
                return
        raise AssertionError("employee_delete_confirmation not found")

    def test_the_adapter_holds_no_deletion_logic_of_its_own(self):
        for forbidden in (
            "user.delete()",
            "employee.delete()",
            "employee_user_id.delete()",
            "NestedObjects",
            "collector",
        ):
            self.assertNotIn(forbidden, self.code, msg=forbidden)

    def test_the_adapter_delegates_to_the_service(self):
        self.assertIn("account_deletion.validate(", self.code)
        self.assertIn("account_deletion.delete_employees(", self.code)
        self.assertIn("account_deletion.preview(", self.code)

    def test_the_adapter_restates_no_relation_names(self):
        for relation in (
            "AttendanceLateComeEarlyOut",
            "reporting_manager_id",
            "approved_by",
            "RotatingShiftAssign",
        ):
            self.assertNotIn(relation, self.code, msg=relation)
