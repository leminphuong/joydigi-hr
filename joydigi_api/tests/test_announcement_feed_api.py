"""
Phase UI-3B backend tests for the mobile Bảng tin (Feed) API.

Covers: the company-isolation fix in `AnnouncementListAPIView` (the
exact bug found in the Phase UI-3A audit — JWT requests never populate
`JoydigiCompanyManager`'s thread-local company, so the old query leaked
announcements across companies), the new reaction endpoint, and the new
comment endpoints reusing the existing `AnnouncementComment` model.
"""

from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from base.models import Announcement, AnnouncementComment, AnnouncementReaction
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_auth.models import JoydigiUser


def _reloaded(user):
    """
    `make_employee(..., user=user)` assigns `emp.employee_user_id = user`
    before `emp.save()` — Django's forward-OneToOne descriptor caches
    that `emp` right back onto `user.employee_get` at assignment time,
    *before* the factory's own follow-up `EmployeeWorkInformation`
    `.update()` runs. `.update()` is a raw bulk SQL statement — it never
    touches that already-cached Python object, so `user.employee_get
    .employee_work_info.company_id` keeps reading the pre-update `None`
    forever, even though the database row is correct. A real HTTP
    request never carries an in-process Python object across requests
    like this — it always re-derives the user from a fresh query — so
    `force_authenticate` must be given a freshly re-fetched user object
    for these tests to exercise the same fresh-lookup path production
    actually takes.
    """
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


class AnnouncementFeedCompanyIsolationTests(TestCase):
    """Items 1-5, 24, 25."""

    def setUp(self):
        self.company_a = make_company("Company A")
        self.company_b = make_company("Company B")

        self.user_a = make_user("emp_a", password="secret123")
        self.employee_a = make_employee(
            company=self.company_a, email="emp_a@test.joydigi", user=self.user_a
        )
        self.user_b = make_user("emp_b", password="secret123")
        self.employee_b = make_employee(
            company=self.company_b, email="emp_b@test.joydigi", user=self.user_b
        )

        self.post_a = make_announcement(
            company=self.company_a, title="Company A news", created_by=self.user_a
        )
        self.post_b = make_announcement(
            company=self.company_b, title="Company B news", created_by=self.user_b
        )

        self.client_a = APIClient()
        self.client_a.force_authenticate(user=_reloaded(self.user_a))
        self.client_b = APIClient()
        self.client_b.force_authenticate(user=_reloaded(self.user_b))

    def _titles(self, response):
        return {item["title"] for item in response.data["results"]}

    def test_company_a_cannot_see_company_b_post(self):
        response = self.client_a.get("/api/base/announcement-view")
        self.assertEqual(response.status_code, 200)
        titles = self._titles(response)
        self.assertIn("Company A news", titles)
        self.assertNotIn("Company B news", titles)

    def test_company_b_cannot_see_company_a_post(self):
        response = self.client_b.get("/api/base/announcement-view")
        self.assertEqual(response.status_code, 200)
        titles = self._titles(response)
        self.assertIn("Company B news", titles)
        self.assertNotIn("Company A news", titles)

    def test_existing_employee_targeting_still_works(self):
        other_employee = make_employee(
            company=self.company_a, email="other_a@test.joydigi"
        )
        targeted = make_announcement(
            company=self.company_a,
            title="Just for other_a",
            created_by=self.user_a,
        )
        targeted.employees.set([other_employee])

        response = self.client_a.get("/api/base/announcement-view")
        # employee_a has no explicit base.view_announcement perm and is
        # not in the targeted list, so the employee-targeted post must
        # not appear even though it's the same company.
        self.assertNotIn("Just for other_a", self._titles(response))

    def test_pinned_data_returned(self):
        pinned = make_announcement(
            company=self.company_a,
            title="Pinned post",
            created_by=self.user_a,
            is_pinned=True,
        )
        response = self.client_a.get("/api/base/announcement-view")
        item = next(
            i for i in response.data["results"] if i["id"] == pinned.id
        )
        self.assertTrue(item["is_pinned"])
        # Pinned-first ordering preserved.
        self.assertEqual(response.data["results"][0]["id"], pinned.id)

    def test_attachment_data_returned(self):
        from base.models import Attachment
        from django.core.files.uploadedfile import SimpleUploadedFile

        attachment = Attachment.objects.create(
            file=SimpleUploadedFile("pic.png", b"fake-bytes", content_type="image/png")
        )
        self.post_a.attachments.add(attachment)

        response = self.client_a.get("/api/base/announcement-view")
        item = next(
            i for i in response.data["results"] if i["id"] == self.post_a.id
        )
        self.assertEqual(len(item["attachments"]), 1)
        self.assertEqual(item["attachments"][0]["id"], attachment.id)
        self.assertTrue(item["attachments"][0]["is_image"])
        self.assertIsNotNone(item["attachments"][0]["url"])

    def test_existing_response_fields_unchanged(self):
        response = self.client_a.get("/api/base/announcement-view")
        item = next(
            i for i in response.data["results"] if i["id"] == self.post_a.id
        )
        for field in (
            "id",
            "title",
            "content",
            "created_at",
            "expire_date",
            "has_viewed",
        ):
            self.assertIn(field, item)

    def test_authentication_required_for_list(self):
        anon = APIClient()
        response = anon.get("/api/base/announcement-view")
        self.assertEqual(response.status_code, 401)


class AnnouncementReactionTests(TestCase):
    """Items 6-12."""

    def setUp(self):
        self.company_a = make_company("Reaction Co A")
        self.company_b = make_company("Reaction Co B")
        self.user_a = make_user("react_a", password="secret123")
        self.employee_a = make_employee(
            company=self.company_a, email="react_a@test.joydigi", user=self.user_a
        )
        self.user_b = make_user("react_b", password="secret123")
        self.employee_b = make_employee(
            company=self.company_b, email="react_b@test.joydigi", user=self.user_b
        )

        self.post_a = make_announcement(company=self.company_a, created_by=self.user_a)
        self.post_b = make_announcement(company=self.company_b, created_by=self.user_b)

        self.client_a = APIClient()
        self.client_a.force_authenticate(user=_reloaded(self.user_a))
        self.client_b = APIClient()
        self.client_b.force_authenticate(user=_reloaded(self.user_b))

    def _url(self, post):
        return f"/api/base/announcement/{post.id}/reaction"

    def test_create_reaction(self):
        response = self.client_a.post(self._url(self.post_a), {"reaction": "like"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["my_reaction"], "like")
        self.assertEqual(response.data["reaction_count"], 1)
        row = AnnouncementReaction.objects.get(
            announcement_id=self.post_a, employee_id=self.employee_a
        )
        self.assertEqual(row.reaction, "like")

    def test_change_reaction(self):
        self.client_a.post(self._url(self.post_a), {"reaction": "like"})
        response = self.client_a.post(self._url(self.post_a), {"reaction": "love"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["my_reaction"], "love")
        self.assertEqual(response.data["reaction_count"], 1)

    def test_delete_reaction(self):
        self.client_a.post(self._url(self.post_a), {"reaction": "like"})
        response = self.client_a.delete(self._url(self.post_a))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["my_reaction"])
        self.assertEqual(response.data["reaction_count"], 0)
        self.assertFalse(
            AnnouncementReaction.objects.filter(
                announcement_id=self.post_a, employee_id=self.employee_a
            ).exists()
        )

    def test_duplicate_reaction_prevented(self):
        self.client_a.post(self._url(self.post_a), {"reaction": "like"})
        self.client_a.post(self._url(self.post_a), {"reaction": "wow"})
        count = AnnouncementReaction.objects.filter(
            announcement_id=self.post_a, employee_id=self.employee_a
        ).count()
        self.assertEqual(count, 1)

    def test_invalid_reaction_rejected(self):
        response = self.client_a.post(
            self._url(self.post_a), {"reaction": "not-a-real-reaction"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            AnnouncementReaction.objects.filter(announcement_id=self.post_a).exists()
        )

    def test_employee_identity_cannot_be_spoofed(self):
        other_employee = make_employee(
            company=self.company_a, email="victim_a@test.joydigi"
        )
        response = self.client_a.post(
            self._url(self.post_a),
            {"reaction": "like", "employee_id": other_employee.id},
        )
        self.assertEqual(response.status_code, 200)
        # Regardless of the spoof attempt in the body, the reaction is
        # always attributed to the authenticated employee.
        self.assertTrue(
            AnnouncementReaction.objects.filter(
                announcement_id=self.post_a, employee_id=self.employee_a
            ).exists()
        )
        self.assertFalse(
            AnnouncementReaction.objects.filter(
                announcement_id=self.post_a, employee_id=other_employee
            ).exists()
        )

    def test_cross_company_reaction_rejected(self):
        response = self.client_b.post(self._url(self.post_a), {"reaction": "like"})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            AnnouncementReaction.objects.filter(announcement_id=self.post_a).exists()
        )


class AnnouncementCommentTests(TestCase):
    """Items 13-23."""

    def setUp(self):
        self.company_a = make_company("Comment Co A")
        self.company_b = make_company("Comment Co B")

        self.user_a = make_user("comment_a", password="secret123")
        self.employee_a = make_employee(
            company=self.company_a, email="comment_a@test.joydigi", user=self.user_a
        )
        self.user_a2 = make_user("comment_a2", password="secret123")
        self.employee_a2 = make_employee(
            company=self.company_a, email="comment_a2@test.joydigi", user=self.user_a2
        )
        self.user_b = make_user("comment_b", password="secret123")
        self.employee_b = make_employee(
            company=self.company_b, email="comment_b@test.joydigi", user=self.user_b
        )

        self.post_a = make_announcement(company=self.company_a, created_by=self.user_a)

        self.client_a = APIClient()
        self.client_a.force_authenticate(user=_reloaded(self.user_a))
        self.client_a2 = APIClient()
        self.client_a2.force_authenticate(user=_reloaded(self.user_a2))
        self.client_b = APIClient()
        self.client_b.force_authenticate(user=_reloaded(self.user_b))

    def _list_url(self, post):
        return f"/api/base/announcement/{post.id}/comments"

    def _detail_url(self, post, comment):
        return f"/api/base/announcement/{post.id}/comments/{comment.id}"

    def test_list_comments(self):
        AnnouncementComment.objects.create(
            announcement_id=self.post_a, employee_id=self.employee_a, comment="Hi"
        )
        response = self.client_a.get(self._list_url(self.post_a))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["comment"], "Hi")

    def test_create_comment(self):
        response = self.client_a.post(
            self._list_url(self.post_a), {"comment": "Great news!"}
        )
        self.assertEqual(response.status_code, 201)
        created = AnnouncementComment.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.employee_a.id)
        self.assertEqual(created.comment, "Great news!")

    def test_empty_comment_rejected(self):
        response = self.client_a.post(self._list_url(self.post_a), {"comment": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            AnnouncementComment.objects.filter(announcement_id=self.post_a).exists()
        )

    def test_over_255_chars_rejected(self):
        response = self.client_a.post(
            self._list_url(self.post_a), {"comment": "x" * 256}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            AnnouncementComment.objects.filter(announcement_id=self.post_a).exists()
        )

    def test_edit_own_comment(self):
        comment = AnnouncementComment.objects.create(
            announcement_id=self.post_a, employee_id=self.employee_a, comment="Old"
        )
        response = self.client_a.patch(
            self._detail_url(self.post_a, comment),
            {"comment": "New"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        comment.refresh_from_db()
        self.assertEqual(comment.comment, "New")

    def test_edit_another_employee_comment_rejected(self):
        comment = AnnouncementComment.objects.create(
            announcement_id=self.post_a, employee_id=self.employee_a, comment="Old"
        )
        response = self.client_a2.patch(
            self._detail_url(self.post_a, comment),
            {"comment": "Hijacked"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        comment.refresh_from_db()
        self.assertEqual(comment.comment, "Old")

    def test_delete_own_comment(self):
        comment = AnnouncementComment.objects.create(
            announcement_id=self.post_a, employee_id=self.employee_a, comment="Bye"
        )
        response = self.client_a.delete(self._detail_url(self.post_a, comment))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(AnnouncementComment.objects.filter(id=comment.id).exists())

    def test_delete_another_employee_comment_rejected(self):
        comment = AnnouncementComment.objects.create(
            announcement_id=self.post_a, employee_id=self.employee_a, comment="Stay"
        )
        response = self.client_a2.delete(self._detail_url(self.post_a, comment))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(AnnouncementComment.objects.filter(id=comment.id).exists())

    def test_cross_company_comment_rejected(self):
        response = self.client_b.post(
            self._list_url(self.post_a), {"comment": "Sneaking in"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            AnnouncementComment.objects.filter(announcement_id=self.post_a).exists()
        )
        list_response = self.client_b.get(self._list_url(self.post_a))
        self.assertEqual(list_response.status_code, 404)

    def test_disable_comments_respected(self):
        self.post_a.disable_comments = True
        self.post_a.save()
        response = self.client_a.post(
            self._list_url(self.post_a), {"comment": "Should be blocked"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            AnnouncementComment.objects.filter(announcement_id=self.post_a).exists()
        )

    def test_public_comments_false_limits_to_own_comments(self):
        self.post_a.public_comments = False
        self.post_a.save()
        AnnouncementComment.objects.create(
            announcement_id=self.post_a, employee_id=self.employee_a, comment="Mine"
        )
        AnnouncementComment.objects.create(
            announcement_id=self.post_a, employee_id=self.employee_a2, comment="Not mine"
        )
        response = self.client_a.get(self._list_url(self.post_a))
        self.assertEqual(response.status_code, 200)
        comments = [c["comment"] for c in response.data["results"]]
        self.assertEqual(comments, ["Mine"])

    def test_authentication_required_for_comments(self):
        anon = APIClient()
        response = anon.get(self._list_url(self.post_a))
        self.assertEqual(response.status_code, 401)
        response = anon.post(self._list_url(self.post_a), {"comment": "x"})
        self.assertEqual(response.status_code, 401)


class AnnouncementVisibilityRuleTests(TestCase):
    """Phase NEWS-FEED-BACKEND-FIX — the one visibility rule.

    The mobile feed came up empty while the web dashboard showed the same
    posts. Two causes, both held down here: the endpoint disagreed with
    `base.announcement.bulletin` about what "still visible" means, and the
    admin form froze the audience of an "everybody" post into
    `Announcement.employees`, so nobody hired afterwards was ever in it.
    """

    def setUp(self):
        self.company = make_company("Visible Co")
        self.other_company = make_company("Other Co")

        self.user = make_user("viewer", password="secret123")
        self.employee = make_employee(
            company=self.company, email="viewer@test.joydigi", user=self.user
        )
        self.client_api = APIClient()
        self.client_api.force_authenticate(user=_reloaded(self.user))

    def _titles(self, response=None):
        response = response or self.client_api.get("/api/base/announcement-view")
        self.assertEqual(response.status_code, 200)
        return [item["title"] for item in response.data["results"]]

    def _item(self, title):
        response = self.client_api.get("/api/base/announcement-view")
        for item in response.data["results"]:
            if item["title"] == title:
                return item
        return None

    # ---------------------------------------------------------------- window

    def test_active_current_post_with_empty_audience_is_visible(self):
        make_announcement(company=self.company, title="Everyone")

        self.assertIn("Everyone", self._titles())

    def test_employee_created_after_the_post_still_sees_an_everyone_post(self):
        """The regression that emptied the feed.

        An "everybody" post must not carry a frozen list of the employees
        who happened to exist when it was published.
        """
        post = make_announcement(company=self.company, title="Everyone")
        self.assertFalse(post.employees.exists())

        latecomer_user = make_user("latecomer", password="secret123")
        make_employee(
            company=self.company,
            email="latecomer@test.joydigi",
            user=latecomer_user,
        )
        client = APIClient()
        client.force_authenticate(user=_reloaded(latecomer_user))

        titles = [i["title"] for i in client.get("/api/base/announcement-view").data["results"]]
        self.assertIn("Everyone", titles)

    def test_expired_post_is_hidden(self):
        make_announcement(
            company=self.company,
            title="Expired",
            expire_date=date.today() - timedelta(days=1),
        )

        self.assertNotIn("Expired", self._titles())

    def test_post_without_expire_date_never_expires(self):
        """NULL means open-ended, the same as it does on the web.

        The endpoint used to backfill these with `created_at + 30 days`
        before filtering — a write on a GET, and one that made an
        open-ended post older than a month vanish from the phone while it
        stayed on the dashboard.
        """
        post = make_announcement(
            company=self.company, title="Open ended", expire_date=None
        )

        self.assertIn("Open ended", self._titles())
        post.refresh_from_db()
        self.assertIsNone(
            post.expire_date,
            msg="a GET must not write an expiry onto an open-ended post",
        )

    def test_post_expiring_today_is_still_visible(self):
        make_announcement(
            company=self.company, title="Last day", expire_date=timezone.localdate()
        )

        self.assertIn("Last day", self._titles())

    def test_inactive_post_is_hidden(self):
        post = make_announcement(company=self.company, title="Soft deleted")
        post.is_active = False
        post.save()

        self.assertNotIn(
            "Soft deleted",
            self._titles(),
            msg="the web bulletin filters is_active; the phone must too",
        )

    # --------------------------------------------------------------- company

    def test_employee_without_company_sees_only_global_posts(self):
        make_announcement(company=self.company, title="Company post")
        globals_ = Announcement.objects.create(
            title="Global post",
            description="<p>hi</p>",
            expire_date=date.today() + timedelta(days=30),
        )
        self.assertFalse(globals_.company_id.exists())

        orphan_user = make_user("orphan", password="secret123")
        orphan = make_employee(
            company=self.company, email="orphan@test.joydigi", user=orphan_user
        )
        # No resolvable company at all — the case that must not 500.
        orphan.employee_work_info.company_id = None
        orphan.employee_work_info.save()

        client = APIClient()
        client.force_authenticate(user=_reloaded(orphan_user))
        response = client.get("/api/base/announcement-view")

        self.assertEqual(response.status_code, 200)
        titles = [i["title"] for i in response.data["results"]]
        self.assertIn("Global post", titles)
        self.assertNotIn("Company post", titles)

    def test_global_post_is_visible_to_a_company_employee_too(self):
        Announcement.objects.create(
            title="Global post",
            description="<p>hi</p>",
            expire_date=date.today() + timedelta(days=30),
        )

        self.assertIn("Global post", self._titles())

    # --------------------------------------------------------------- content

    def test_plain_text_description_is_returned_as_content(self):
        """The seeded announcements looked like this: no HTML at all.

        `find_all` on block tags returned nothing, so the phone got a title
        with an empty body.
        """
        make_announcement(
            company=self.company,
            title="Plain",
            description="Nhân viên vui lòng kiểm tra lịch làm việc.",
        )

        item = self._item("Plain")
        self.assertEqual(
            item["content"],
            [
                {
                    "type": "paragraph",
                    "text": "Nhân viên vui lòng kiểm tra lịch làm việc.",
                }
            ],
        )

    def test_multi_line_plain_text_keeps_its_lines(self):
        make_announcement(
            company=self.company,
            title="Two lines",
            description="Dòng một.\nDòng hai.",
        )

        self.assertEqual(
            [b["text"] for b in self._item("Two lines")["content"]],
            ["Dòng một.", "Dòng hai."],
        )

    def test_html_description_still_parses_into_blocks(self):
        make_announcement(
            company=self.company,
            title="Rich",
            description="<h2>Tiêu đề</h2><p>Đoạn một</p><p>Đoạn hai</p>",
        )

        self.assertEqual(
            self._item("Rich")["content"],
            [
                {"type": "heading", "text": "Tiêu đề"},
                {"type": "paragraph", "text": "Đoạn một"},
                {"type": "paragraph", "text": "Đoạn hai"},
            ],
        )

    def test_empty_blocks_are_dropped(self):
        make_announcement(
            company=self.company,
            title="Padded",
            description="<p></p><p>Thật</p><p>   </p>",
        )

        self.assertEqual(
            self._item("Padded")["content"],
            [{"type": "paragraph", "text": "Thật"}],
        )

    def test_inline_markup_stays_one_paragraph(self):
        make_announcement(
            company=self.company,
            title="Inline",
            description="Xin <b>chào</b> cả nhà",
        )

        self.assertEqual(
            self._item("Inline")["content"],
            [{"type": "paragraph", "text": "Xin chào cả nhà"}],
        )

    def test_blank_description_returns_empty_content(self):
        make_announcement(company=self.company, title="No body", description="")

        self.assertEqual(self._item("No body")["content"], [])

    # ------------------------------------------------------- shape / ordering

    def test_pinned_posts_come_first(self):
        make_announcement(company=self.company, title="Plain one")
        make_announcement(company=self.company, title="Pinned one", is_pinned=True)

        self.assertEqual(self._titles()[0], "Pinned one")

    def test_envelope_shape_is_what_the_app_parses(self):
        make_announcement(company=self.company, title="Shape")
        response = self.client_api.get("/api/base/announcement-view")

        self.assertEqual(
            sorted(response.data.keys()), ["count", "next", "previous", "results"]
        )
        item = response.data["results"][0]
        for key in ("id", "title", "content", "created_at", "is_pinned"):
            self.assertIn(key, item)

    def test_a_post_appears_once_even_with_several_matching_rows(self):
        """distinct(): company and audience are both M2M joins."""
        post = make_announcement(company=self.company, title="Joined")
        post.company_id.add(self.other_company)
        post.employees.set([self.employee])
        other_employee = make_employee(
            company=self.company, email="second@test.joydigi"
        )
        post.employees.add(other_employee)

        self.assertEqual(self._titles().count("Joined"), 1)
