"""Phase LEAVE-7A.5 (assign-leave modal redesign).

Covers the "Chỉ định nghỉ phép" modal (`assign-one/<pk>/` ->
`LeaveTypeAssignForm`): that it renders, is permission-gated, is
company-scoped, shows employee name + code, and — critically — that the
existing submit payload (`employee_id` repeated + `leave_days_<id>`)
and the existing AvailableLeave allocation behaviour are unchanged by
the UI redesign.
"""

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from employee.models import Employee
from joydigi.testkit import make_company, make_employee, make_user
from leave.models import AvailableLeave, LeaveType


class AssignLeaveModalBaseTests(TestCase):
    def setUp(self):
        self.company = make_company("Assign Modal Co")
        self.admin = make_user("assign_admin", is_superuser=True)
        make_employee(
            company=self.company,
            email="assign_admin@test.joydigi",
            user=self.admin,
        )
        self.leave_type = LeaveType.objects.create(
            name="Nghỉ phép năm", total_days=12
        )
        self.emp_a = make_employee(
            company=self.company,
            email="bao.tran@test.joydigi",
            first_name="Bảo Trân",
            last_name="Huỳnh",
        )
        self.emp_b = make_employee(
            company=self.company,
            email="gia.bao@test.joydigi",
            first_name="Gia Bảo",
            last_name="Võ",
        )
        Employee.objects.filter(pk=self.emp_a.pk).update(badge_id="JD013")
        Employee.objects.filter(pk=self.emp_b.pk).update(badge_id="JD006")
        self.emp_a.refresh_from_db()
        self.emp_b.refresh_from_db()
        self.url = reverse("assign-one", kwargs={"pk": self.leave_type.pk})
        self.client = Client()

    def _get_modal(self):
        return self.client.get(self.url, HTTP_HX_REQUEST="true")


class AssignLeaveModalRenderTests(AssignLeaveModalBaseTests):
    def test_authorized_admin_sees_modal(self):
        self.client.force_login(self.admin)
        response = self._get_modal()
        self.assertEqual(response.status_code, 200)

    def test_unauthorized_user_denied(self):
        """`permission_required` (leave.view_leavetype) renders
        `decorator_404.html` for HTMX requests — HTTP 200 with a
        permission-denied body rather than a 403, which is this app's
        pre-existing convention. Assert on the body, not the status."""
        user = make_user("assign_denied")
        make_employee(
            company=self.company, email="denied@test.joydigi", user=user
        )
        self.client.force_login(user)
        html = self._get_modal().content.decode()
        # Language-agnostic: the permission-denied page uses this class.
        self.assertIn("oh-404__title", html)
        # The security property that actually matters — no modal, no
        # employee picker, no employee data leaked.
        self.assertNotIn("Lưu chỉ định", html)
        self.assertNotIn('name="employee_id"', html)
        self.assertNotIn("JD013", html)

    def test_employee_name_and_code_displayed(self):
        self.client.force_login(self.admin)
        html = self._get_modal().content.decode()
        self.assertIn("Bảo Trân", html)
        self.assertIn("JD013", html)
        self.assertIn("Gia Bảo", html)
        self.assertIn("JD006", html)

    def test_modal_has_cancel_and_save_buttons(self):
        self.client.force_login(self.admin)
        html = self._get_modal().content.decode()
        self.assertIn("Hủy", html)
        self.assertIn("Lưu chỉ định", html)

    def test_has_search_input(self):
        self.client.force_login(self.admin)
        html = self._get_modal().content.decode()
        self.assertIn("Tìm theo tên hoặc mã nhân viên", html)

    def test_employee_checkboxes_use_employee_id_name(self):
        """The redesigned picker must keep the exact field name the
        existing view reads via `request.POST.getlist('employee_id')`."""
        self.client.force_login(self.admin)
        html = self._get_modal().content.decode()
        self.assertIn('name="employee_id"', html)
        self.assertIn('type="checkbox"', html)

    def test_no_legacy_capped_select2_markup(self):
        """The old layout's root cause: a Select2-enhanced
        `<select multiple>` whose selection box was capped at 70px with
        an internal scrollbar, plus an absolutely-positioned Filter
        overlay. Neither may survive in this modal."""
        self.client.force_login(self.admin)
        html = self._get_modal().content.decode()
        self.assertNotIn("max-height: 70px", html)
        self.assertNotIn("oh-select2", html)


class AssignLeaveSubmitTests(AssignLeaveModalBaseTests):
    """The redesign must not change allocation behaviour."""

    def test_single_employee_assignment_creates_available_leave(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            self.url,
            {
                "employee_id": [str(self.emp_a.pk)],
                f"leave_days_{self.emp_a.pk}": "7",
            },
            HTTP_HX_REQUEST="true",
        )
        self.assertIn(response.status_code, (200, 302))
        avail = AvailableLeave.objects.filter(
            employee_id=self.emp_a, leave_type_id=self.leave_type
        ).first()
        self.assertIsNotNone(avail)
        self.assertEqual(avail.available_days, 7)

    def test_multiple_employees_assignment(self):
        self.client.force_login(self.admin)
        self.client.post(
            self.url,
            {
                "employee_id": [str(self.emp_a.pk), str(self.emp_b.pk)],
                f"leave_days_{self.emp_a.pk}": "5",
                f"leave_days_{self.emp_b.pk}": "9",
            },
            HTTP_HX_REQUEST="true",
        )
        a = AvailableLeave.objects.filter(
            employee_id=self.emp_a, leave_type_id=self.leave_type
        ).first()
        b = AvailableLeave.objects.filter(
            employee_id=self.emp_b, leave_type_id=self.leave_type
        ).first()
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a.available_days, 5)
        self.assertEqual(b.available_days, 9)

    def test_blank_leave_days_falls_back_to_leave_type_total(self):
        self.client.force_login(self.admin)
        self.client.post(
            self.url,
            {
                "employee_id": [str(self.emp_a.pk)],
                f"leave_days_{self.emp_a.pk}": "",
            },
            HTTP_HX_REQUEST="true",
        )
        avail = AvailableLeave.objects.filter(
            employee_id=self.emp_a, leave_type_id=self.leave_type
        ).first()
        self.assertIsNotNone(avail)
        self.assertEqual(avail.available_days, int(self.leave_type.total_days))

    def test_duplicate_assignment_does_not_create_second_row(self):
        AvailableLeave.objects.create(
            employee_id=self.emp_a,
            leave_type_id=self.leave_type,
            available_days=3,
            carryforward_days=0,
            total_leave_days=3,
        )
        self.client.force_login(self.admin)
        self.client.post(
            self.url,
            {
                "employee_id": [str(self.emp_a.pk)],
                f"leave_days_{self.emp_a.pk}": "8",
            },
            HTTP_HX_REQUEST="true",
        )
        rows = AvailableLeave.objects.filter(
            employee_id=self.emp_a, leave_type_id=self.leave_type
        )
        self.assertEqual(rows.count(), 1)
        # Untouched — the existing view skips already-assigned employees.
        self.assertEqual(rows.first().available_days, 3)

    def test_no_employee_selected_creates_nothing(self):
        self.client.force_login(self.admin)
        self.client.post(self.url, {}, HTTP_HX_REQUEST="true")
        self.assertEqual(
            AvailableLeave.objects.filter(leave_type_id=self.leave_type).count(), 0
        )


class AssignLeaveCompanyScopeTests(AssignLeaveModalBaseTests):
    def test_employee_list_is_company_scoped(self):
        other_company = make_company("Other Assign Co")
        make_employee(
            company=other_company,
            email="foreign@test.joydigi",
            first_name="Foreign",
            last_name="Person",
        )
        self.client.force_login(self.admin)
        html = self._get_modal().content.decode()
        self.assertIn("Bảo Trân", html)
        # Scoping is delegated to the existing queryset/manager — this
        # asserts the redesign did not widen it.
        self.assertNotIn("foreign@test.joydigi", html)
