from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from base.models import CompanyGroupAssignment
from base.roles import LEADER_ROLE, ensure_standard_roles
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_audit.models import UserActivityLog


class UserActivityLogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Activity Log Co")

        cls.admin_user = make_user("activity_admin", is_superuser=True)
        make_employee(
            company=cls.company,
            email="activity-admin@test.joydigi",
            user=cls.admin_user,
        )

        cls.leader_user = make_user("activity_leader")
        make_employee(
            company=cls.company,
            email="activity-leader@test.joydigi",
            user=cls.leader_user,
        )
        ensure_standard_roles()
        leader_group = Group.objects.get(name=LEADER_ROLE)
        cls.leader_user.groups.add(leader_group)
        CompanyGroupAssignment.objects.get_or_create(
            user=cls.leader_user,
            company=cls.company,
            group=leader_group,
        )

        cls.employee_user = make_user("activity_employee")
        make_employee(
            company=cls.company,
            email="activity-employee@test.joydigi",
            user=cls.employee_user,
        )

    def _latest_for(self, user, path):
        return UserActivityLog.objects.filter(user=user, path=path).latest("created_at")

    def test_middleware_records_all_three_standard_roles(self):
        cases = (
            (self.admin_user, "checkin-settings", UserActivityLog.ROLE_ADMIN),
            (self.leader_user, "today-attendance", UserActivityLog.ROLE_LEADER),
            (self.employee_user, "view-my-attendance", UserActivityLog.ROLE_EMPLOYEE),
        )
        for user, url_name, expected_role in cases:
            with self.subTest(role=expected_role):
                self.client.force_login(user)
                url = reverse(url_name)
                response = self.client.get(url)
                self.assertLess(response.status_code, 500)
                log = self._latest_for(user, url)
                self.assertEqual(log.role, expected_role)
                self.assertEqual(log.company_id, self.company.id)
                self.assertEqual(log.method, "GET")
                self.client.logout()

    def test_only_admin_can_view_activity_log(self):
        self.client.force_login(self.employee_user)
        response = self.client.get(reverse("user-activity-log"))
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("user-activity-log"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nhật ký hoạt động")

    def test_activity_log_does_not_record_sensitive_form_values(self):
        self.client.force_login(self.employee_user)
        url = reverse("view-my-attendance")
        self.client.post(url, {"password": "khong-duoc-luu", "token": "bi-mat"})

        log = self._latest_for(self.employee_user, url)
        serialized = str(log.details)
        self.assertNotIn("khong-duoc-luu", serialized)
        self.assertNotIn("bi-mat", serialized)
        self.assertNotIn("password", log.details.get("submitted_fields", []))
        self.assertNotIn("token", log.details.get("submitted_fields", []))
