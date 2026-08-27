"""Phase AUTH-6A.2: admin "Đăng xuất khỏi thiết bị" security tests.

Covers authorization scoping (manager/company), POST-only/CSRF
behavior, that the mutation only ever touches session_version, and
that the automatic UserActivityLog entry never carries a token or
password.
"""

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from rest_framework.test import APIClient

from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_audit.models import UserActivityLog

FORCE_LOGOUT_URL = "/employee/employee-force-mobile-logout/{}/"


def _grant_change_employee(user):
    perm = Permission.objects.get(
        codename="change_employee", content_type__app_label="employee"
    )
    user.user_permissions.add(perm)


def _set_manager(target_employee, manager_employee):
    EmployeeWorkInformation.objects.filter(employee_id=target_employee).update(
        reporting_manager_id=manager_employee
    )


class ForceMobileLogoutAuthorizationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.company_a = make_company("Force Logout Co A")
        self.company_b = make_company(
            "Force Logout Co B", address="2 Other St", city="SF", zip="94105"
        )

        self.manager_user = make_user("fl_manager", password="secret123")
        self.manager = make_employee(
            company=self.company_a,
            email="fl_manager@test.joydigi",
            user=self.manager_user,
        )

        self.target_user = make_user("fl_target", password="secret123")
        self.target = make_employee(
            company=self.company_a,
            email="fl_target@test.joydigi",
            user=self.target_user,
        )
        _set_manager(self.target, self.manager)

        self.other_company_user = make_user("fl_other_co", password="secret123")
        self.other_company_employee = make_employee(
            company=self.company_b,
            email="fl_other_co@test.joydigi",
            user=self.other_company_user,
        )

        self.unrelated_manager_user = make_user(
            "fl_unrelated_mgr", password="secret123"
        )
        self.unrelated_manager = make_employee(
            company=self.company_a,
            email="fl_unrelated_mgr@test.joydigi",
            user=self.unrelated_manager_user,
        )

        self.ordinary_user = make_user("fl_ordinary", password="secret123")
        make_employee(
            company=self.company_a,
            email="fl_ordinary@test.joydigi",
            user=self.ordinary_user,
        )

    def test_get_does_not_mutate(self):
        _grant_change_employee(self.manager_user)
        self.client.force_login(self.manager_user)
        before = self.target_user.session_version
        response = self.client.get(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )
        self.assertIn(response.status_code, (404, 405))
        self.target_user.refresh_from_db()
        self.assertEqual(self.target_user.session_version, before)

    def test_manager_with_change_employee_perm_can_force_logout(self):
        _grant_change_employee(self.manager_user)
        self.client.force_login(self.manager_user)
        response = self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.target_user.refresh_from_db()
        self.assertEqual(self.target_user.session_version, 1)

    def test_direct_manager_without_global_perm_can_force_logout(self):
        # No employee.change_employee permission granted — authorized
        # purely via the reporting-chain check_manager relationship.
        self.client.force_login(self.manager_user)
        response = self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.target_user.refresh_from_db()
        self.assertEqual(self.target_user.session_version, 1)

    def test_cross_company_manager_blocked_even_with_global_perm(self):
        _grant_change_employee(self.manager_user)
        self.client.force_login(self.manager_user)
        response = self.client.post(
            FORCE_LOGOUT_URL.format(self.other_company_employee.id),
            HTTP_HX_REQUEST="true",
        )
        # 403 from the explicit company check in
        # `_can_force_mobile_logout`, or 404 because the tenant-scoped
        # `Employee.objects` manager (`JoydigiCompanyManager`) already
        # excludes another company's rows from `get_object_or_404` —
        # both are a safe "blocked" outcome; either is acceptable here.
        self.assertIn(response.status_code, (403, 404))
        self.other_company_user.refresh_from_db()
        self.assertEqual(self.other_company_user.session_version, 0)

    def test_unrelated_manager_without_perm_blocked(self):
        self.client.force_login(self.unrelated_manager_user)
        response = self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 403)
        self.target_user.refresh_from_db()
        self.assertEqual(self.target_user.session_version, 0)

    def test_ordinary_employee_blocked(self):
        self.client.force_login(self.ordinary_user)
        response = self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 403)
        self.target_user.refresh_from_db()
        self.assertEqual(self.target_user.session_version, 0)

    def test_only_target_employee_affected(self):
        _grant_change_employee(self.manager_user)
        self.client.force_login(self.manager_user)
        self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )
        self.unrelated_manager_user.refresh_from_db()
        self.ordinary_user.refresh_from_db()
        self.assertEqual(self.unrelated_manager_user.session_version, 0)
        self.assertEqual(self.ordinary_user.session_version, 0)

    def test_repeated_force_logout_increments_safely(self):
        _grant_change_employee(self.manager_user)
        self.client.force_login(self.manager_user)
        for expected in (1, 2, 3):
            response = self.client.post(
                FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
            )
            self.assertEqual(response.status_code, 200)
            self.target_user.refresh_from_db()
            self.assertEqual(self.target_user.session_version, expected)

    def test_does_not_set_is_active_false(self):
        _grant_change_employee(self.manager_user)
        self.client.force_login(self.manager_user)
        self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )
        self.target_user.refresh_from_db()
        self.target.refresh_from_db()
        self.assertTrue(self.target_user.is_active)
        self.assertTrue(self.target.is_active)

    def test_does_not_change_password(self):
        _grant_change_employee(self.manager_user)
        old_password_hash = self.target_user.password
        self.client.force_login(self.manager_user)
        self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )
        self.target_user.refresh_from_db()
        self.assertEqual(self.target_user.password, old_password_hash)

    def test_target_can_login_again_immediately(self):
        _grant_change_employee(self.manager_user)
        self.client.force_login(self.manager_user)
        self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id), HTTP_HX_REQUEST="true"
        )

        api_client = APIClient()
        response = api_client.post(
            "/api/auth/login/",
            {"username": "fl_target", "password": "secret123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

    def test_audit_log_never_contains_token_or_password(self):
        _grant_change_employee(self.manager_user)
        self.client.force_login(self.manager_user)
        self.client.post(
            FORCE_LOGOUT_URL.format(self.target.id),
            {"password": "should-never-be-logged"},
            HTTP_HX_REQUEST="true",
        )
        entries = UserActivityLog.objects.filter(
            route_name="employee-force-mobile-logout"
        )
        self.assertTrue(entries.exists())
        for entry in entries:
            blob = str(entry.details).lower()
            self.assertNotIn("should-never-be-logged", blob)
            self.assertNotIn("bearer", blob)
            self.assertNotIn("password", blob)
