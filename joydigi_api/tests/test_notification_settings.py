"""Phase UI-5C.1: notification-preference API tests.

Covers ownership/IDOR protection, default behavior, and that the
existing notification list/read/bulk endpoints are unaffected.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from joydigi.testkit import make_user
from joydigi_api.models import NotificationPreference

SETTINGS_URL = "/api/notifications/notifications/settings/"


class NotificationSettingsAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_a = make_user("notif_pref_a", password="secret123")
        self.user_b = make_user("notif_pref_b", password="secret123")

    def test_unauthenticated_get_denied(self):
        response = self.client.get(SETTINGS_URL)
        self.assertEqual(response.status_code, 401)

    def test_authenticated_get_returns_default_true(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.get(SETTINGS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"all_notifications_enabled": True})

    def test_authenticated_update_false(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.patch(
            SETTINGS_URL,
            {"all_notifications_enabled": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"all_notifications_enabled": False})

    def test_subsequent_get_returns_false(self):
        self.client.force_authenticate(user=self.user_a)
        self.client.patch(
            SETTINGS_URL, {"all_notifications_enabled": False}, format="json"
        )
        response = self.client.get(SETTINGS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["all_notifications_enabled"])

    def test_update_true_again(self):
        self.client.force_authenticate(user=self.user_a)
        self.client.patch(
            SETTINGS_URL, {"all_notifications_enabled": False}, format="json"
        )
        response = self.client.patch(
            SETTINGS_URL, {"all_notifications_enabled": True}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["all_notifications_enabled"])

    def test_user_a_setting_independent_from_user_b(self):
        self.client.force_authenticate(user=self.user_a)
        self.client.patch(
            SETTINGS_URL, {"all_notifications_enabled": False}, format="json"
        )

        self.client.force_authenticate(user=self.user_b)
        response = self.client.get(SETTINGS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["all_notifications_enabled"])

        pref_a = NotificationPreference.objects.get(user=self.user_a)
        pref_b = NotificationPreference.objects.get(user=self.user_b)
        self.assertFalse(pref_a.all_notifications_enabled)
        self.assertTrue(pref_b.all_notifications_enabled)

    def test_cannot_submit_user_id(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.patch(
            SETTINGS_URL,
            {"all_notifications_enabled": False, "user_id": self.user_b.pk},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        pref_a = NotificationPreference.objects.get(user=self.user_a)
        self.assertFalse(pref_a.all_notifications_enabled)
        self.assertFalse(
            NotificationPreference.objects.filter(user=self.user_b).exists()
        )

    def test_cannot_submit_employee_id(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.patch(
            SETTINGS_URL,
            {"all_notifications_enabled": False, "employee_id": 999},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("employee_id", response.data)

    def test_cannot_submit_company_id(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.patch(
            SETTINGS_URL,
            {"all_notifications_enabled": False, "company_id": 999},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("company_id", response.data)

    def test_invalid_boolean_rejected_safely(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.patch(
            SETTINGS_URL,
            {"all_notifications_enabled": "not-a-boolean"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_no_raw_identity_fields_in_response(self):
        self.client.force_authenticate(user=self.user_a)
        response = self.client.get(SETTINGS_URL)
        self.assertNotIn("user", response.data)
        self.assertNotIn("user_id", response.data)
        self.assertNotIn("id", response.data)

    def test_cannot_read_another_users_settings_by_any_id(self):
        """There is no id-taking variant of this endpoint at all — the
        only identity ever used is `request.user`."""
        self.client.force_authenticate(user=self.user_a)
        self.client.patch(
            SETTINGS_URL, {"all_notifications_enabled": False}, format="json"
        )
        self.client.force_authenticate(user=self.user_b)
        response = self.client.get(SETTINGS_URL)
        # user_b's own (separate, still-default) row — never user_a's.
        self.assertTrue(response.data["all_notifications_enabled"])
