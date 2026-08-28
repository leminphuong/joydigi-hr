"""Phase LEAVE-7A.3: standalone "Loại nghỉ phép" page.

`LeaveTypeView` (`type-view/`) used to redirect into the merged Leave
Settings page (`leave-settings-view`) — Phase LEAVE-7A.2 pointed the
sidebar there as a stopgap. This phase un-redirects `LeaveTypeView` so
it renders its own page again, and repoints the sidebar at it
directly. Reuses the existing `LeaveTypeForm`/`UpdateLeaveTypeForm`/
`LeaveTypeListView` CRUD entirely — no new views, no new model fields,
no migration.
"""

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from joydigi.testkit import make_company, make_employee, make_user
from leave.models import LeaveType


def _valid_leave_type_post_data(**overrides):
    data = {
        "name": "Nghỉ chế độ",
        "payment": "unpaid",
        "payment_type": "unpaid",
        "period_in": "day",
        "count": 1,
        "total_days": 1,
        "limit_leave": "on",
        "reset": "",
        "carryforward_type": "no carryforward",
        "require_approval": "yes",
        "require_attachment": "no",
        "exclude_company_leave": "no",
        "exclude_holiday": "no",
        "is_encashable": "",
        "is_compensatory_leave": "",
        "is_active": "on",
    }
    data.update(overrides)
    return data


class LeaveTypeStandalonePageRoutingTests(TestCase):
    """A — Routing."""

    def setUp(self):
        self.company = make_company("Standalone Routing Co")
        self.admin = make_user("standalone_admin", is_superuser=True)
        make_employee(
            company=self.company,
            email="standalone_admin@test.joydigi",
            user=self.admin,
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_standalone_url_resolves(self):
        self.assertTrue(reverse("type-view"))

    def test_get_standalone_page_returns_200(self):
        response = self.client.get(reverse("type-view"))
        self.assertEqual(response.status_code, 200)

    def test_standalone_page_no_longer_redirects(self):
        response = self.client.get(reverse("type-view"), follow=False)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(response.status_code, (301, 302))

    def test_sidebar_points_to_standalone_page(self):
        # Phase LEAVE-7A.3A: the real top-level sidebar link (see
        # `test_leave_type_top_level_sidebar.py`) — no longer a
        # `leave/sidebar.py` SUBMENUS entry.
        response = self.client.get(reverse("type-view"))
        self.assertIn(
            f'href="{reverse("type-view")}"', response.content.decode()
        )

    def test_sidebar_does_not_point_to_leave_settings_view(self):
        response = self.client.get(reverse("type-view"))
        self.assertNotIn(
            f'href="{reverse("leave-settings-view")}"',
            response.content.decode(),
        )

    def test_no_duplicate_menu_entry(self):
        response = self.client.get(reverse("type-view"))
        self.assertEqual(
            response.content.decode().count('data-menu="Loại nghỉ phép"'), 1
        )


class LeaveTypeStandalonePagePermissionTests(TestCase):
    """B — Permission."""

    def setUp(self):
        self.company = make_company("Standalone Perm Co")
        self.client = Client()

    # Menu-visibility coverage (superuser / permitted / denied) now
    # lives in `test_leave_type_top_level_sidebar.py`, against the
    # actual rendered top-level link — this class only covers page
    # access itself.

    def test_superuser_sees_page_and_menu(self):
        user = make_user("standalone_super", is_superuser=True)
        make_employee(company=self.company, email="s1@test.joydigi", user=user)
        self.client.force_login(user)
        response = self.client.get(reverse("type-view"))
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-menu="Loại nghỉ phép"', response.content.decode())

    def test_user_with_permission_sees_page_and_menu(self):
        user = make_user("standalone_permitted")
        make_employee(company=self.company, email="s2@test.joydigi", user=user)
        perm = Permission.objects.get(
            content_type__app_label="leave", codename="view_leavetype"
        )
        user.user_permissions.add(perm)
        self.client.force_login(user)
        response = self.client.get(reverse("type-view"))
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-menu="Loại nghỉ phép"', response.content.decode())

    def test_user_without_permission_cannot_access_or_see_menu(self):
        user = make_user("standalone_denied")
        make_employee(company=self.company, email="s3@test.joydigi", user=user)
        self.client.force_login(user)
        response = self.client.get(reverse("type-view"))
        self.assertNotEqual(response.status_code, 200)


class LeaveTypeStandaloneCreateEditTests(TestCase):
    """C — Create, Edit."""

    def setUp(self):
        self.company = make_company("Standalone Create Co")
        self.admin = make_user("standalone_creator", is_superuser=True)
        make_employee(
            company=self.company,
            email="standalone_creator@test.joydigi",
            user=self.admin,
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_get_create_form_returns_200(self):
        response = self.client.get(reverse("type-creation"))
        self.assertEqual(response.status_code, 200)

    def test_valid_create_creates_leave_type(self):
        self.client.post(reverse("type-creation"), _valid_leave_type_post_data())
        self.assertTrue(LeaveType.objects.filter(name="Nghỉ chế độ").exists())

    def test_create_redirects_to_standalone_list(self):
        response = self.client.post(
            reverse("type-creation"), _valid_leave_type_post_data()
        )
        self.assertRedirects(response, reverse("type-view"))

    def test_created_active_type_appears_in_list(self):
        self.client.post(reverse("type-creation"), _valid_leave_type_post_data())
        leave_type = LeaveType.objects.get(name="Nghỉ chế độ")
        list_response = self.client.get(
            reverse("leave-type-list") + f"?search={leave_type.name}",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, leave_type.name)

    def test_get_edit_form_returns_200(self):
        leave_type = LeaveType.objects.create(name="Sẽ sửa", total_days=5)
        response = self.client.get(
            reverse("type-update", kwargs={"id": leave_type.id})
        )
        self.assertEqual(response.status_code, 200)

    def test_edit_persists_changes(self):
        leave_type = LeaveType.objects.create(name="Sẽ sửa", total_days=5)
        self.client.post(
            reverse("type-update", kwargs={"id": leave_type.id}),
            _valid_leave_type_post_data(name="Sẽ sửa", count=5, total_days=5),
        )
        leave_type.refresh_from_db()
        self.assertTrue(leave_type.is_active)

    def test_edit_redirects_to_standalone_list(self):
        leave_type = LeaveType.objects.create(name="Sẽ sửa", total_days=5)
        response = self.client.post(
            reverse("type-update", kwargs={"id": leave_type.id}),
            _valid_leave_type_post_data(name="Sẽ sửa", count=5, total_days=5),
        )
        self.assertRedirects(response, reverse("type-view"))


class LeaveTypeStandaloneActiveStateTests(TestCase):
    """D — Active state."""

    def setUp(self):
        self.company = make_company("Standalone Active Co")
        self.admin = make_user("standalone_toggle", is_superuser=True)
        make_employee(
            company=self.company,
            email="standalone_toggle@test.joydigi",
            user=self.admin,
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_active_type_shown_as_active_in_list(self):
        leave_type = LeaveType.objects.create(name="Đang hoạt động rồi", total_days=5)
        response = self.client.get(
            reverse("leave-type-list") + f"?search={leave_type.name}",
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(response, "Đang hoạt động")

    def test_inactive_type_shown_as_inactive_in_list(self):
        leave_type = LeaveType.objects.create(
            name="Đã tắt rồi", total_days=5, is_active=False
        )
        response = self.client.get(
            reverse("leave-type-list") + f"?search={leave_type.name}",
            HTTP_HX_REQUEST="true",
        )
        self.assertContains(response, "Đã tắt")

    def test_inactive_type_excluded_from_mobile_active_api(self):
        password = "secret123"
        emp_user = make_user("standalone_mobile_emp", password=password)
        make_employee(
            company=self.company,
            email="standalone_mobile_emp@test.joydigi",
            user=emp_user,
        )
        active = LeaveType.objects.create(name="Còn hoạt động", total_days=5)
        inactive = LeaveType.objects.create(
            name="Đã bị tắt", total_days=5, is_active=False
        )

        api_client = APIClient()
        login = api_client.post(
            "/api/auth/login/",
            {"username": "standalone_mobile_emp", "password": password},
            format="json",
        )
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        response = api_client.get("/api/leave/leave-type/?is_active=true")

        names = {item["name"] for item in response.data["results"]}
        self.assertIn(active.name, names)
        self.assertNotIn(inactive.name, names)


class LeaveTypeStandaloneRegressionTests(TestCase):
    """E — Regression."""

    def setUp(self):
        self.company = make_company("Standalone Regression Co")
        self.admin = make_user("standalone_regress", is_superuser=True)
        make_employee(
            company=self.company,
            email="standalone_regress@test.joydigi",
            user=self.admin,
        )
        self.client = Client()
        self.client.force_login(self.admin)

    def test_existing_leave_type_history_unaffected(self):
        leave_type = LeaveType.objects.create(name="Lịch sử cũ", total_days=10)
        self.client.post(
            reverse("type-update", kwargs={"id": leave_type.id}),
            _valid_leave_type_post_data(
                name="Lịch sử cũ", count=10, total_days=10, is_active=""
            ),
        )
        leave_type.refresh_from_db()
        self.assertEqual(leave_type.name, "Lịch sử cũ")
        self.assertEqual(leave_type.total_days, 10)
        self.assertFalse(leave_type.is_active)

    def test_leave_settings_view_still_returns_200(self):
        response = self.client.get(reverse("leave-settings-view"))
        self.assertEqual(response.status_code, 200)
