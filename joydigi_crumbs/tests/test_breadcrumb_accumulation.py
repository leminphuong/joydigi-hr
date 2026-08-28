"""Phase UI-7B.1: breadcrumbs must reflect only the CURRENT page's
genuine hierarchy — never pages visited earlier in the same session.

Root cause: `breadcrumbs()` only reset its session-stored trail when
the current page's last path segment happened to be listed in the
hardcoded `sidebar_urls` allowlist. Any page missing from that list —
e.g. the checkin-portal's Vietnamese-slug pages (`cham-cong-hom-nay/`,
`duyet-don/`) or the LeaveType create/update pages (`type-creation/`,
`type-update/<id>/`) — never triggered a reset, so the trail just kept
growing across unrelated page visits for the rest of the session. The
fix (see `context_processors.py`) replaces the allowlist-driven check
with a general "did the resolved top-level section change since the
last request" comparison — no per-page hardcoding required.
"""

from django.test import Client, TestCase

from joydigi.testkit import make_company, make_employee, make_user
from leave.models import LeaveType


def _names(breadcrumbs):
    return [str(item["name"]) for item in breadcrumbs]


class BreadcrumbAccumulationTests(TestCase):
    def setUp(self):
        self.company = make_company("Breadcrumb Co")
        self.admin = make_user("breadcrumb_admin", is_superuser=True)
        make_employee(
            company=self.company,
            email="breadcrumb_admin@test.joydigi",
            user=self.admin,
        )
        self.client = Client()
        self.client.force_login(self.admin)

    # -- 1/10: the exact reported sequence -----------------------------
    def test_sequence_does_not_leak_into_final_page_breadcrumb(self):
        # A: Leave Type list
        self.client.get("/leave/type-view/")
        # B: Leave Type create
        self.client.get("/leave/type-creation/")
        # C: Duyệt đơn (checkin portal)
        self.client.get("/duyet-don/")
        # D: Chấm công hôm nay
        response = self.client.get("/cham-cong-hom-nay/")

        names = _names(response.context["breadcrumbs"])

        self.assertNotIn("type-creation", names)
        self.assertNotIn("duyet-don", names)
        self.assertNotIn("Loại nghỉ phép", names)
        self.assertNotIn("Thêm loại nghỉ phép", names)
        self.assertNotIn("Duyệt đơn", names)

    # -- 2: direct GET -----------------------------------------------
    def test_direct_get_attendance_today(self):
        response = self.client.get("/cham-cong-hom-nay/")
        names = _names(response.context["breadcrumbs"])
        self.assertIn("Chấm công hôm nay", names)
        self.assertEqual(len(names), 2)  # company root + this page

    # -- 3: refresh must not change breadcrumb -------------------------
    def test_refresh_same_page_is_idempotent(self):
        first = self.client.get("/cham-cong-hom-nay/")
        second = self.client.get("/cham-cong-hom-nay/")
        self.assertEqual(
            _names(first.context["breadcrumbs"]),
            _names(second.context["breadcrumbs"]),
        )

    # -- 4/5: cross-navigation between sections ------------------------
    def test_navigate_leave_then_attendance(self):
        self.client.get("/leave/type-view/")
        response = self.client.get("/cham-cong-hom-nay/")
        names = _names(response.context["breadcrumbs"])
        self.assertNotIn("Loại nghỉ phép", names)
        self.assertNotIn("type-view", names)

    def test_navigate_attendance_then_leave(self):
        self.client.get("/cham-cong-hom-nay/")
        response = self.client.get("/leave/type-view/")
        names = _names(response.context["breadcrumbs"])
        self.assertNotIn("Chấm công hôm nay", names)
        self.assertNotIn("cham-cong-hom-nay", names)

    # -- 5/6/7: Leave Type standalone/create/edit ----------------------
    def test_standalone_leave_type_page_breadcrumb(self):
        response = self.client.get("/leave/type-view/")
        names = _names(response.context["breadcrumbs"])
        # index 0 is the white-labelled company root (defaults to
        # "Joydigi"), not the test's tenant `Company` object's name.
        self.assertEqual(names[1:], ["Nghỉ phép", "Loại nghỉ phép"])

    def test_create_leave_type_page_breadcrumb(self):
        response = self.client.get("/leave/type-creation/")
        names = _names(response.context["breadcrumbs"])
        self.assertEqual(names[1:], ["Nghỉ phép", "Thêm loại nghỉ phép"])

    def test_edit_leave_type_page_breadcrumb(self):
        leave_type = LeaveType.objects.create(name="Nghỉ chế độ", total_days=1)
        response = self.client.get(f"/leave/type-update/{leave_type.id}/")
        names = _names(response.context["breadcrumbs"])
        self.assertEqual(names[1], "Nghỉ phép")
        self.assertEqual(names[2], "Cập nhật loại nghỉ phép")
        # The object-id segment resolves to the instance's own name,
        # per the existing (unmodified) generic id-segment behavior.
        self.assertEqual(names[3], "Nghỉ chế độ")

    # -- 8: cross-session isolation -------------------------------------
    def test_two_sessions_do_not_contaminate_each_other(self):
        other_admin = make_user("breadcrumb_admin_2", is_superuser=True)
        make_employee(
            company=self.company,
            email="breadcrumb_admin_2@test.joydigi",
            user=other_admin,
        )
        other_client = Client()
        other_client.force_login(other_admin)

        self.client.get("/leave/type-view/")
        self.client.get("/leave/type-creation/")

        response = other_client.get("/cham-cong-hom-nay/")
        names = _names(response.context["breadcrumbs"])

        self.assertNotIn("Loại nghỉ phép", names)
        self.assertNotIn("Thêm loại nghỉ phép", names)
        self.assertNotIn("type-creation", names)

    # -- 9: no raw route/URL-name labels ---------------------------------
    def test_no_raw_route_names_in_rendered_breadcrumb(self):
        pages = [
            "/leave/type-view/",
            "/leave/type-creation/",
            "/duyet-don/",
            "/cham-cong-hom-nay/",
        ]
        raw_names = {
            "type-view",
            "type-creation",
            "type-update",
            "duyet-don",
            "cham-cong-hom-nay",
            "leave",
        }
        for page in pages:
            response = self.client.get(page)
            names = set(_names(response.context["breadcrumbs"]))
            leaked = names & raw_names
            self.assertFalse(
                leaked, f"raw route name(s) {leaked} leaked into breadcrumb for {page}"
            )
