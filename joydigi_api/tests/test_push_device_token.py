"""
Phase ATTENDANCE-PUSH-NOTIFICATION-FCM-SAFE-IMPLEMENT-1.

Registering a device for push, and sending to it.

Two things are load-bearing here. The first is ownership: a client must
never be able to register a token against somebody else's account, or
they would receive that person's reminders. The second is that push is
strictly an extra channel — no Firebase, no credentials, no network, a
dead token, none of it may raise into the caller, because the caller is a
scheduler loop reminding everybody in the company.

Firebase is mocked throughout. Nothing here contacts Google.
"""

import sys
import types
from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api import push as push_module
from joydigi_api.models import PushDeviceToken

# The notifications include is itself mounted under "notifications/",
# so the full path repeats the segment — matching every other route
# in this app rather than inventing a tidier one.
ENDPOINT = "/api/notifications/notifications/device-token/"


class PushDeviceTokenAPITests(TestCase):
    def setUp(self):
        self.company = make_company("Push Co")
        self.user = make_user("pushuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="push@test.joydigi", user=self.user
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def register(self, **payload):
        body = {"token": "tok-1", "platform": "android"}
        body.update(payload)
        return self.client.post(ENDPOINT, body)

    def test_registering_stores_the_token_for_the_caller(self):
        response = self.register()

        self.assertEqual(response.status_code, 200, response.data)
        device = PushDeviceToken.objects.get(token="tok-1")
        self.assertEqual(device.user, self.user)
        self.assertEqual(device.platform, "android")
        self.assertTrue(device.is_active)

    def test_registering_the_same_token_twice_is_idempotent(self):
        self.register()
        self.register()
        self.assertEqual(PushDeviceToken.objects.filter(token="tok-1").count(), 1)

    def test_one_user_may_register_several_devices(self):
        self.register(token="phone", platform="android")
        self.register(token="tablet", platform="ios")
        self.assertEqual(
            PushDeviceToken.objects.filter(user=self.user, is_active=True).count(), 2
        )

    def test_a_token_is_moved_when_someone_else_signs_in_on_that_device(self):
        # Firebase hands the same registration token to whoever is signed
        # in on the handset. If the row stayed with the first user, the
        # second user's reminders would go to the first user's session.
        self.register(token="shared-handset")

        other_user = make_user("otherpushuser", password="secret123")
        make_employee(
            company=self.company, email="other-push@test.joydigi", user=other_user
        )
        other_client = APIClient()
        other_client.force_authenticate(user=other_user)
        other_client.post(
            ENDPOINT, {"token": "shared-handset", "platform": "android"}
        )

        device = PushDeviceToken.objects.get(token="shared-handset")
        self.assertEqual(device.user, other_user)
        self.assertEqual(PushDeviceToken.objects.count(), 1)

    def test_the_owner_cannot_be_chosen_by_the_client(self):
        other_user = make_user("victim", password="secret123")
        self.client.post(
            ENDPOINT,
            {"token": "tok-x", "platform": "android", "user": other_user.pk},
        )
        self.assertEqual(PushDeviceToken.objects.get(token="tok-x").user, self.user)

    def test_an_anonymous_caller_is_rejected(self):
        anonymous = APIClient()
        response = anonymous.post(ENDPOINT, {"token": "t", "platform": "android"})
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(PushDeviceToken.objects.exists())

    def test_a_missing_token_is_rejected(self):
        response = self.register(token="")
        self.assertEqual(response.status_code, 400)

    def test_an_unknown_platform_is_rejected(self):
        response = self.register(platform="symbian")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PushDeviceToken.objects.exists())

    def test_logging_out_deactivates_only_that_device(self):
        self.register(token="phone")
        self.register(token="tablet")

        response = self.client.delete(ENDPOINT, {"token": "phone"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(PushDeviceToken.objects.get(token="phone").is_active)
        self.assertTrue(PushDeviceToken.objects.get(token="tablet").is_active)

    def test_logging_out_cannot_deactivate_another_users_device(self):
        other_user = make_user("otherowner", password="secret123")
        PushDeviceToken.objects.create(
            user=other_user, token="not-mine", platform="android"
        )

        response = self.client.delete(ENDPOINT, {"token": "not-mine"}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(PushDeviceToken.objects.get(token="not-mine").is_active)

    def test_logging_out_an_unknown_token_is_not_an_error(self):
        response = self.client.delete(ENDPOINT, {"token": "ghost"}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_registering_again_reactivates_a_logged_out_device(self):
        self.register(token="phone")
        self.client.delete(ENDPOINT, {"token": "phone"}, format="json")
        self.register(token="phone")
        self.assertTrue(PushDeviceToken.objects.get(token="phone").is_active)


class PushSenderTests(TestCase):
    def setUp(self):
        self.company = make_company("Sender Co")
        self.user = make_user("senderuser", password="secret123")
        make_employee(
            company=self.company, email="sender@test.joydigi", user=self.user
        )
        # Each test decides for itself whether Firebase is "configured".
        push_module._app = None
        push_module._app_attempted = False
        self.addCleanup(self._reset_module_state)

    def _reset_module_state(self):
        push_module._app = None
        push_module._app_attempted = False

    def token(self, value, active=True):
        return PushDeviceToken.objects.create(
            user=self.user, token=value, platform="android", is_active=active
        )

    def fake_firebase(self, send=None):
        """
        Stand in for the `firebase_admin` package.

        The real SDK is an optional dependency and is not installed on a
        developer machine, so there is no attribute to patch — the module
        itself has to be supplied. `Message` keeps its keyword arguments
        so a test can tell which token a send was for.
        """
        messaging = types.SimpleNamespace(
            send=mock.Mock(side_effect=send) if send else mock.Mock(),
            Message=lambda **kwargs: mock.Mock(**kwargs),
            Notification=lambda **kwargs: mock.Mock(**kwargs),
        )
        package = types.ModuleType("firebase_admin")
        package.messaging = messaging
        return messaging, mock.patch.dict(
            sys.modules,
            {"firebase_admin": package, "firebase_admin.messaging": messaging},
        )

    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_without_firebase_configured_nothing_is_sent_and_nothing_raises(self):
        self.token("tok")
        result = push_module.send_to_user(self.user, "t", "b")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["sent"], 0)

    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_is_configured_is_false_without_a_credentials_file(self):
        self.assertFalse(push_module.is_configured())

    @override_settings(FIREBASE_CREDENTIALS_FILE="/nowhere/creds.json")
    def test_an_unreadable_credentials_file_disables_push_quietly(self):
        self.token("tok")
        result = push_module.send_to_user(self.user, "t", "b")
        self.assertTrue(result["skipped"])

    def test_a_user_with_no_device_is_skipped(self):
        result = push_module.send_to_user(self.user, "t", "b")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["sent"], 0)

    def test_an_inactive_device_is_not_sent_to(self):
        self.token("retired", active=False)
        result = push_module.send_to_user(self.user, "t", "b")
        self.assertTrue(result["skipped"])

    def test_every_active_device_receives_the_reminder_once(self):
        self.token("phone")
        self.token("tablet")
        messaging, patched = self.fake_firebase()

        with mock.patch.object(push_module, "_load_app", return_value=object()):
            with patched:
                result = push_module.send_to_user(self.user, "Title", "Body")

        self.assertEqual(result["sent"], 2)
        self.assertEqual(messaging.send.call_count, 2)
        sent_to = {call.args[0].token for call in messaging.send.call_args_list}
        self.assertEqual(sent_to, {"phone", "tablet"})

    def test_one_dead_token_does_not_stop_the_others(self):
        self.token("good-1")
        self.token("dead")
        self.token("good-2")

        def send(message, app=None):
            if getattr(message, "token", None) == "dead":
                raise Exception("Requested entity was not found. NotRegistered")
            return "ok"

        _messaging, patched = self.fake_firebase(send=send)

        with mock.patch.object(push_module, "_load_app", return_value=object()):
            with patched:
                result = push_module.send_to_user(self.user, "Title", "Body")

        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["deactivated"], 1)
        self.assertFalse(PushDeviceToken.objects.get(token="dead").is_active)
        self.assertTrue(PushDeviceToken.objects.get(token="good-1").is_active)
        self.assertTrue(PushDeviceToken.objects.get(token="good-2").is_active)

    def test_a_transient_failure_does_not_retire_the_token(self):
        self.token("flaky")

        def send(message, app=None):
            raise Exception("503 Service Unavailable")

        _messaging, patched = self.fake_firebase(send=send)

        with mock.patch.object(push_module, "_load_app", return_value=object()):
            with patched:
                result = push_module.send_to_user(self.user, "Title", "Body")

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["deactivated"], 0)
        self.assertTrue(PushDeviceToken.objects.get(token="flaky").is_active)
