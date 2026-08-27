"""Phase UI-5C.1 Section 16: confirms the existing notification list/
read/bulk endpoints are unchanged by the new settings endpoint."""

from django.test import TestCase
from notifications.signals import notify
from rest_framework.test import APIClient

from joydigi.testkit import make_company, make_employee, make_user


class NotificationListRegressionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Notif Regression Co")
        self.password = "secret123"
        self.user = make_user("notif_regress", password=self.password)
        self.employee = make_employee(
            company=self.company,
            email="notif_regress@test.joydigi",
            user=self.user,
        )
        self.client.force_authenticate(user=self.user)
        notify.send(
            self.employee,
            recipient=self.user,
            verb="Regression test notification",
        )

    def test_list_all(self):
        response = self.client.get("/api/notifications/notifications/list/all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

    def test_list_unread(self):
        response = self.client.get("/api/notifications/notifications/list/unread")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

    def test_mark_one_read(self):
        notif_id = self.user.notifications.first().id
        response = self.client.post(f"/api/notifications/notifications/{notif_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["unread"])

    def test_bulk_read(self):
        response = self.client.post("/api/notifications/notifications/bulk-read/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.user.notifications.unread().count(), 0)

    def test_bulk_delete(self):
        response = self.client.delete("/api/notifications/notifications/bulk-delete/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.user.notifications.active().count(), 0)

    def test_bulk_delete_unread(self):
        response = self.client.delete(
            "/api/notifications/notifications/bulk-delete-unread/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.user.notifications.unread().count(), 0)
        self.assertEqual(self.user.notifications.deleted().count(), 1)
