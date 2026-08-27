"""Phase UI-5C.1A: NotificationPreference enforcement at the single
central `notify.send(...)` -> `notify_handler` creation seam.

No direct `Notification.objects.create(...)` bypass exists anywhere in
the repo (confirmed by audit) — `notify_handler` is the one and only
receiver connected to the `notify` signal, so testing through
`notify.send(...)` exercises the real production path used by every
feature (attendance, leave, announcements, ...).
"""

from django.test import TestCase
from notifications.signals import notify
from rest_framework.test import APIClient

from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api.models import NotificationPreference

SETTINGS_URL = "/api/notifications/notifications/settings/"


class NotificationPreferenceEnforcementTests(TestCase):
    def setUp(self):
        self.company = make_company("Notif Enforcement Co")
        self.user_a = make_user("notif_enforce_a", password="secret123")
        self.employee_a = make_employee(
            company=self.company,
            email="notif_enforce_a@test.joydigi",
            user=self.user_a,
        )

    def test_default_no_preference_row_creates_notification(self):
        """A recipient with no NotificationPreference row is treated
        as enabled — no regression for existing users."""
        self.assertFalse(
            NotificationPreference.objects.filter(user=self.user_a).exists()
        )
        notify.send(self.employee_a, recipient=self.user_a, verb="Routine event")
        self.assertEqual(self.user_a.notifications.count(), 1)

    def test_enabled_preference_creates_notification(self):
        NotificationPreference.objects.create(
            user=self.user_a, all_notifications_enabled=True
        )
        notify.send(self.employee_a, recipient=self.user_a, verb="Routine event")
        self.assertEqual(self.user_a.notifications.count(), 1)

    def test_disabled_preference_skips_notification(self):
        NotificationPreference.objects.create(
            user=self.user_a, all_notifications_enabled=False
        )
        notify.send(self.employee_a, recipient=self.user_a, verb="Routine event")
        self.assertEqual(self.user_a.notifications.count(), 0)

    def test_multi_recipient_filters_per_recipient(self):
        user_b = make_user("notif_enforce_b", password="secret123")
        make_employee(
            company=self.company, email="notif_enforce_b@test.joydigi", user=user_b
        )
        user_c = make_user("notif_enforce_c", password="secret123")
        make_employee(
            company=self.company, email="notif_enforce_c@test.joydigi", user=user_c
        )

        NotificationPreference.objects.create(
            user=self.user_a, all_notifications_enabled=True
        )
        NotificationPreference.objects.create(
            user=user_b, all_notifications_enabled=False
        )
        # user_c: no row at all.

        notify.send(
            self.employee_a,
            recipient=[self.user_a, user_b, user_c],
            verb="Routine broadcast",
        )

        self.assertEqual(self.user_a.notifications.count(), 1)
        self.assertEqual(user_b.notifications.count(), 0)
        self.assertEqual(user_c.notifications.count(), 1)

    def test_old_history_untouched_when_disabled(self):
        # An existing notification created while still enabled.
        notify.send(self.employee_a, recipient=self.user_a, verb="Old event")
        old = self.user_a.notifications.first()
        old.mark_as_read()
        old_id = old.id

        NotificationPreference.objects.create(
            user=self.user_a, all_notifications_enabled=False
        )

        # New routine send is skipped...
        notify.send(self.employee_a, recipient=self.user_a, verb="New event")
        self.assertEqual(self.user_a.notifications.count(), 1)

        # ...but the old row is completely unchanged.
        old.refresh_from_db()
        self.assertEqual(old.id, old_id)
        self.assertFalse(old.unread)
        self.assertFalse(old.deleted)
        self.assertEqual(old.verb, "Old event")

    def test_re_enable_resumes_future_delivery_without_replay(self):
        preference = NotificationPreference.objects.create(
            user=self.user_a, all_notifications_enabled=False
        )
        notify.send(self.employee_a, recipient=self.user_a, verb="Skipped event")
        self.assertEqual(self.user_a.notifications.count(), 0)

        preference.all_notifications_enabled = True
        preference.save()

        notify.send(self.employee_a, recipient=self.user_a, verb="Delivered event")
        notifications = list(self.user_a.notifications.all())
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].verb, "Delivered event")
        # The earlier skipped one was never retroactively created.
        self.assertFalse(
            self.user_a.notifications.filter(verb="Skipped event").exists()
        )

    def test_force_delivery_bypasses_disabled_preference(self):
        """Trusted server-side-only mechanism for a future security
        alert — never reachable from the mobile client."""
        NotificationPreference.objects.create(
            user=self.user_a, all_notifications_enabled=False
        )
        notify.send(self.employee_a, recipient=self.user_a, verb="Routine event")
        self.assertEqual(self.user_a.notifications.count(), 0)

        notify.send(
            self.employee_a,
            recipient=self.user_a,
            verb="Security alert",
            force_delivery=True,
        )
        self.assertEqual(self.user_a.notifications.count(), 1)
        self.assertEqual(self.user_a.notifications.first().verb, "Security alert")

    def test_mobile_settings_api_cannot_set_force_delivery(self):
        """The Notification Settings API's serializer has no such
        field — posting it is simply ignored, same as any other
        unknown key."""
        client = APIClient()
        client.force_authenticate(user=self.user_a)
        response = client.patch(
            SETTINGS_URL,
            {"all_notifications_enabled": True, "force_delivery": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"all_notifications_enabled": True})
        preference = NotificationPreference.objects.get(user=self.user_a)
        self.assertFalse(hasattr(preference, "force_delivery"))
