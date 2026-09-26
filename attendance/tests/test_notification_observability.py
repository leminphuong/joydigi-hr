"""Phase NOTIFY-2 — you can tell why a notification did not arrive.

Before this phase the notification path failed in silence. `send_to_user`
answered "nobody has registered a device" and "this deployment has no
Firebase credential" with the same `skipped=True` and no log line
anywhere, and because the project configured no `LOGGING` at all, even
the one `logger.info` that existed reached nothing — Python's
`lastResort` handler emits WARNING and above. Push could be switched off
for weeks and leave no evidence.

Two things are pinned here. The first is that every outcome now has a
name a caller can count. The second matters more and is the reason the
order inside `_send_reminder` is what it is:

    the in-app notification is written FIRST, and no push failure may
    take it away.

That row is not only what the employee sees in the app — it is also the
deduplication marker. If a Firebase outage could roll it back, the next
run would find no marker and send the reminder again, and again, for as
long as the outage lasted.

Nothing here contacts Google. Firebase is a stand-in module throughout.
"""

import sys
import types
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from attendance import scheduler as scheduler_module
from attendance.methods import reminders
from attendance.methods.reminders import STAGE_START_MINUS_5
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api import push as push_module
from joydigi_api.models import PushDeviceToken
from notifications.models import Notification


def fake_firebase(send=None):
    """Stand in for the `firebase_admin` package.

    Copied in shape from `joydigi_api/tests/test_push_device_token.py` so
    both files fake the SDK the same way: the module is supplied rather
    than patched, because the real one is an optional dependency.
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


class PushBase(TestCase):
    def setUp(self):
        # `_app`/`_app_attempted` are module-level caches, deliberately, so
        # a misconfigured deployment logs once rather than every minute.
        # Each test needs a clean slate.
        push_module._app = None
        push_module._app_attempted = False
        self.company = make_company("Notify Co")
        self.user = make_user("notifyuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="notify@test.joydigi", user=self.user
        )

    def tearDown(self):
        push_module._app = None
        push_module._app_attempted = False

    def token(self, value, active=True):
        return PushDeviceToken.objects.create(
            user=self.user, token=value, platform="android", is_active=active
        )

    def fresh_user(self):
        return type(self.user).objects.get(pk=self.user.pk)


# ======================================================================
# A. Every outcome has a name
# ======================================================================


class PushStatusTests(PushBase):
    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_no_registered_device_is_reported_as_such(self):
        result = push_module.send_to_user(self.user, "t", "b")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["status"], push_module.STATUS_NO_ACTIVE_TOKEN)

    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_a_missing_credential_is_reported_distinctly_from_no_device(self):
        # The whole point: these two used to be indistinguishable, and they
        # call for completely different responses.
        self.token("tok")
        result = push_module.send_to_user(self.user, "t", "b")
        self.assertTrue(result["skipped"])
        self.assertEqual(result["status"], push_module.STATUS_NOT_CONFIGURED)
        self.assertNotEqual(result["status"], push_module.STATUS_NO_ACTIVE_TOKEN)

    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_an_inactive_device_counts_as_no_device(self):
        self.token("retired", active=False)
        result = push_module.send_to_user(self.user, "t", "b")
        self.assertEqual(result["status"], push_module.STATUS_NO_ACTIVE_TOKEN)

    def test_a_delivered_push_is_reported_as_sent(self):
        self.token("phone")
        _messaging, patched = fake_firebase()
        with patched, mock.patch.object(
            push_module, "_load_app", return_value=mock.Mock()
        ):
            result = push_module.send_to_user(self.user, "t", "b")
        self.assertEqual(result["status"], push_module.STATUS_SENT)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["deactivated"], 0)

    def test_a_token_firebase_calls_dead_is_retired(self):
        # The REAL exception the installed SDK raises, constructed from
        # firebase_admin itself. Deliberately not a hand-written string:
        # `messaging.UnregisteredError`'s message is the bare sentence
        # "Requested entity was not found." and contains none of the
        # legacy markers, so a test that fabricates its own marker proves
        # only that the fabrication matches — which is how this went
        # unnoticed.
        from firebase_admin import messaging as real_messaging

        device = self.token("gone")
        _messaging, patched = fake_firebase(
            send=real_messaging.UnregisteredError(
                "Requested entity was not found."
            )
        )
        with patched, mock.patch.object(
            push_module, "_load_app", return_value=mock.Mock()
        ):
            result = push_module.send_to_user(self.user, "t", "b")
        self.assertEqual(result["deactivated"], 1)
        self.assertEqual(result["failed"], 1)
        device.refresh_from_db()
        self.assertFalse(device.is_active)

    def test_a_token_issued_for_another_firebase_project_is_retired(self):
        from firebase_admin import messaging as real_messaging

        device = self.token("foreign")
        _messaging, patched = fake_firebase(
            send=real_messaging.SenderIdMismatchError("mismatch")
        )
        with patched, mock.patch.object(
            push_module, "_load_app", return_value=mock.Mock()
        ):
            push_module.send_to_user(self.user, "t", "b")
        device.refresh_from_db()
        self.assertFalse(device.is_active)

    def test_a_temporary_failure_keeps_the_device_registered(self):
        # A Firebase outage or a timeout must not unregister everybody's
        # phone. These are the real exception classes the SDK raises, not
        # invented strings, and none of them may retire a device.
        from firebase_admin import exceptions as fb_exceptions
        from firebase_admin import messaging as real_messaging

        device = self.token("phone")
        transients = [
            fb_exceptions.UnavailableError("backend unavailable"),
            fb_exceptions.InternalError("internal"),
            fb_exceptions.DeadlineExceededError("deadline exceeded"),
            real_messaging.QuotaExceededError("quota"),
            # An APNs credential problem of ours, not the device's.
            real_messaging.ThirdPartyAuthError("apns auth"),
            # Our own malformed payload must never cost somebody's device.
            fb_exceptions.InvalidArgumentError("bad payload"),
            Exception("connection reset by peer"),
        ]
        for error in transients:
            label = type(error).__name__
            device.is_active = True
            device.save(update_fields=["is_active"])
            _messaging, patched = fake_firebase(send=error)
            with patched, mock.patch.object(
                push_module, "_load_app", return_value=mock.Mock()
            ):
                result = push_module.send_to_user(self.user, "t", "b")
            self.assertEqual(result["deactivated"], 0, label)
            self.assertEqual(result["status"], push_module.STATUS_FAILED, label)
            device.refresh_from_db()
            self.assertTrue(device.is_active, label)

    def test_the_legacy_string_markers_still_work(self):
        # Kept as a fallback for an older SDK or a caller that raises a
        # plain exception; the pre-existing suite relies on them.
        device = self.token("legacy")
        _messaging, patched = fake_firebase(send=Exception("NotRegistered"))
        with patched, mock.patch.object(
            push_module, "_load_app", return_value=mock.Mock()
        ):
            push_module.send_to_user(self.user, "t", "b")
        device.refresh_from_db()
        self.assertFalse(device.is_active)

    def test_one_dead_device_does_not_stop_the_healthy_one(self):
        from firebase_admin import messaging as real_messaging

        self.token("good")
        dead = self.token("bad")

        def selective(message, app=None):
            if getattr(message, "token", None) == "bad":
                raise real_messaging.UnregisteredError("not found")
            return "ok"

        _messaging, patched = fake_firebase(send=selective)
        with patched, mock.patch.object(
            push_module, "_load_app", return_value=mock.Mock()
        ):
            result = push_module.send_to_user(self.user, "t", "b")
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["deactivated"], 1)
        dead.refresh_from_db()
        self.assertFalse(dead.is_active)


# ======================================================================
# B. The in-app notification never depends on the push succeeding
# ======================================================================


class InAppSurvivesPushFailureTests(PushBase):
    """The mandated invariant, from three directions."""

    def send(self):
        start_at = timezone.localtime() - timedelta(minutes=5)
        return reminders._send_reminder(
            self.employee, STAGE_START_MINUS_5, timezone.localdate(), start_at
        )

    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_no_firebase_credential_still_creates_the_in_app_row(self):
        self.assertTrue(self.send())
        self.assertEqual(Notification.objects.count(), 1)

    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_no_registered_device_still_creates_the_in_app_row(self):
        self.token("tok", active=False)
        self.assertTrue(self.send())
        self.assertEqual(Notification.objects.count(), 1)

    def test_a_push_that_raises_does_not_take_the_row_with_it(self):
        self.token("phone")
        with mock.patch.object(
            push_module, "send_to_user", side_effect=RuntimeError("firebase down")
        ):
            self.assertTrue(self.send())
        self.assertEqual(Notification.objects.count(), 1)

    @override_settings(FIREBASE_CREDENTIALS_FILE="")
    def test_the_stored_row_is_what_stops_a_second_send(self):
        # The row is the dedupe marker as well as the notification, which
        # is why losing it to a push failure would mean re-sending.
        self.assertTrue(self.send())
        self.assertFalse(self.send())
        self.assertEqual(Notification.objects.count(), 1)


class PushTallyTests(PushBase):
    """Statuses are counted per run, not logged per employee."""

    def test_statuses_accumulate(self):
        tally = {}
        reminders.record_push_status(tally, {"status": push_module.STATUS_SENT})
        reminders.record_push_status(tally, {"status": push_module.STATUS_SENT})
        reminders.record_push_status(
            tally, {"status": push_module.STATUS_NO_ACTIVE_TOKEN}
        )
        self.assertEqual(
            tally,
            {push_module.STATUS_SENT: 2, push_module.STATUS_NO_ACTIVE_TOKEN: 1},
        )

    def test_a_missing_or_odd_result_is_ignored_rather_than_raising(self):
        tally = {}
        for junk in (None, {}, {"status": None}, "nonsense", 7):
            reminders.record_push_status(tally, junk)
        self.assertEqual(tally, {})
        # And a None tally must be accepted, so a caller that does not
        # care can pass nothing.
        reminders.record_push_status(None, {"status": push_module.STATUS_SENT})

    def test_an_idle_run_logs_nothing(self):
        with self.assertNoLogs("attendance.methods.reminders", level="INFO"):
            reminders.log_run_summary(
                "attendance_reminders",
                {"start_minus_5": 0, "start_plus_5": 0, "skipped": 4},
                {},
            )

    def test_a_run_that_delivered_logs_one_line(self):
        with self.assertLogs("attendance.methods.reminders", level="INFO") as caught:
            reminders.log_run_summary(
                "attendance_reminders",
                {"start_minus_5": 2, "start_plus_5": 0, "skipped": 1},
                {push_module.STATUS_SENT: 2},
            )
        self.assertEqual(len(caught.records), 1)
        line = caught.output[0]
        self.assertIn("REMINDER_CREATED=2", line)
        self.assertIn(push_module.STATUS_SENT, line)


# ======================================================================
# C. One scheduler owner, and none at all during tests
# ======================================================================


class SchedulerStartupTests(TestCase):
    def test_no_scheduler_is_running_inside_the_test_suite(self):
        # The guard used not to name `test`, so every test run started a
        # real BackgroundScheduler against the test database on a
        # one-minute tick. This assertion is the whole fix, observed from
        # inside the thing it fixes.
        self.assertIn("test", sys.argv)
        self.assertFalse(scheduler_module.should_start_embedded())
        self.assertIsNone(scheduler_module.scheduler)

    def test_every_excluded_command_is_recognised(self):
        for command in scheduler_module.NO_SCHEDULER_COMMANDS:
            with mock.patch.object(sys, "argv", ["manage.py", command]):
                self.assertTrue(
                    scheduler_module.running_excluded_command(), command
                )

    def test_runserver_is_not_excluded(self):
        # A developer running the server does want the jobs.
        with mock.patch.object(sys, "argv", ["manage.py", "runserver"]):
            self.assertFalse(scheduler_module.running_excluded_command())

    @override_settings(ATTENDANCE_SCHEDULER_MODE="dedicated")
    def test_dedicated_mode_stops_web_processes_owning_one(self):
        with mock.patch.object(sys, "argv", ["gunicorn", "joydigi.wsgi"]):
            self.assertEqual(
                scheduler_module.scheduler_mode(), scheduler_module.MODE_DEDICATED
            )
            self.assertFalse(scheduler_module.should_start_embedded())

    @override_settings(ATTENDANCE_SCHEDULER_MODE="embedded")
    def test_embedded_mode_is_the_unchanged_behaviour(self):
        with mock.patch.object(sys, "argv", ["gunicorn", "joydigi.wsgi"]):
            self.assertTrue(scheduler_module.should_start_embedded())

    @override_settings(ATTENDANCE_SCHEDULER_MODE="disabled")
    def test_disabled_mode_starts_nothing(self):
        with mock.patch.object(sys, "argv", ["gunicorn", "joydigi.wsgi"]):
            self.assertFalse(scheduler_module.should_start_embedded())

    @override_settings(ATTENDANCE_SCHEDULER_MODE="  DEDICATED  ")
    def test_the_mode_is_normalised(self):
        self.assertEqual(
            scheduler_module.scheduler_mode(), scheduler_module.MODE_DEDICATED
        )

    @override_settings(ATTENDANCE_SCHEDULER_MODE="nonsense")
    def test_an_unknown_mode_falls_back_to_embedded_loudly(self):
        # Never silently to `disabled`: a typo must not be able to stop
        # auto punch-out and forgotten-session finalization.
        with self.assertLogs("base.backends", level="WARNING"):
            self.assertEqual(
                scheduler_module.scheduler_mode(), scheduler_module.MODE_EMBEDDED
            )

    def test_the_two_modes_cannot_drift_apart(self):
        # Embedded and dedicated share one job description, so this is the
        # list both of them get.
        scheduler = scheduler_module.register_jobs(
            scheduler_module.build_scheduler()
        )
        try:
            ids = {job.id for job in scheduler.get_jobs() if job.id}
            self.assertIn("auto_punch_out", ids)
            self.assertIn("attendance_reminders", ids)
            self.assertIn("end_of_day_checkout", ids)
            self.assertIn("forgotten_session_finalization", ids)
            self.assertIn("create_daily_work_record", ids)
            self.assertEqual(len(scheduler.get_jobs()), 6)
        finally:
            # Never started, so nothing to stop; dispose of the jobstore.
            scheduler.remove_all_jobs()


class DedicatedCommandTests(TestCase):
    """The command refuses rather than doubling the jobs.

    Only the refusal paths are exercised: the success path calls
    `BlockingScheduler.start()`, which by design never returns.
    """

    @override_settings(ATTENDANCE_SCHEDULER_MODE="embedded")
    def test_it_refuses_when_the_web_processes_already_own_one(self):
        with self.assertRaises(CommandError) as caught:
            call_command("run_attendance_scheduler")
        self.assertIn("dedicated", str(caught.exception))

    @override_settings(ATTENDANCE_SCHEDULER_MODE="disabled")
    def test_it_refuses_when_scheduling_is_switched_off(self):
        with self.assertRaises(CommandError):
            call_command("run_attendance_scheduler")
