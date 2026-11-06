"""Phase 2 W1: the JOYOKR web shell at /okr/, its HR menu entry, the first-
admin command and collation-independent Vietnamese search. Fake data only."""

import io
import json
import re

from django.contrib.auth.context_processors import PermWrapper
from django.core.management import CommandError, call_command
from django.template.loader import render_to_string
from django.test import RequestFactory, TestCase

from joydigi.testkit import make_employee, make_user
from okr.models import OKRRole
from okr.services.search import fold
from okr.templatetags.okr_web import okr_access
from okr.tests.factories import FAKE_PASSWORD, okr_company, okr_cycle, okr_employee, okr_objective, okr_role
from okr.tests.test_api import APITestBase


class ShellTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = okr_company("Web Co")
        cls.employee = okr_employee(cls.company, "web_employee")
        cls.manager = okr_employee(cls.company, "web_manager")
        okr_role(cls.manager, "manager")

    def login(self, employee):
        self.client.force_login(employee.employee_user_id)

    def test_requires_the_hr_login(self):
        response = self.client.get("/okr/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/login/?"))
        self.assertIn("next=/okr/", response["Location"])

    def test_shell_with_config_and_strict_csp(self):
        self.login(self.employee)
        response = self.client.get("/okr/")
        self.assertEqual(response.status_code, 200)
        csp = response["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(response["Cache-Control"], "no-store")
        html = response.content.decode()
        config = json.loads(re.search(r'<script id="joyokr-config" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(config["root"], "/api/okr/")
        self.assertEqual(config["currentUser"]["id"], self.employee.pk)
        self.assertNotIn("joyokr_approve", config["capabilities"])
        self.assertTrue(config["csrfToken"])
        # No inline JavaScript at all: every <script> is either JSON data or a file.
        for tag in re.findall(r"<script[^>]*>", html):
            self.assertTrue('type="application/json"' in tag or "src=" in tag, tag)
        self.assertNotIn("admin/approvals", html)
        self.assertIn('href="/"', html)  # back to the HR web

    def test_manager_sees_the_approval_menu(self):
        self.login(self.manager)
        html = self.client.get("/okr/").content.decode()
        self.assertIn('href="/okr/admin/approvals"', html)

    def test_names_cannot_break_out_of_the_config(self):
        from employee.models import Employee

        hostile = okr_employee(self.company, "web_hostile")
        # HR's own validation refuses such names; write them directly to stand
        # in for legacy or imported data.
        Employee._base_manager.filter(pk=hostile.pk).update(
            employee_first_name="</script><script>alert(1)</script>",
            employee_last_name='"><img src=x onerror=alert(2)>',
        )
        self.login(hostile)
        html = self.client.get("/okr/").content.decode()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x onerror", html)

    def test_deep_links_and_unknown_routes(self):
        self.login(self.employee)
        self.assertEqual(self.client.get("/okr/objectives/12").status_code, 200)
        self.assertEqual(self.client.get("/okr/key-results/3/").status_code, 200)
        self.assertEqual(self.client.get("/okr/admin/users").status_code, 404)
        self.assertEqual(self.client.get("/okr/profile").status_code, 404)

    def test_account_without_employee_is_sent_back_to_login(self):
        # HR's CompanyMiddleware logs such accounts out before any page runs.
        user = make_user("web_no_employee", password=FAKE_PASSWORD, email="web_no_employee@example.com")
        self.client.force_login(user)
        response = self.client.get("/okr/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_inactive_employee_gets_the_denied_page(self):
        from employee.models import Employee

        leaver = okr_employee(self.company, "web_leaver")
        Employee._base_manager.filter(pk=leaver.pk).update(is_active=False)
        self.login(leaver)
        response = self.client.get("/okr/")
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Chưa được dùng Mục tiêu", status_code=403)


class MenuEntryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = okr_company("Menu Co")
        cls.employee = okr_employee(cls.company, "menu_employee")
        cls.loose_user = make_user("menu_loose", password=FAKE_PASSWORD, email="menu_loose@example.com")

    def render_menu(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return render_to_string(
            "joydigi_theme/components/sidebar/top_menu.html", {"request": request, "perms": PermWrapper(user)}
        )

    def test_entry_only_for_people_who_can_open_okr(self):
        self.assertTrue(okr_access(self.employee.employee_user_id))
        self.assertFalse(okr_access(self.loose_user))
        self.assertIn('href="/okr/"', self.render_menu(self.employee.employee_user_id))
        self.assertNotIn('href="/okr/"', self.render_menu(self.loose_user))


class GrantRoleCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group

        from base.roles import ADMIN_ROLE

        cls.company = okr_company("Cmd Co")
        cls.other = okr_company("Cmd Other")
        cls.target = okr_employee(cls.company, "cmd_target")
        cls.hr_admin = okr_employee(cls.company, "cmd_hr_admin")
        cls.foreign_admin = okr_employee(cls.other, "cmd_foreign_admin")
        cls.plain = okr_employee(cls.company, "cmd_plain")
        group, _ = Group.objects.get_or_create(name=ADMIN_ROLE)
        cls.hr_admin.employee_user_id.groups.add(group)
        cls.foreign_admin.employee_user_id.groups.add(group)

    def run_command(self, *args):
        out = io.StringIO()
        call_command("okr_grant_role", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_then_apply(self):
        args = ["--employee", str(self.target.pk), "--role", "admin", "--granted-by", "cmd_hr_admin"]
        self.assertIn("Chạy thử", self.run_command(*args))
        self.assertFalse(OKRRole.objects.filter(employee=self.target).exists())
        self.assertIn("Đã áp dụng", self.run_command(*args, "--yes"))
        role = OKRRole.objects.get(employee=self.target)
        self.assertEqual((role.role, role.company_id, role.granted_by.username), ("admin", self.company.pk, "cmd_hr_admin"))
        with self.assertRaisesMessage(CommandError, "đã có vai trò"):
            self.run_command(*args, "--yes")
        with self.assertRaisesMessage(CommandError, "cuối cùng"):
            self.run_command(*args, "--revoke", "--yes")

    def test_authoriser_must_be_admin_of_the_same_company(self):
        for username in ("cmd_plain", "cmd_foreign_admin", "nobody_at_all"):
            with self.assertRaises(CommandError):
                self.run_command("--employee", str(self.target.pk), "--role", "admin",
                                 "--granted-by", username, "--yes")
        self.assertFalse(OKRRole.objects.exists())

    def test_lookup_by_email_and_inactive_employees(self):
        self.run_command("--employee", "cmd_target@example.com", "--role", "manager",
                         "--granted-by", "cmd_hr_admin", "--yes")
        self.assertTrue(OKRRole.objects.filter(employee=self.target, role="manager").exists())
        self.plain.is_active = False
        self.plain.save()
        with self.assertRaisesMessage(CommandError, "không còn hoạt động"):
            self.run_command("--employee", str(self.plain.pk), "--role", "admin", "--granted-by", "cmd_hr_admin")


class VietnameseSearchTests(APITestBase):
    def test_fold(self):
        self.assertEqual(fold("Đạt MỤC tiêu ĐỖ"), "dat muc tieu do")
        self.assertEqual(fold("NGUYỄN Văn Ánh"), "nguyen van anh")

    def test_objective_search_ignores_case_accents_and_d(self):
        cycle = self.cycle
        target = okr_objective(cycle, self.owner, title="Đạt doanh thu Quý IV", scope="company",
                               approval_status="approved")
        okr_objective(cycle, self.owner, title="Đặt lịch họp", scope="company", approval_status="approved")
        okr_objective(cycle, self.owner, title="Giảm chi phí", scope="company", approval_status="approved")
        for term in ("Đạt doanh", "đạt DOANH", "ĐẠT", "dat doanh thu", "QUÝ iv", "thu đạt"):
            ids = {o["id"] for o in self.call(self.owner, "get", f"objectives/?q={term}")["results"]}
            self.assertIn(target.pk, ids, term)
        ids = {o["id"] for o in self.call(self.owner, "get", "objectives/?q=giảm chi")["results"]}
        self.assertEqual(len(ids), 1)
        # Accent-insensitive like JOYOKR's MySQL collation: "dat" also finds "Đặt".
        self.assertEqual(self.call(self.owner, "get", "objectives/?q=dat")["count"], 2)

    def test_people_search_by_vietnamese_name(self):
        do = make_employee(company=self.company, email="ddo@example.com", first_name="Đỗ", last_name="Thị Hằng",
                           user=make_user("vn_do", password=FAKE_PASSWORD, email="ddo@example.com"))
        nguyen = make_employee(company=self.company, email="nguyen@example.com", first_name="Nguyễn",
                               last_name="Văn Ánh",
                               user=make_user("vn_nguyen", password=FAKE_PASSWORD, email="nguyen@example.com"))
        cases = {"đỗ": do.pk, "ĐỖ": do.pk, "do thi hang": do.pk, "HẰNG": do.pk,
                 "nguyễn": nguyen.pk, "NGUYEN van": nguyen.pk, "ánh": nguyen.pk}
        for term, expected in cases.items():
            ids = {p["id"] for p in self.call(self.colleague, "get", f"people/?q={term}&page_size=100")["results"]}
            self.assertIn(expected, ids, term)
        foreign = okr_employee(okr_company("Search Other"), "vn_foreign")
        self.assertNotIn(foreign.pk, {p["id"] for p in self.call(self.colleague, "get", "people/?q=test")["results"]})
