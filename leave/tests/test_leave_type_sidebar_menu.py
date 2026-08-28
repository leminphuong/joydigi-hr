"""Phase LEAVE-7A.2A (superseded by LEAVE-7A.3A — see below): originally
reproduced the reported "Loại nghỉ phép" sidebar item missing on
production, using `joydigi.config.get_MENUS` -> `sidebar(request)` ->
`importlib.import_module("leave.sidebar")` -> `SUBMENUS` loop.

Phase LEAVE-7A.3A moved "Loại nghỉ phép" out of that SUBMENUS registry
entirely, into a permanent top-level link in the actual rendered
sidebar (`joydigi_theme/components/sidebar/top_menu.html` — see
`test_leave_type_top_level_sidebar.py` for that coverage). This file
now only proves the `leave/sidebar.py` registry itself — still used by
`joydigi_crumbs` for breadcrumb section-label resolution, per
UI-7B.1 — stays intact and does NOT carry a duplicate "Loại nghỉ
phép" entry alongside the new top-level link.
"""

from django.test import Client, TestCase
from django.urls import reverse

from joydigi.testkit import make_company, make_employee, make_user


def _leave_menu(sidebar_menus):
    for menu in sidebar_menus:
        if menu["app"] == "leave":
            return menu
    return None


def _submenu_labels(leave_menu):
    return {str(item["menu"]) for item in leave_menu["submenu"]}


class LeaveTypeSidebarMenuRenderTests(TestCase):
    def setUp(self):
        self.company = make_company("Sidebar Menu Co")
        self.client = Client()

    def _get_sidebar(self):
        response = self.client.get("/leave/user-request-view/")
        self.assertEqual(response.status_code, 200)
        return response.context["sidebar"]

    def test_leave_type_not_in_the_submenu_registry(self):
        # By design since Phase LEAVE-7A.3A — it lives as a top-level
        # link now, not a "Nghỉ phép" submenu entry.
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
        self.assertNotIn("Loại nghỉ phép", _submenu_labels(leave_menu))

    def test_other_nghi_phep_submenu_entries_still_render(self):
        admin = make_user("sidebar_other_entries", is_superuser=True)
        make_employee(
            company=self.company,
            email="sidebar_other_entries@test.joydigi",
            user=admin,
        )
        self.client.force_login(admin)

        sidebar = self._get_sidebar()
        leave_menu = _leave_menu(sidebar)
        labels = _submenu_labels(leave_menu)

        self.assertIn("Đơn nghỉ của tôi", labels)
        self.assertIn("Duyệt đơn nghỉ", labels)

    def test_type_view_resolves_and_returns_200(self):
        admin = make_user("sidebar_url_check", is_superuser=True)
        make_employee(
            company=self.company, email="sidebar_url_check@test.joydigi", user=admin
        )
        self.client.force_login(admin)

        response = self.client.get(reverse("type-view"))
        self.assertEqual(response.status_code, 200)
