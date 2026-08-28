"""Phase LEAVE-7A.3A: "Loại nghỉ phép" as a permanent, non-collapsible
top-level sidebar item.

IMPORTANT audit finding: the sidebar actually rendered in production
is `joydigi_theme/templates/joydigi_theme/components/sidebar/
top_menu.html` — a flat list of `<a class="sideboxmain" data-menu="...">`
links, each independently gated by role/permission `{% if %}` blocks,
included from `joydigi_theme/templates/index.html`. `leave/sidebar.py`'s
MENU/SUBMENUS registry (and the base `templates/sidebar.html` that
renders it) is a *separate* mechanism still used by the breadcrumb
system (`joydigi_crumbs`) for section-label resolution, but it is
never included by the theme's `index.html` and therefore never
produces any visible sidebar markup — adding an entry there alone
(as earlier phases assumed) has no visible effect. The real fix is a
new `<a>` in `top_menu.html`, gated by `{% if perms.leave.view_leavetype %}`
— independent of the checkin-portal `checkin_admin`/`checkin_leader`
role checks that gate its neighbors, since Section 4 of this phase
requires keeping the existing Django permission, not tying it to that
separate role system.
"""

from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

from joydigi.testkit import make_company, make_employee, make_user
from leave.models import LeaveType

SIDEBAR_ENTRY = 'data-menu="Loại nghỉ phép"'


class LeaveTypeTopLevelSidebarTests(TestCase):
    def setUp(self):
        self.company = make_company("Top Level Sidebar Co")
        self.client = Client()

    def _login_superuser(self):
        user = make_user("top_level_super", is_superuser=True)
        make_employee(company=self.company, email="tls1@test.joydigi", user=user)
        self.client.force_login(user)
        return user

    def _login_permitted(self):
        user = make_user("top_level_permitted")
        make_employee(company=self.company, email="tls2@test.joydigi", user=user)
        perm = Permission.objects.get(
            content_type__app_label="leave", codename="view_leavetype"
        )
        user.user_permissions.add(perm)
        self.client.force_login(user)
        return user

    def _login_denied(self):
        user = make_user("top_level_denied")
        make_employee(company=self.company, email="tls3@test.joydigi", user=user)
        self.client.force_login(user)
        return user

    # 1/3: renders as a real, flat top-level link to type-view --------
    def test_renders_as_top_level_link_to_type_view(self):
        self._login_superuser()
        response = self.client.get(reverse("type-view"))
        html = response.content.decode()
        self.assertIn(SIDEBAR_ENTRY, html)
        self.assertIn(f'href="{reverse("type-view")}"', html)
        # Not wrapped in any collapsible/accordion markup class used by
        # the (separate, unused-for-rendering) legacy sidebar mechanism.
        self.assertNotIn("oh-sidebar__submenu-link", html)

    # 2: not nested under "Nghỉ phép" (the parallel registry used only
    # for breadcrumb section-resolution — see module docstring) --------
    def test_not_present_in_nghi_phep_submenu_registry(self):
        self._login_superuser()
        response = self.client.get(reverse("type-view"))
        leave_menu = next(
            (m for m in response.context["sidebar"] if m["app"] == "leave"), None
        )
        self.assertIsNotNone(leave_menu)
        submenu_labels = {str(item["menu"]) for item in leave_menu["submenu"]}
        self.assertNotIn("Loại nghỉ phép", submenu_labels)

    # 4: exactly one sidebar occurrence (not counting the breadcrumb or
    # the page's own H1, which legitimately also say "Loại nghỉ phép") --
    def test_exactly_one_sidebar_entry(self):
        self._login_superuser()
        response = self.client.get(reverse("type-view"))
        html = response.content.decode()
        self.assertEqual(html.count(SIDEBAR_ENTRY), 1)

    # 5/6/7: permission --------------------------------------------------
    def test_superuser_sees_it(self):
        self._login_superuser()
        response = self.client.get(reverse("type-view"))
        self.assertIn(SIDEBAR_ENTRY, response.content.decode())

    def test_user_with_permission_sees_it(self):
        self._login_permitted()
        response = self.client.get(reverse("type-view"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(SIDEBAR_ENTRY, response.content.decode())

    def test_user_without_permission_does_not_see_it(self):
        self._login_denied()
        response = self.client.get(reverse("user-request-view"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SIDEBAR_ENTRY, response.content.decode())

    # 8: type-view still returns 200 ------------------------------------
    def test_type_view_returns_200(self):
        self._login_superuser()
        response = self.client.get(reverse("type-view"))
        self.assertEqual(response.status_code, 200)

    # 9: create/update pages preserve the correct top-level item --------
    def test_create_page_preserves_top_level_sidebar_item(self):
        self._login_superuser()
        response = self.client.get(reverse("type-creation"))
        self.assertEqual(response.content.decode().count(SIDEBAR_ENTRY), 1)

    def test_update_page_preserves_top_level_sidebar_item(self):
        self._login_superuser()
        leave_type = LeaveType.objects.create(name="Đang sửa", total_days=1)
        response = self.client.get(
            reverse("type-update", kwargs={"id": leave_type.id})
        )
        self.assertEqual(response.content.decode().count(SIDEBAR_ENTRY), 1)

    # 10: no other sidebar link disappears -------------------------------
    def test_no_other_sidebar_link_disappears(self):
        self._login_superuser()
        response = self.client.get(reverse("type-view"))
        html = response.content.decode()
        for other_label in (
            "Duyệt đơn",
            "Bảng chấm công",
            "Xếp ca",
            "Chấm công hôm nay",
            "Nhân sự",
            "Bảng tin",
        ):
            self.assertIn(f'data-menu="{other_label}"', html)
