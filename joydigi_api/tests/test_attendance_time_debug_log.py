"""Phase ATT-TIME-2G — the temporary ATT_TIME_DEBUG diagnostics.

Production still reports attendance ~1h30m early even though the code
reads `django_timezone.localtime()` and the shipped default is
Asia/Ho_Chi_Minh. These log lines exist to make the whole chain visible
in the ordinary application log:

    running settings -> computed instant -> values passed into
    `perform_clock_*` -> the row actually stored

These tests pin two things: that the numbers in the log genuinely track
the server clock end to end (so a production log line can be trusted as
evidence), and that nothing sensitive is ever written to it.
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
LOGGER = "joydigi_api.api_views.attendance.views"

VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")

SECRET_LOOKING = "super-secret-proof-token-value"


def field(lines, key):
    """Pulls `key=value` out of the captured ATT_TIME_DEBUG lines."""
    for line in lines:
        for part in line.split():
            if part.startswith(f"{key}="):
                return part.split("=", 1)[1]
    return None


class AttendanceDebugLogTests(TestCase):
    def setUp(self):
        self.company = make_company("Debug Log Co")
        self.user = make_user("debuguser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="debug@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Debug Shift")
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

    def _clock_in(self, wall_clock, payload=None):
        with at_vietnam(wall_clock), mock.patch(
            "attendance.views.clock_in_out.validate_checkin_source",
            return_value={"allowed": True, "method": "att-time-2g test"},
        ):
            with self.assertLogs(LOGGER, level="INFO") as captured:
                response = self.client.post(
                    CLOCK_IN_URL, payload or {}, format="json"
                )
        return response, [line for line in captured.output if "ATT_TIME_DEBUG" in line]

    def _clock_out(self, wall_clock, payload=None):
        with at_vietnam(wall_clock):
            with self.assertLogs(LOGGER, level="INFO") as captured:
                response = self.client.post(
                    CLOCK_OUT_URL, payload or {}, format="json"
                )
        return response, [line for line in captured.output if "ATT_TIME_DEBUG" in line]

    # ------------------------------------------------------------------
    # The chain: server clock -> logged input -> logged stored row
    # ------------------------------------------------------------------

    def test_clock_in_log_matches_the_server_clock_and_the_stored_row(self):
        """Server at 11:30 VN must appear as 11:30 at every stage."""
        response, lines = self._clock_in(self._vn(11, 30))
        self.assertEqual(response.status_code, 200)

        self.assertEqual(field(lines, "action"), "clock_in")
        self.assertEqual(field(lines, "employee_id"), str(self.employee.id))
        self.assertEqual(field(lines, "settings_timezone"), "Asia/Ho_Chi_Minh")
        self.assertTrue(field(lines, "utc_now"))

        # Computed input.
        self.assertTrue(field(lines, "local_datetime").startswith(f"{self.day}T11:30"))
        self.assertEqual(field(lines, "attendance_date"), str(self.day))
        self.assertEqual(field(lines, "attendance_time"), "11:30:00")

        # What the database actually holds.
        self.assertEqual(field(lines, "stored_attendance_date"), str(self.day))
        self.assertEqual(field(lines, "stored_clock_in"), "11:30:00")
        self.assertTrue(field(lines, "attendance_id"))

        # ...and the log agrees with the row itself.
        stored = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.day
        )
        self.assertEqual(stored.attendance_clock_in, time(11, 30))

    def test_clock_out_log_matches_the_server_clock_and_the_stored_row(self):
        self._clock_in(self._vn(8, 0))

        response, lines = self._clock_out(self._vn(17, 45))
        self.assertEqual(response.status_code, 200)

        self.assertEqual(field(lines, "action"), "clock_out")
        self.assertEqual(field(lines, "employee_id"), str(self.employee.id))
        self.assertEqual(field(lines, "settings_timezone"), "Asia/Ho_Chi_Minh")
        self.assertTrue(field(lines, "local_datetime").startswith(f"{self.day}T17:45"))
        self.assertEqual(field(lines, "attendance_time"), "17:45:00")
        self.assertEqual(field(lines, "stored_clock_out"), "17:45:00")

        stored = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.day
        )
        self.assertEqual(stored.attendance_clock_out, time(17, 45))

    def test_both_stages_are_logged_for_one_action(self):
        """A computed line and a stored line — the pair is what makes the
        production log diagnostic rather than merely suggestive."""
        _response, lines = self._clock_in(self._vn(11, 30))
        self.assertTrue(any("local_datetime=" in line for line in lines))
        self.assertTrue(any("stage=stored" in line for line in lines))

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------

    def test_no_secrets_are_ever_logged(self):
        """§4 — a verification proof is supplied and must not appear, nor
        may any credential-shaped field."""
        _response, lines = self._clock_in(
            self._vn(11, 30), payload={"verification_proof": SECRET_LOOKING}
        )
        blob = "\n".join(lines)

        self.assertNotIn(SECRET_LOOKING, blob)
        for forbidden in (
            "verification_proof",
            "password",
            "token",
            "Authorization",
            "Bearer",
            "cookie",
            "Cookie",
            "HTTP_",
        ):
            self.assertNotIn(forbidden, blob, msg=f"{forbidden} leaked into the log")

    def test_the_log_lines_carry_only_the_expected_keys(self):
        _response, lines = self._clock_in(self._vn(11, 30))
        allowed = {
            "action",
            "employee_id",
            "settings_timezone",
            "utc_now",
            "local_datetime",
            "attendance_date",
            "attendance_time",
            "stage",
            "attendance_id",
            "stored_attendance_date",
            "stored_clock_in",
            "stored_clock_out",
        }
        for line in lines:
            payload = line.split("ATT_TIME_DEBUG", 1)[1]
            for part in payload.split():
                if "=" in part:
                    self.assertIn(part.split("=", 1)[0], allowed)

    def test_diagnostics_never_break_a_clock_in(self):
        """The logging is observational; a failure inside it must not
        cost an employee their attendance record."""
        with mock.patch(
            "joydigi_api.api_views.attendance.views.Attendance.refresh_from_db",
            side_effect=RuntimeError("boom"),
        ):
            with at_vietnam(self._vn(11, 30)), mock.patch(
                "attendance.views.clock_in_out.validate_checkin_source",
                return_value={"allowed": True, "method": "att-time-2g test"},
            ):
                response = self.client.post(CLOCK_IN_URL, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.day
            ).exists()
        )
