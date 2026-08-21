from datetime import date

from django.contrib.auth.models import Group
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from attendance.models import Attendance
from base.models import CompanyGroupAssignment
from base.roles import EMPLOYEE_ROLE, LEADER_ROLE
from joydigi.joydigi_middlewares import _thread_locals, set_selected_company
from joydigi.testkit import make_company, make_employee, make_user


class EmployeeDefaultPermissionsTests(TestCase):
    def setUp(self):
        _thread_locals.request = None
        set_selected_company(None)
        self.company = make_company("Employee Permission Co")
        self.user = make_user("default_employee", password="123456")
        self.employee = make_employee(
            company=self.company,
            email="default-employee@test.joydigi",
            user=self.user,
        )
        # Test factory updates work information in bulk; save once to exercise
        # the same role-assignment signal used by the real employee form.
        work_info = self.employee.employee_work_info
        work_info.company_id = self.company
        work_info.save(update_fields=["company_id"])

    def tearDown(self):
        _thread_locals.request = None
        set_selected_company(None)

    def test_new_employee_gets_company_role_and_safe_self_permissions(self):
        employee_group = Group.objects.get(name=EMPLOYEE_ROLE)

        self.assertTrue(self.user.groups.filter(pk=employee_group.pk).exists())
        self.assertTrue(
            CompanyGroupAssignment.objects.filter(
                user=self.user,
                company=self.company,
                group=employee_group,
            ).exists()
        )

        expected = {
            "view_ownprofile",
            "change_ownprofile",
            "clock_in_out",
            "view_own_attendance",
            "view_own_leave_request",
            "add_own_leave_request",
            "change_own_leave_request",
        }
        self.assertTrue(
            expected.issubset(
                set(employee_group.permissions.values_list("codename", flat=True))
            )
        )
        self.assertFalse(employee_group.permissions.filter(codename="view_employee").exists())
        self.assertFalse(employee_group.permissions.filter(codename="view_attendance").exists())
        self.assertTrue(
            expected.issubset(
                set(
                    Group.objects.get(name=LEADER_ROLE).permissions.values_list(
                        "codename", flat=True
                    )
                )
            )
        )

    def test_employee_home_and_sidebar_only_show_personal_features(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("home-page"))
        self.assertRedirects(
            response,
            reverse("view-my-attendance"),
            fetch_redirect_response=False,
        )

        page = self.client.get(reverse("view-my-attendance"))
        menu_html = render_to_string(
            "joydigi_theme/components/sidebar/top_menu.html",
            request=page.wsgi_request,
        )
        self.assertIn(reverse("view-my-attendance"), menu_html)
        self.assertIn(reverse("user-request-view"), menu_html)
        self.assertIn(reverse("bulletin"), menu_html)
        self.assertNotIn(reverse("dashboard"), menu_html)
        self.assertNotIn(reverse("approval-hub"), menu_html)
        self.assertNotIn(reverse("settings"), menu_html)

        mobile_menu_html = render_to_string(
            "joydigi_theme/components/mobile_bottom_menu.html",
            request=page.wsgi_request,
        )
        self.assertIn(reverse("view-my-attendance"), mobile_menu_html)
        self.assertIn(reverse("user-request-view"), mobile_menu_html)
        self.assertIn(reverse("bulletin"), mobile_menu_html)
        self.assertIn(reverse("employee-profile"), mobile_menu_html)
        self.assertNotIn(reverse("dashboard"), mobile_menu_html)
        self.assertNotIn(reverse("approval-hub"), mobile_menu_html)
        self.assertNotIn(reverse("settings"), mobile_menu_html)

    def test_employee_cannot_open_another_employees_attendance_detail(self):
        coworker = make_employee(
            company=self.company,
            email="coworker@test.joydigi",
        )
        other_attendance = Attendance.objects.create(
            employee_id=coworker,
            attendance_date=date(2026, 8, 20),
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("my-attendance-detail", kwargs={"pk": other_attendance.pk}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("HX-Redirect"), "/")
        self.assertNotContains(response, coworker.get_full_name())
