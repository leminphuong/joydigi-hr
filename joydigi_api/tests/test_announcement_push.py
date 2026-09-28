"""
Phase NEWS-FEED-BACKEND-FIX — notification and push for a new bulletin.

`AnnouncementFormView` already recorded an in-app notification when a post
was published, but nothing ever pushed it: `joydigi_api.push.send_to_user`
was reached only from the attendance reminders. These tests hold the new
behaviour and, just as importantly, the things it must not do — announce an
edit, ring a phone for a notification the app does not have, or let a
Firebase failure cost the admin their post.

The view's `form_valid` is driven directly rather than over HTTP: it is the
whole unit under test, and going through the HTMX form-rendering plumbing
would test that instead.
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from base.cbv.announcement_cbv import (
    ANNOUNCEMENT_PUSH_TITLE,
    AnnouncementFormView,
    announcement_notifications,
)
from base.forms import AnnouncementForm
from base.models import Announcement, Department, JobPosition
from joydigi.joydigi_middlewares import _thread_locals
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api.models import NotificationPreference, PushDeviceToken
from joydigi_auth.models import JoydigiUser

PUSH_TARGET = "joydigi_api.push.send_to_user"


def _reloaded(user):
    """See the note in `test_announcement_feed_api._reloaded`."""
    return JoydigiUser.objects.get(pk=user.pk)


class AnnouncementPushTestCase(TestCase):
    """Shared harness: an admin who can post, and two employees to reach."""

    def setUp(self):
        self.company = make_company("Push Co")

        self.admin_user = make_user("poster", password="secret123")
        self.admin_employee = make_employee(
            company=self.company, email="poster@test.joydigi", user=self.admin_user
        )
        self.admin_user.user_permissions.add(
            Permission.objects.get(codename="add_announcement")
        )

        # `employee_one` sits in its own department, which is now the only
        # way an administrator can narrow a bulletin's audience: the
        # per-employee selector was removed from the form.
        # `Department.company_id` is a ManyToMany, so it is attached after
        # create — the same pattern the attendance fixtures use.
        self.department = Department.objects.create(department="Ky thuat")
        self.department.company_id.add(self.company)
        self.user_one = make_user("recipient_one", password="secret123")
        self.employee_one = make_employee(
            company=self.company,
            email="one@test.joydigi",
            user=self.user_one,
            department=self.department,
        )
        self.user_two = make_user("recipient_two", password="secret123")
        self.employee_two = make_employee(
            company=self.company,
            email="two@test.joydigi",
            user=self.user_two,
        )

        self.factory = RequestFactory()

    # -------------------------------------------------------------- harness

    def form_data(self, **overrides):
        data = {
            "title": "Lịch làm việc tuần mới",
            "description": "Các nhóm kiểm tra lịch làm việc.",
            "expire_date": (date.today() + timedelta(days=30)).isoformat(),
            "company_id": [self.company.pk],
            # A checkbox absent from a POST reads as False, so the flags
            # under test have to be sent explicitly.
            "send_notification": "on",
        }
        data.update(overrides)
        return data

    def submit(self, instance=None, execute_commit=True, **overrides):
        """Publish (or re-save) a bulletin the way the admin form does."""
        form = (
            AnnouncementForm(self.form_data(**overrides), instance=instance)
            if instance is not None
            else AnnouncementForm(self.form_data(**overrides))
        )
        self.assertTrue(form.is_valid(), msg=form.errors.as_json())

        request = self.factory.post("/create-announcement/", HTTP_REFERER="/")
        request.user = _reloaded(self.admin_user)
        request.session = {}
        request._messages = FallbackStorage(request)

        # `JoydigiFormView.__init__` resolves its request from the
        # middleware's thread-local, not from a constructor argument, so a
        # test that instantiates the view has to stand in for the
        # middleware. Cleared afterwards so nothing leaks into the next
        # test through the same thread.
        _thread_locals.request = request
        try:
            view = AnnouncementFormView()
            view.request = request
            with self.captureOnCommitCallbacks(execute=execute_commit) as callbacks:
                view.form_valid(form)
        finally:
            _thread_locals.request = None
        return callbacks

    def recipients_notified(self, announcement):
        return set(
            announcement_notifications(announcement).values_list(
                "recipient_id", flat=True
            )
        )

    def latest(self):
        return Announcement.objects.entire().order_by("-pk").first()


class NewAnnouncementNotifiesTests(AnnouncementPushTestCase):
    def test_create_records_one_notification_per_recipient(self):
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()

        announcement = self.latest()
        self.assertEqual(
            self.recipients_notified(announcement),
            {self.user_one.pk, self.user_two.pk, self.admin_user.pk},
        )
        self.assertEqual(
            announcement_notifications(announcement).count(),
            3,
            msg="exactly one row each, no duplicates",
        )

    def test_create_pushes_once_per_recipient(self):
        with mock.patch(
            PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}
        ) as send:
            self.submit()

        pushed = {call.args[0].pk for call in send.call_args_list}
        self.assertEqual(
            pushed, {self.user_one.pk, self.user_two.pk, self.admin_user.pk}
        )
        self.assertEqual(send.call_count, 3)

    def test_push_payload(self):
        with mock.patch(
            PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}
        ) as send:
            self.submit()

        announcement = self.latest()
        _user, title, body = send.call_args_list[0].args
        data = send.call_args_list[0].kwargs["data"]

        self.assertEqual(title, ANNOUNCEMENT_PUSH_TITLE)
        self.assertEqual(title, "Thông báo mới từ JOYDIGI")
        self.assertEqual(body, announcement.title)
        self.assertEqual(data["type"], "NEWS")
        self.assertEqual(data["post_id"], str(announcement.pk))
        self.assertIsInstance(data["post_id"], str)
        self.assertNotIn(
            announcement.description,
            [title, body],
            msg="the body of the post never goes into a push",
        )

    def test_empty_audience_does_not_freeze_the_employee_list(self):
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()

        announcement = self.latest()
        self.assertFalse(
            announcement.employees.exists(),
            msg=(
                "an everybody post must keep employees empty, or the audience "
                "is frozen at publication time and the feed goes empty for "
                "anyone hired later"
            ),
        )

    def test_audience_subset_reaches_only_that_subset(self):
        """Targeting a department reaches that department and nobody else.

        Since the per-employee selector was removed, a department (or a job
        position) is the only narrowing an administrator can express — so
        this is the test that it is honoured rather than silently widened
        to the whole company.
        """
        with mock.patch(
            PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}
        ) as send:
            self.submit(department=[self.department.pk])

        announcement = self.latest()
        self.assertEqual(self.recipients_notified(announcement), {self.user_one.pk})
        self.assertEqual({c.args[0].pk for c in send.call_args_list}, {self.user_one.pk})
        self.assertEqual(
            set(announcement.employees.values_list("pk", flat=True)),
            {self.employee_one.pk},
            msg=(
                "the resolved department members are materialised into "
                "`employees`, because that is the set the visibility "
                "filters read"
            ),
        )
        self.assertNotIn(
            self.user_two.pk,
            self.recipients_notified(announcement),
            msg="somebody outside the department must not be told",
        )


class EditAnnouncementDoesNotNotifyTests(AnnouncementPushTestCase):
    def test_editing_sends_nothing(self):
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()
        announcement = self.latest()
        before = announcement_notifications(announcement).count()

        with mock.patch(
            PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}
        ) as send:
            self.submit(instance=announcement, title="Lịch làm việc tuần mới (sửa)")

        self.assertEqual(
            announcement_notifications(announcement).count(),
            before,
            msg="fixing a typo must not tell the whole company again",
        )
        send.assert_not_called()

    def test_deleting_sends_nothing(self):
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()
        announcement = self.latest()

        with mock.patch(PUSH_TARGET) as send:
            announcement.delete()

        send.assert_not_called()


class NotificationOptOutTests(AnnouncementPushTestCase):
    def test_send_notification_false_records_and_pushes_nothing(self):
        with mock.patch(PUSH_TARGET) as send:
            # Absent checkbox == unchecked.
            self.submit(send_notification="")

        announcement = self.latest()
        self.assertEqual(announcement_notifications(announcement).count(), 0)
        send.assert_not_called()

    def test_disabled_preference_gets_neither_notification_nor_push(self):
        NotificationPreference.objects.create(
            user=self.user_one, all_notifications_enabled=False
        )

        with mock.patch(
            PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}
        ) as send:
            self.submit()

        announcement = self.latest()
        notified = self.recipients_notified(announcement)
        self.assertNotIn(self.user_one.pk, notified)
        self.assertIn(self.user_two.pk, notified)
        self.assertNotIn(
            self.user_one.pk,
            {call.args[0].pk for call in send.call_args_list},
            msg=(
                "a phone must never ring for a notification the in-app list "
                "does not have"
            ),
        )


class PushFailureIsolationTests(AnnouncementPushTestCase):
    def test_no_active_token_still_leaves_the_notification(self):
        # Real push path, no device registered anywhere.
        self.submit()

        announcement = self.latest()
        self.assertIsNotNone(announcement)
        self.assertEqual(announcement_notifications(announcement).count(), 3)

    def test_firebase_not_configured_still_leaves_the_notification(self):
        PushDeviceToken.objects.create(
            user=self.user_one,
            token="token-for-one",
            platform=PushDeviceToken.ANDROID,
        )

        # Real push path: this test environment has no Firebase credential,
        # so `send_to_user` reports PUSH_SKIPPED_FIREBASE_NOT_CONFIGURED.
        self.submit()

        announcement = self.latest()
        self.assertIsNotNone(announcement)
        self.assertIn(self.user_one.pk, self.recipients_notified(announcement))

    def test_a_raising_push_does_not_cost_the_post(self):
        with mock.patch(PUSH_TARGET, side_effect=RuntimeError("firebase down")):
            self.submit()

        announcement = self.latest()
        self.assertIsNotNone(
            announcement, msg="the post is the admin's work; a push is not"
        )
        self.assertEqual(announcement_notifications(announcement).count(), 3)

    def test_push_waits_for_the_commit(self):
        with mock.patch(PUSH_TARGET) as send:
            callbacks = self.submit(execute_commit=False)

        self.assertEqual(
            len(callbacks), 1, msg="registered as an on_commit callback"
        )
        send.assert_not_called()

    def test_a_rolled_back_publish_pushes_nothing(self):
        with mock.patch(PUSH_TARGET) as send:
            with mock.patch(
                "base.cbv.announcement_cbv.announcement_notifications",
                side_effect=RuntimeError("db exploded"),
            ):
                with self.assertRaises(RuntimeError):
                    self.submit()

        send.assert_not_called()
        self.assertFalse(
            Announcement.objects.entire()
            .filter(title="Lịch làm việc tuần mới")
            .exists(),
            msg="the atomic block rolled the post back, so nothing was announced",
        )


class NotificationDedupeTests(AnnouncementPushTestCase):
    def test_re_recording_the_same_announcement_adds_nothing(self):
        """A retry on the same post notifies nobody twice.

        This is the guarantee the `action_object` lookup buys: the rows
        already written for this announcement are found and skipped.
        """
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()
        announcement = self.latest()
        self.assertEqual(announcement_notifications(announcement).count(), 3)

        request = self.factory.post("/create-announcement/", HTTP_REFERER="/")
        request.user = _reloaded(self.admin_user)
        _thread_locals.request = request
        try:
            view = AnnouncementFormView()
            view.request = request
            audience = type(self.employee_one).objects.filter(is_active=True)
            again = view._record_announcement_notification(announcement, audience)
        finally:
            _thread_locals.request = None

        self.assertEqual(again, ())
        self.assertEqual(
            announcement_notifications(announcement).count(),
            3,
            msg="no second row for anybody",
        )


class PushLoggingTests(AnnouncementPushTestCase):
    def test_no_raw_token_is_logged(self):
        PushDeviceToken.objects.create(
            user=self.user_one,
            token="SECRET-REGISTRATION-TOKEN-VALUE",
            platform=PushDeviceToken.IOS,
        )

        with self.assertLogs("base.cbv.announcement_cbv", level="INFO") as logs:
            self.submit()

        output = "\n".join(logs.output)
        self.assertNotIn(
            "SECRET-REGISTRATION-TOKEN-VALUE",
            output,
            msg="a registration token is a credential for reaching a device",
        )
        self.assertIn("announcement push for user", output)


class AnnouncementFormAudienceTests(AnnouncementPushTestCase):
    """Phase NEWS-FEED-FORM-CLEANUP — no more picking employees by hand.

    The bulletin form used to offer a "Nhân viên nhận bản tin" selector, and
    whatever it produced was written straight into `Announcement.employees`
    — the set every visibility filter reads. Removing it leaves department
    and job position as the only narrowing an administrator can express, and
    leaving both blank as the only way to say "everybody".
    """

    def test_form_no_longer_offers_an_employee_selector(self):
        form = AnnouncementForm()

        self.assertNotIn("employees", form.fields)
        self.assertNotIn("employees", [bf.name for bf in form.visible_fields()])
        self.assertNotIn(
            "Nhân viên nhận bản tin",
            [str(f.label) for f in form.fields.values()],
        )

    def test_the_model_field_is_untouched(self):
        """Removed from the form, kept on the model — no migration."""
        self.assertTrue(
            any(f.name == "employees" for f in Announcement._meta.get_fields())
        )

    def test_blank_audience_leaves_employees_empty(self):
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()

        announcement = self.latest()
        self.assertFalse(
            announcement.employees.exists(),
            msg="blank means everybody, and the set has to stay empty to say it",
        )
        self.assertEqual(
            self.recipients_notified(announcement),
            {self.admin_user.pk, self.user_one.pk, self.user_two.pk},
        )

    def test_department_target_resolves_into_employees(self):
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit(department=[self.department.pk])

        announcement = self.latest()
        self.assertEqual(
            set(announcement.employees.values_list("pk", flat=True)),
            {self.employee_one.pk},
        )
        self.assertEqual(
            set(announcement.department.values_list("pk", flat=True)),
            {self.department.pk},
        )

    def test_job_position_target_resolves_into_employees(self):
        position = JobPosition.objects.create(
            job_position="Truong nhom", department_id=self.department
        )
        info = self.employee_two.employee_work_info
        info.job_position_id = position
        info.save()

        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit(job_position=[position.pk])

        announcement = self.latest()
        self.assertEqual(
            set(announcement.employees.values_list("pk", flat=True)),
            {self.employee_two.pk},
        )

    def test_department_and_job_position_together_are_a_union(self):
        """Documented, not changed: both selected reaches either, not both."""
        position = JobPosition.objects.create(
            job_position="Truong nhom", department_id=self.department
        )
        info = self.employee_two.employee_work_info
        info.job_position_id = position
        info.save()

        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit(
                department=[self.department.pk], job_position=[position.pk]
            )

        announcement = self.latest()
        self.assertEqual(
            set(announcement.employees.values_list("pk", flat=True)),
            {self.employee_one.pk, self.employee_two.pk},
            msg="UNION — employee_one by department, employee_two by position",
        )

    def test_create_does_not_raise_without_the_employees_key(self):
        """The view used to read `cleaned_data["employees"]` directly."""
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()

        self.assertIsNotNone(self.latest())

    def test_update_does_not_raise_without_the_employees_key(self):
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()
        announcement = self.latest()

        with mock.patch(PUSH_TARGET) as send:
            self.submit(instance=announcement, title="Da sua tieu de")

        announcement.refresh_from_db()
        self.assertEqual(announcement.title, "Da sua tieu de")
        send.assert_not_called()

    def test_narrowing_an_existing_post_actually_narrows_it(self):
        """`set`, not `add`: editing must be able to shrink the audience."""
        with mock.patch(PUSH_TARGET, return_value={"status": "PUSH_SEND_SUCCESS"}):
            self.submit()
        announcement = self.latest()
        self.assertFalse(announcement.employees.exists())

        with mock.patch(PUSH_TARGET):
            self.submit(instance=announcement, department=[self.department.pk])

        self.assertEqual(
            set(announcement.employees.values_list("pk", flat=True)),
            {self.employee_one.pk},
        )
