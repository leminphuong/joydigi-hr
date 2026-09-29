"""
Phase NEWS-DETAIL-API-AND-PUSH-ROUTING-HARDENING.

`GET /api/base/announcement/<id>` — the read the app needs to open the exact
post a NEWS push names. Before it, the app hunted for the id in the paginated
list, which simply cannot reach a bulletin that has scrolled past the first
few pages, so an old post could never be opened from its notification.

The whole risk of a by-id read is that an id is guessable, so most of what is
held here is the visibility rule: the endpoint must answer for exactly the
posts the list would have shown this employee, and 404 for every other one —
including ones that exist and belong to somebody else, which must be
indistinguishable from ones that do not exist at all.
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from base.models import Announcement, Department, JobPosition
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_auth.models import JoydigiUser

PUSH_TARGET = "joydigi_api.push.send_to_user"


def _reloaded(user):
    """See `test_announcement_feed_api._reloaded` — a request always
    re-derives its user from a fresh query, and the employee factory leaves a
    stale `employee_work_info` cached on the object it returns."""
    return JoydigiUser.objects.get(pk=user.pk)


def make_announcement(*, company, created_by=None, **overrides):
    defaults = {
        "title": "Test announcement",
        "description": "<p>Hello team</p>",
        "expire_date": date.today() + timedelta(days=30),
    }
    defaults.update(overrides)
    ann = Announcement.objects.create(created_by=created_by, **defaults)
    ann.company_id.set([company])
    return ann


def detail_url(announcement):
    return "/api/base/announcement/%s" % announcement.pk


class AnnouncementDetailVisibilityTests(TestCase):
    """A, B, C, F, G, H — the endpoint answers for exactly the list's posts."""

    def setUp(self):
        self.company_a = make_company("Detail Co A")
        self.company_b = make_company("Detail Co B")

        self.user_a = make_user("detail_a", password="secret123")
        self.employee_a = make_employee(
            company=self.company_a, email="detail_a@test.joydigi", user=self.user_a
        )
        self.user_b = make_user("detail_b", password="secret123")
        self.employee_b = make_employee(
            company=self.company_b, email="detail_b@test.joydigi", user=self.user_b
        )

        self.client_a = APIClient()
        self.client_a.force_authenticate(user=_reloaded(self.user_a))

    def test_a_post_visible_in_the_list_is_readable_by_id(self):
        post = make_announcement(
            company=self.company_a, title="Nghi le 2/9", created_by=self.user_a
        )

        listed = self.client_a.get("/api/base/announcement-view")
        self.assertIn(post.pk, [item["id"] for item in listed.data["results"]])

        response = self.client_a.get(detail_url(post))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], post.pk)
        self.assertEqual(response.data["title"], "Nghi le 2/9")

    def test_b_a_post_from_another_company_is_404(self):
        post = make_announcement(
            company=self.company_b, title="Company B only", created_by=self.user_b
        )

        response = self.client_a.get(detail_url(post))

        self.assertEqual(
            response.status_code,
            404,
            msg="a guessed id must not leak another company's bulletin",
        )

    def test_c_a_post_addressed_to_someone_else_is_404(self):
        post = make_announcement(
            company=self.company_a, title="For somebody else", created_by=self.user_a
        )
        other_user = make_user("detail_other", password="secret123")
        other = make_employee(
            company=self.company_a,
            email="detail_other@test.joydigi",
            user=other_user,
        )
        post.employees.set([other])

        response = self.client_a.get(detail_url(post))

        self.assertEqual(response.status_code, 404)

    def test_f_an_inactive_post_is_404(self):
        post = make_announcement(company=self.company_a, created_by=self.user_a)
        post.is_active = False
        post.save()

        response = self.client_a.get(detail_url(post))

        self.assertEqual(response.status_code, 404)

    def test_g_an_expired_post_is_404(self):
        post = make_announcement(
            company=self.company_a,
            created_by=self.user_a,
            expire_date=date.today() - timedelta(days=1),
        )

        response = self.client_a.get(detail_url(post))

        self.assertEqual(
            response.status_code,
            404,
            msg="the same window the list applies, not a wider one",
        )

    def test_h_an_open_ended_post_is_readable(self):
        post = make_announcement(
            company=self.company_a, created_by=self.user_a, expire_date=None
        )

        response = self.client_a.get(detail_url(post))

        self.assertEqual(
            response.status_code,
            200,
            msg="expire_date NULL means it does not expire, as on the web",
        )

    def test_a_post_that_does_not_exist_is_the_same_404(self):
        # Same shape as "exists but is not yours", so the two cannot be told
        # apart by probing.
        hidden = make_announcement(company=self.company_b, created_by=self.user_b)
        missing = self.client_a.get("/api/base/announcement/99999999")
        forbidden = self.client_a.get(detail_url(hidden))

        self.assertEqual(missing.status_code, 404)
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(missing.data, forbidden.data)

    def test_i_authentication_is_required(self):
        post = make_announcement(company=self.company_a, created_by=self.user_a)

        response = APIClient().get(detail_url(post))

        self.assertEqual(response.status_code, 401)

    def test_the_trailing_slash_form_answers_the_same(self):
        post = make_announcement(company=self.company_a, created_by=self.user_a)

        with_slash = self.client_a.get("/api/base/announcement/%s/" % post.pk)

        self.assertEqual(with_slash.status_code, 200)
        self.assertEqual(with_slash.data["id"], post.pk)

    def test_it_is_read_only(self):
        post = make_announcement(company=self.company_a, created_by=self.user_a)

        for method in (self.client_a.post, self.client_a.put, self.client_a.delete):
            self.assertEqual(
                method(detail_url(post)).status_code,
                405,
                msg="a read endpoint must not accept writes",
            )


class AnnouncementDetailAudienceTests(TestCase):
    """D, E — department and job-position targeting, as production writes it.

    `Announcement.employees` is the audience snapshot the create/update form
    resolves from the chosen departments and job positions (covered in
    `test_announcement_push`), and it is what the list filters on. These
    build rows the same shape and check the detail endpoint agrees.
    """

    def setUp(self):
        self.company = make_company("Audience Co")
        self.department = Department.objects.create(department="Ky thuat")
        self.department.company_id.add(self.company)
        self.position = JobPosition.objects.create(
            job_position="Truong nhom", department_id=self.department
        )

        self.user = make_user("aud_member", password="secret123")
        self.member = make_employee(
            company=self.company,
            email="aud_member@test.joydigi",
            user=self.user,
            department=self.department,
        )
        info = self.member.employee_work_info
        info.job_position_id = self.position
        info.save()

        self.outsider_user = make_user("aud_outsider", password="secret123")
        self.outsider = make_employee(
            company=self.company,
            email="aud_outsider@test.joydigi",
            user=self.outsider_user,
        )

        self.client_member = APIClient()
        self.client_member.force_authenticate(user=_reloaded(self.user))
        self.client_outsider = APIClient()
        self.client_outsider.force_authenticate(user=_reloaded(self.outsider_user))

    def test_d_a_department_targeted_post_is_readable_by_a_member(self):
        post = make_announcement(company=self.company, title="Cho phong ky thuat")
        post.department.set([self.department])
        post.employees.set([self.member])

        self.assertEqual(self.client_member.get(detail_url(post)).status_code, 200)
        self.assertEqual(
            self.client_outsider.get(detail_url(post)).status_code,
            404,
            msg="and nobody outside that department can read it by id",
        )

    def test_e_a_job_position_targeted_post_is_readable_by_its_holder(self):
        post = make_announcement(company=self.company, title="Cho truong nhom")
        post.job_position.set([self.position])
        post.employees.set([self.member])

        self.assertEqual(self.client_member.get(detail_url(post)).status_code, 200)
        self.assertEqual(
            self.client_outsider.get(detail_url(post)).status_code, 404
        )

    def test_a_post_addressed_to_nobody_reaches_the_whole_company(self):
        post = make_announcement(company=self.company, title="Toan cong ty")

        self.assertEqual(self.client_member.get(detail_url(post)).status_code, 200)
        self.assertEqual(
            self.client_outsider.get(detail_url(post)).status_code,
            200,
            msg="an empty audience is the model's own 'everybody' contract",
        )

    def test_the_blanket_permission_sees_a_targeted_post(self):
        # Same latitude the list gives `base.view_announcement`, no more.
        post = make_announcement(company=self.company, title="Cho phong ky thuat")
        post.employees.set([self.member])
        self.outsider_user.user_permissions.add(
            Permission.objects.get(codename="view_announcement")
        )
        privileged = APIClient()
        privileged.force_authenticate(user=_reloaded(self.outsider_user))

        self.assertEqual(privileged.get(detail_url(post)).status_code, 200)


class AnnouncementDetailResponseShapeTests(TestCase):
    """J — one response shape, so one client-side parser keeps working."""

    def setUp(self):
        self.company = make_company("Shape Co")
        self.user = make_user("shape_user", password="secret123")
        self.employee = make_employee(
            company=self.company, email="shape@test.joydigi", user=self.user
        )
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=_reloaded(self.user))
        self.post = make_announcement(
            company=self.company,
            title="Thong bao",
            description="Dong mot\nDong hai",
            created_by=self.user,
        )

    def test_j_the_detail_body_is_the_list_item_byte_for_byte(self):
        listed = self.client_api.get("/api/base/announcement-view").json()
        item = next(row for row in listed["results"] if row["id"] == self.post.pk)

        detail = self.client_api.get(detail_url(self.post)).json()

        self.assertEqual(
            detail,
            item,
            msg="the app parses both with one DTO; a second shape would mean "
            "a second parser, and a second parser drifts",
        )

    def test_the_plain_text_body_is_parsed_into_blocks_here_too(self):
        detail = self.client_api.get(detail_url(self.post)).json()

        self.assertEqual(
            [block["text"] for block in detail["content"]],
            ["Dong mot", "Dong hai"],
        )

    def test_every_field_the_app_reads_is_present(self):
        detail = self.client_api.get(detail_url(self.post)).json()

        for field in (
            "id",
            "title",
            "content",
            "created_at",
            "expire_date",
            "has_viewed",
            "is_pinned",
            "author",
            "attachments",
            "comment_count",
            "reaction_count",
            "my_reaction",
            "reaction_summary",
        ):
            self.assertIn(field, detail)


class AnnouncementDetailSideEffectTests(TestCase):
    """K, L, M — a read stays a read."""

    def setUp(self):
        self.company = make_company("Side Effect Co")
        self.user = make_user("side_user", password="secret123")
        self.employee = make_employee(
            company=self.company, email="side@test.joydigi", user=self.user
        )
        self.department = Department.objects.create(department="Van phong")
        self.department.company_id.add(self.company)

        self.client_api = APIClient()
        self.client_api.force_authenticate(user=_reloaded(self.user))

        self.post = make_announcement(
            company=self.company, title="Read only", created_by=self.user
        )
        self.post.department.set([self.department])
        self.post.employees.set([self.employee])

    def test_k_reading_a_post_creates_no_notification(self):
        from notifications.models import Notification

        before = Notification.objects.count()

        self.assertEqual(self.client_api.get(detail_url(self.post)).status_code, 200)

        self.assertEqual(Notification.objects.count(), before)

    def test_l_reading_a_post_sends_no_push(self):
        with mock.patch(PUSH_TARGET) as send:
            self.assertEqual(
                self.client_api.get(detail_url(self.post)).status_code, 200
            )

        send.assert_not_called()

    def test_m_reading_a_post_does_not_mutate_its_audience(self):
        employees_before = set(self.post.employees.values_list("pk", flat=True))
        departments_before = set(self.post.department.values_list("pk", flat=True))
        positions_before = set(self.post.job_position.values_list("pk", flat=True))
        expire_before = self.post.expire_date

        self.assertEqual(self.client_api.get(detail_url(self.post)).status_code, 200)

        self.post.refresh_from_db()
        self.assertEqual(
            set(self.post.employees.values_list("pk", flat=True)), employees_before
        )
        self.assertEqual(
            set(self.post.department.values_list("pk", flat=True)),
            departments_before,
        )
        self.assertEqual(
            set(self.post.job_position.values_list("pk", flat=True)),
            positions_before,
        )
        self.assertEqual(
            self.post.expire_date,
            expire_before,
            msg="no expire backfill on a GET — that bug is fixed and must "
            "not come back through a new endpoint",
        )

    def test_reading_a_post_does_not_mark_it_viewed(self):
        from base.models import AnnouncementView

        before = AnnouncementView.objects.count()

        self.assertEqual(self.client_api.get(detail_url(self.post)).status_code, 200)

        self.assertEqual(
            AnnouncementView.objects.count(),
            before,
            msg="the list does not record views either; this endpoint must "
            "not quietly start doing it",
        )
