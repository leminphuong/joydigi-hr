"""Phase ATT-TIME-2C — the *server* clock is the sole authority for
attendance times, on every path including mobile.

ATT-TIME-2A briefly made the employee's phone the authority. That was
reverted: a device clock can be changed by the person whose attendance
it records, so it cannot decide `attendance_date`,
`attendance_clock_in` or `attendance_clock_out`.

`test_attendance_timezone.py` already proves the server reads the right
timezone (Asia/Ho_Chi_Minh), covers 08:00/08:15/12:30/17:45, the
midnight boundaries, the API round trip, duplicate clock-in and
`AttendanceActivity`. This file adds the part that is specific to
*authority*: no client-supplied time can influence what gets stored —
which also stops device-time authority from being reintroduced by
accident, since these tests would start failing the moment it was.
"""

from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceGeneralSetting
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

from joydigi_api.tests.test_attendance_timezone import at_vietnam

CLOCK_IN_URL = "/api/attendance/clock-in/"
CLOCK_OUT_URL = "/api/attendance/clock-out/"
MY_ATTENDANCE_URL = "/api/attendance/my-attendance/"

VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")

#: Every shape a tampered client might use to try to dictate the time.
#: None of them may have any effect.
TAMPERED_CLIENT_FIELDS = {
    "device_timestamp": "2026-01-01T06:00:00+07:00",
    "timestamp": "2026-01-01T06:00:00+07:00",
    "attendance_clock_in": "06:00:00",
    "attendance_clock_out": "23:00:00",
    "attendance_date": "2026-01-01",
    "date": "2026-01-01",
    "time": "06:00",
}


class ServerAuthorityBase(TestCase):
    def setUp(self):
        self.company = make_company("Server Authority Co")
        self.user = make_user("authorityuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="authority@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Authority Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )
        for shift_day in EmployeeShiftDay.objects.all():
            EmployeeShiftSchedule.objects.get_or_create(
                shift_id=self.shift,
                day=shift_day,
                defaults={
                    "is_night_shift": False,
                    "minimum_working_hour": "08:00",
                    "start_time": "08:00:00",
                    "end_time": "17:00:00",
                },
            )
        AttendanceGeneralSetting.objects.create(
            company_id=self.company, enable_check_in=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.day = date.today() - timedelta(days=1)

    def _vn(self, hour, minute, second=0):
        return datetime(
            self.day.year, self.day.month, self.day.day, hour, minute, second
        )

    def clock_in(self, server_wall_clock, payload=None):
        with at_vietnam(server_wall_clock), mock.patch(
            "attendance.views.clock_in_out.validate_checkin_source",
            return_value={"allowed": True, "method": "att-time-2c test"},
        ):
            return self.client.post(CLOCK_IN_URL, payload or {}, format="json")

    def clock_out(self, server_wall_clock, payload=None):
        with at_vietnam(server_wall_clock):
            return self.client.post(CLOCK_OUT_URL, payload or {}, format="json")

    def attendance_on(self, attendance_date):
        return Attendance.objects.get(
            employee_id=self.employee, attendance_date=attendance_date
        )


class ClientClockCannotInfluenceAttendanceTests(ServerAuthorityBase):
    def test_a_tampered_client_clock_cannot_change_clock_in(self):
        """§13 / §20.6 — the server was at 08:15; the client claimed
        06:00 in every shape it could. 08:15 wins."""
        response = self.clock_in(self._vn(8, 15), payload=TAMPERED_CLIENT_FIELDS)

        self.assertEqual(response.status_code, 200)
        stored = self.attendance_on(self.day).attendance_clock_in
        self.assertEqual(stored, time(8, 15))
        self.assertNotEqual(stored, time(6, 0), msg="client clock was honoured")
        self.assertEqual(response.data["clock_in"], "08:15:00")

    def test_a_client_clock_running_fast_cannot_change_clock_in(self):
        """The mirror case: a client claiming a *later* time (11:30)
        must not win either."""
        response = self.clock_in(
            self._vn(8, 15),
            payload={"device_timestamp": "2026-01-01T11:30:00+07:00"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.attendance_on(self.day).attendance_clock_in, time(8, 15)
        )

    def test_a_tampered_client_clock_cannot_change_clock_out(self):
        """§13 / §20.7"""
        self.clock_in(self._vn(8, 0))
        response = self.clock_out(
            self._vn(17, 45), payload=TAMPERED_CLIENT_FIELDS
        )

        self.assertEqual(response.status_code, 200)
        stored = self.attendance_on(self.day).attendance_clock_out
        self.assertEqual(stored, time(17, 45))
        self.assertNotEqual(stored, time(23, 0))
        self.assertEqual(response.data["clock_out"], "17:45:00")

    def test_a_tampered_client_date_cannot_move_the_attendance_day(self):
        """The date is derived from the same server instant, so a client
        claiming a date in January cannot file the record there."""
        self.clock_in(self._vn(8, 15), payload=TAMPERED_CLIENT_FIELDS)

        self.assertEqual(self.attendance_on(self.day).attendance_date, self.day)
        self.assertFalse(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=date(2026, 1, 1)
            ).exists()
        )

    def test_the_api_reports_the_server_time_back(self):
        """§14 / §20.8 — what the employee's app then reads is the
        server's value, not the one it sent."""
        self.clock_in(self._vn(8, 17, 25), payload=TAMPERED_CLIENT_FIELDS)
        self.clock_out(self._vn(17, 45, 10), payload=TAMPERED_CLIENT_FIELDS)

        response = self.client.get(MY_ATTENDANCE_URL)
        row = next(
            r
            for r in response.data["results"]
            if r["attendance_date"] == self.day.isoformat()
        )
        # Stored to the minute by the write path's `strftime("%H:%M")`.
        self.assertEqual(row["attendance_clock_in"], "08:17:00")
        self.assertEqual(row["attendance_clock_out"], "17:45:00")


class VerificationStillRequiredTests(ServerAuthorityBase):
    def test_a_denied_verification_still_blocks_the_clock_in(self):
        """§20.12 — attendance-source verification is untouched by this
        phase and still fails closed."""
        with at_vietnam(self._vn(8, 15)), mock.patch(
            "attendance.views.clock_in_out.validate_checkin_source",
            return_value={
                "allowed": False,
                "code": "VERIFICATION_REQUIRED",
                "message": "Cần xác thực.",
            },
        ):
            response = self.client.post(CLOCK_IN_URL, {}, format="json")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "VERIFICATION_REQUIRED")
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.employee).exists()
        )

    def test_verification_is_still_invoked_on_every_clock_in(self):
        with at_vietnam(self._vn(8, 15)), mock.patch(
            "attendance.views.clock_in_out.validate_checkin_source",
            return_value={"allowed": True, "method": "spy"},
        ) as spy:
            self.client.post(CLOCK_IN_URL, {}, format="json")

        spy.assert_called_once()


class NoDeviceTimeAuthorityRemainsTests(TestCase):
    """§5/§6/§17/§21 — ATT-TIME-2A left no residue."""

    def test_no_device_time_module_exists(self):
        import os

        self.assertFalse(
            os.path.exists("joydigi_api/api_views/attendance/device_time.py"),
            msg="ATT-TIME-2A's device_time.py should have been removed",
        )

    def test_attendance_api_never_reads_a_client_timestamp(self):
        with open(
            "joydigi_api/api_views/attendance/views.py", encoding="utf-8"
        ) as handle:
            source = handle.read()
        for forbidden in (
            "device_timestamp",
            "evaluate_clock_skew",
            "_resolve_attendance_instant",
            "CLOCK_SKEW",
        ):
            self.assertNotIn(forbidden, source)

    def test_the_write_path_uses_one_django_aware_server_instant(self):
        with open(
            "joydigi_api/api_views/attendance/views.py", encoding="utf-8"
        ) as handle:
            source = handle.read()
        # Exactly one capture per action, for the two actions.
        self.assertEqual(source.count("django_timezone.localtime()"), 2)
        self.assertEqual(source.count("current_date = current_datetime.date()"), 2)
        self.assertEqual(source.count("current_time = current_datetime.time()"), 2)
