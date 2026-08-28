"""Phase LEAVE-7A.2A: reproduce (or rule out) the reported "Loại nghỉ
phép" sidebar item missing on production, using the real, deployed
code path — `joydigi.config.get_MENUS` (a template context processor,
registered in `TEMPLATES[0]["OPTIONS"]["context_processors"]`, run on
every authenticated page render) -> `sidebar(request)` ->
`importlib.import_module("leave.sidebar")` -> `SUBMENUS` loop ->
per-entry `accessibility` check.

Every other test touching this menu entry so far (`test_leave_type_
admin_ui.py`) only called `leave_type_accessibility` directly — never
exercised the actual sidebar-assembly pipeline a real page load goes
through. This file closes that gap.
"""

from django.test import Client, TestCase

from joydigi.testkit import make_company, make_employee, make_user


def _leave_menu(sidebar_menus):
    for menu in sidebar_menus:
        if menu["app"] == "leave":
            return menu
    return None


def _submenu_labels(leave_menu):
    return {str(item["menu"]) for item in leave_menu["submenu"]}


class LeaveTypeSidebarMenuRenderTests(TestCase):
    """Exercises the exact context processor a real page load runs,
    for a superuser and for a non-superuser holding the specific
    permission — both are legitimate "admin" shapes this app supports,
    and Django superuser status bypasses `has_perm` backend checks
    entirely, which a mocked user object cannot verify."""

    def setUp(self):
        self.company = make_company("Sidebar Menu Co")
        self.client = Client()

    def _get_sidebar(self, username="sidebar_probe"):
        response = self.client.get("/leave/user-request-view/")
        self.assertEqual(response.status_code, 200)
        return response.context["sidebar"]

    def test_superuser_sees_loai_nghi_phep_in_the_real_rendered_sidebar(self):
        admin = make_user("sidebar_super", is_superuser=True)
        make_employee(
            company=self.company, email="sidebar_super@test.joydigi", user=admin
        )
        self.client.force_login(admin)

        sidebar = self._get_sidebar()
        leave_menu = _leave_menu(sidebar)

        self.assertIsNotNone(
            leave_menu, "the 'Nghỉ phép' top-level menu did not render at all"
        )
        self.assertIn("Loại nghỉ phép", _submenu_labels(leave_menu))

    def test_user_with_view_leavetype_permission_sees_it(self):
        from django.contrib.auth.models import Permission

        user = make_user("sidebar_permitted")
        make_employee(
            company=self.company, email="sidebar_permitted@test.joydigi", user=user
        )
        perm = Permission.objects.get(
            content_type__app_label="leave", codename="view_leavetype"
        )
        user.user_permissions.add(perm)
        self.client.force_login(user)

        sidebar = self._get_sidebar()
        leave_menu = _leave_menu(sidebar)

        self.assertIsNotNone(leave_menu)
        self.assertIn("Loại nghỉ phép", _submenu_labels(leave_menu))

    def test_user_without_the_permission_does_not_see_it(self):
        user = make_user("sidebar_denied")
        make_employee(
            company=self.company, email="sidebar_denied@test.joydigi", user=user
        )
        self.client.force_login(user)

        sidebar = self._get_sidebar()
        leave_menu = _leave_menu(sidebar)

        if leave_menu is not None:
            self.assertNotIn("Loại nghỉ phép", _submenu_labels(leave_menu))

    def test_no_duplicate_entry(self):
        admin = make_user("sidebar_dup_check", is_superuser=True)
        make_employee(
            company=self.company, email="sidebar_dup_check@test.joydigi", user=admin
        )
        self.client.force_login(admin)

        sidebar = self._get_sidebar()
        leave_menu = _leave_menu(sidebar)
        labels = [str(item["menu"]) for item in leave_menu["submenu"]]
        self.assertEqual(labels.count("Loại nghỉ phép"), 1)

    def test_menu_target_url_resolves_to_the_leave_types_page(self):
        # Phase LEAVE-7A.3: the sidebar now points at the standalone
        # `type-view` page directly, not the merged `leave-settings-view`
        # (see `test_leave_type_standalone_page.py` for the full
        # standalone-page test suite).
        from django.urls import reverse

        admin = make_user("sidebar_url_check", is_superuser=True)
        make_employee(
            company=self.company, email="sidebar_url_check@test.joydigi", user=admin
        )
        self.client.force_login(admin)

        sidebar = self._get_sidebar()
        leave_menu = _leave_menu(sidebar)
        entry = next(
            item
            for item in leave_menu["submenu"]
            if str(item["menu"]) == "Loại nghỉ phép"
        )
        self.assertEqual(str(entry["redirect"]), reverse("type-view"))

        response = self.client.get(entry["redirect"])
        self.assertEqual(response.status_code, 200)
