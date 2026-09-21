"""
Phase 3 — attendance production hardening and error UX.

Three promises, each tested against the real API:

* every expected refusal answers with a stable `code` and a Vietnamese
  `message`, and writes nothing;
* a 2xx means the attendance really was written — the id in the response is
  a row that exists, and its activity was opened or closed;
* anything unexpected is rolled back, answered with a fixed sentence that
  reveals nothing about the internals, and logged with enough to diagnose
  it — but never the proof, the coordinates, or the request body.

Rows are created in the test database only.
"""

import logging
from datetime import datetime, time, timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.methods.utils import Request
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceGeneralSetting,
    AttendanceLateComeEarlyOut,
)
from attendance.views.clock_in_out import (
    clock_in_attendance_and_activity,
    perform_clock_in,
)
from base.models import (
    AttendanceAllowedIP,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
)
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

CLOCK_IN = "/api/attendance/clock-in/"
CLOCK_OUT = "/api/attendance/clock-out/"
MY_ATTENDANCE = "/api/attendance/my-attendance/"
POLICY = "/api/attendance/policy/"
VERIFY_SOURCE = "/api/attendance/verify-source/"
VERIFY_FACE = "/api/attendance/verify-face/"

API_LOGGER = "joydigi_api.api_views.attendance.views"


def is_vietnamese(text):
    """True when the message is written in Vietnamese, not English."""
    return any(ch in text for ch in "ăâđêôơưàáạảãèéẹẻẽìíịỉĩòóọỏõùúụủũỳýỵỷỹ")


class HardeningBase(TestCase):
    def setUp(self):
        self.company = make_company("Hardening Co")
        self.user = make_user("hardeninguser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="hardening@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )
        self.today = timezone.localdate()
        day = EmployeeShiftDay.objects.get(day=self.today.strftime("%A").lower())
        EmployeeShiftSchedule.objects.get_or_create(
            shift_id=self.shift,
            day=day,
            defaults={
                "is_night_shift": False,
                "minimum_working_hour": "08:00",
                "start_time": "08:00:00",
                "end_time": "17:00:00",
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.fresh_user())

    def fresh_user(self):
        # A freshly loaded user, as production has on every request. The
        # object `make_employee` handed back still caches the Employee from
        # before its work information was attached, so its company reads
        # as None — which silently skips every company-scoped check.
        return type(self.user).objects.get(pk=self.user.pk)

    def open_session(self, minutes_ago=120):
        day = EmployeeShiftDay.objects.get(day=self.today.strftime("%A").lower())
        started = timezone.localtime() - timedelta(minutes=minutes_ago)
        clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=self.today,
            attendance_date=self.today,
            day=day,
            now=started.strftime("%H:%M"),
            shift=self.shift,
            minimum_hour="08:00",
            start_time=0,
            end_time=1,
            in_datetime=started,
        )
        return Attendance.objects.get(employee_id=self.employee, attendance_date=self.today)

    def world(self):
        """Every row the attendance flow can write, for zero-mutation checks."""
        return (
            list(
                Attendance.objects.order_by("pk").values_list(
                    "pk",
                    "attendance_clock_in",
                    "attendance_clock_out",
                    "attendance_clock_out_date",
                    "attendance_worked_hour",
                    "attendance_validated",
                    "is_validate_request",
                )
            ),
            list(
                AttendanceActivity.objects.order_by("pk").values_list(
                    "pk", "clock_out", "out_datetime"
                )
            ),
            AttendanceLateComeEarlyOut.objects.count(),
        )

    def assert_controlled(self, response, code, status=400):
        self.assertEqual(response.status_code, status, getattr(response, "data", None))
        self.assertEqual(response.data["code"], code)
        self.assertTrue(
            is_vietnamese(response.data["message"]),
            f"{code} must answer in Vietnamese, got {response.data['message']!r}",
        )


class ExpectedRejectionTests(HardeningBase):
    """1–7: every expected refusal is controlled, Vietnamese, and inert."""

    def test_1_already_clocked_in(self):
        self.open_session()
        before = self.world()
        self.assert_controlled(self.client.post(CLOCK_IN), "ALREADY_CLOCKED_IN")
        self.assertEqual(self.world(), before)

    def test_2_already_clocked_out(self):
        before = self.world()
        self.assert_controlled(self.client.post(CLOCK_OUT), "ALREADY_CLOCKED_OUT")
        self.assertEqual(self.world(), before)

    def test_3_checkout_too_soon_keeps_its_code(self):
        row = self.open_session(minutes_ago=5)
        before = self.world()
        self.assert_controlled(self.client.post(CLOCK_OUT), "CHECKOUT_TOO_SOON")
        self.assertEqual(self.world(), before)
        self.assertTrue(
            AttendanceActivity.objects.filter(
                employee_id=self.employee, clock_out__isnull=True
            ).exists()
        )
        row.refresh_from_db()
        self.assertIsNone(row.attendance_clock_out)

    def test_4_missing_employee_is_never_a_500_anywhere_in_the_flow(self):
        orphan = make_user("orphanuser", password="secret123")
        client = APIClient()
        client.force_authenticate(user=orphan)
        calls = [
            ("get", MY_ATTENDANCE, {}),
            ("get", POLICY, {}),
            ("post", VERIFY_SOURCE, {"method": "location"}),
            ("post", VERIFY_FACE, {}),
            ("post", CLOCK_IN, {}),
            ("post", CLOCK_OUT, {}),
        ]
        for method, url, body in calls:
            response = getattr(client, method)(url, body)
            self.assertNotEqual(response.status_code, 500, url)
            self.assert_controlled(response, "EMPLOYEE_PROFILE_MISSING")

    def test_5_an_unknown_verification_method_is_refused_in_vietnamese(self):
        response = self.client.post(VERIFY_SOURCE, {"method": "telepathy"})
        self.assert_controlled(response, "METHOD_NOT_ENABLED")

    def test_5b_feature_disabled_message_is_vietnamese(self):
        # Reachable from the web flow only: the mobile API always passes a
        # timestamp, which skips this switch. The reason dict is shared.
        AttendanceGeneralSetting.objects.update_or_create(
            company_id=self.company, defaults={"enable_check_in": False}
        )
        _attendance, allowed, reason = perform_clock_in(
            Request(
                user=self.fresh_user(), date=self.today, time=time(8, 0), datetime=None
            )
        )
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "METHOD_NOT_ENABLED")
        self.assertTrue(is_vietnamese(reason["message"]), reason["message"])

    def test_5c_incomplete_profile_is_refused_in_vietnamese(self):
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).delete()
        before = self.world()
        self.assert_controlled(self.client.post(CLOCK_IN), "PROFILE_INCOMPLETE")
        self.assertEqual(self.world(), before)

    def test_6_a_disallowed_network_is_refused_in_vietnamese(self):
        # A network that genuinely is not on the list; the refusal itself is
        # correct here, and what is checked is how it is expressed. (The
        # message used to read "Hạn chế đăng ký" / "Hạn chế thanh toán" —
        # "registration" / "payment" restricted — from a bad translation.)
        AttendanceAllowedIP.objects.create(
            company_id=self.company,
            is_enabled=True,
            additional_data={"allowed_ips": ["10.99.99.0/24"]},
        )
        before = self.world()
        response = self.client.post(CLOCK_IN)
        self.assert_controlled(response, "WIFI_NOT_ALLOWED")
        self.assertNotIn("đăng ký", response.data["message"])
        self.assertNotIn("thanh toán", response.data["message"])
        self.assertEqual(self.world(), before)

    def test_7_an_invalid_verification_proof_is_refused(self):
        before = self.world()
        response = self.client.post(CLOCK_IN, {"verification_proof": "not-a-proof"})
        self.assert_controlled(response, "VERIFICATION_REQUIRED")
        self.assertEqual(self.world(), before)


class SuccessMeansPersistedTests(HardeningBase):
    """8–9: a 2xx points at rows that really exist, in the state it claims."""

    def test_8_a_successful_check_in_is_persisted(self):
        response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 200, response.data)
        attendance = Attendance.objects.get(pk=response.data["attendance_id"])
        self.assertEqual(attendance.employee_id_id, self.employee.pk)
        self.assertEqual(attendance.attendance_date, self.today)
        self.assertIsNone(attendance.attendance_clock_out)
        activity = AttendanceActivity.objects.get(
            employee_id=self.employee, clock_out__isnull=True
        )
        self.assertEqual(activity.attendance_date, self.today)
        self.assertIsNotNone(activity.in_datetime)

    def test_9_a_successful_check_out_really_closed_the_session(self):
        row = self.open_session(minutes_ago=120)

        response = self.client.post(CLOCK_OUT)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.data["attendance_id"])
        self.assertEqual(response.data["attendance_id"], row.pk)
        row.refresh_from_db()
        self.assertIsNotNone(row.attendance_clock_out)
        self.assertEqual(str(row.attendance_clock_out), response.data["clock_out"])
        self.assertNotEqual(row.attendance_worked_hour, None)
        self.assertFalse(
            AttendanceActivity.objects.filter(
                employee_id=self.employee, clock_out__isnull=True
            ).exists()
        )
        activity = AttendanceActivity.objects.get(employee_id=self.employee)
        self.assertIsNotNone(activity.out_datetime)


class UnexpectedFailureTests(HardeningBase):
    """11 + transaction audit: a crash is rolled back and reveals nothing."""

    SECRET = "SELECT password FROM auth_user /srv/joydigi/.env"

    def test_11_an_unexpected_error_reveals_nothing(self):
        with mock.patch(
            "joydigi_api.api_views.attendance.views.perform_clock_in",
            side_effect=RuntimeError(self.SECRET),
        ), self.assertLogs(API_LOGGER, level="ERROR"):
            response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data["code"], "ATTENDANCE_UNEXPECTED_ERROR")
        body = str(response.content)
        for leak in ("SELECT", "password", ".env", "Traceback", "RuntimeError"):
            self.assertNotIn(leak, body)

    def test_a_crash_midway_through_check_in_leaves_nothing_behind(self):
        # `late_come` runs after the Attendance row and its activity have
        # already been written; failing there proves the whole write is one
        # transaction.
        before = self.world()
        with mock.patch(
            "attendance.views.clock_in_out.late_come",
            side_effect=RuntimeError("boom"),
        ), self.assertLogs(API_LOGGER, level="ERROR"):
            response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.world(), before)

    def test_a_crash_midway_through_check_out_leaves_the_session_open(self):
        row = self.open_session(minutes_ago=120)
        before = self.world()
        with mock.patch(
            "attendance.views.clock_in_out.early_out",
            side_effect=RuntimeError("boom"),
        ), self.assertLogs(API_LOGGER, level="ERROR"):
            response = self.client.post(CLOCK_OUT)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.world(), before)
        row.refresh_from_db()
        self.assertIsNone(row.attendance_clock_out)


class SafeLoggingTests(HardeningBase):
    """12: logs say who, what and why — never the sensitive inputs."""

    PROOF = "PROOF-TOKEN-7f3a9c"
    LATITUDE = "21.028511"

    def test_a_refusal_is_logged_with_its_code_and_nothing_sensitive(self):
        with self.assertLogs(API_LOGGER, level="WARNING") as logs:
            self.client.post(
                CLOCK_IN,
                {"verification_proof": self.PROOF, "latitude": self.LATITUDE},
            )

        text = "\n".join(logs.output)
        self.assertIn("ATTENDANCE_CLOCK_IN", text)
        self.assertIn("code=VERIFICATION_REQUIRED", text)
        self.assertIn(f"employee={self.employee.pk}", text)
        self.assertNotIn(self.PROOF, text)
        self.assertNotIn(self.LATITUDE, text)

    def test_an_unexpected_error_log_carries_no_request_payload(self):
        with mock.patch(
            "joydigi_api.api_views.attendance.views.perform_clock_in",
            side_effect=RuntimeError("boom"),
        ), self.assertLogs(API_LOGGER, level="ERROR") as logs:
            self.client.post(
                CLOCK_IN,
                {"verification_proof": self.PROOF, "latitude": self.LATITUDE},
            )

        text = "\n".join(logs.output)
        self.assertIn("result=UNEXPECTED_ERROR", text)
        self.assertIn("error=RuntimeError", text)
        self.assertNotIn(self.PROOF, text)
        self.assertNotIn(self.LATITUDE, text)

    def test_a_successful_check_in_logs_no_rejection(self):
        logger = logging.getLogger(API_LOGGER)
        with mock.patch.object(logger, "warning") as warning:
            response = self.client.post(CLOCK_IN)
        self.assertEqual(response.status_code, 200)
        warning.assert_not_called()
