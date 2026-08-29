"""Phase ATT-TIME-2I — Vietnam is a fixed business rule, not a knob.

The ATT-TIME-2H diagnostic page proved production was running
`settings.TIME_ZONE = Asia/Kolkata` with a +05:30 offset and
`os.environ["TZ"] = Asia/Kolkata`, which is exactly the -90 minute
attendance error that was reported. The source default had already been
corrected to Asia/Ho_Chi_Minh, but it was never reached: a stale
`TIME_ZONE` in the deployment environment kept winning.

So the setting stopped being read from the environment at all. These
tests are the guard on that decision — the first one deliberately sets
the environment variable that used to break production and asserts it
now has no effect.
"""

import importlib
import os
from datetime import date, datetime, time, timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase
from django.utils import timezone as django_timezone
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceGeneralSetting
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

from joydigi_api.tests.test_attendance_timezone import at_vietnam

CLOCK_IN_URL = "/api/attendance/clock-in/"
CLOCK_OUT_URL = "/api/attendance/clock-out/"

VIETNAM = "Asia/Ho_Chi_Minh"


class BusinessTimezoneIsFixedTests(TestCase):
    def test_time_zone_is_vietnam(self):
        self.assertEqual(settings.TIME_ZONE, VIETNAM)

    def test_use_tz_remains_enabled(self):
        self.assertTrue(settings.USE_TZ)

    def test_a_stale_environment_value_cannot_override_it(self):
        """The critical regression.

        `TIME_ZONE=Asia/Kolkata` is put back into the environment — the
        exact production condition — and the settings module is
        re-executed. It must still come out as Vietnam.
        """
        import joydigi.settings.base as base_settings

        with mock.patch.dict(os.environ, {"TIME_ZONE": "Asia/Kolkata"}):
            # Sanity: the environment really does carry the bad value.
            self.assertEqual(os.environ["TIME_ZONE"], "Asia/Kolkata")
            importlib.reload(base_settings)
            reloaded = base_settings.TIME_ZONE

        # Restore the module to its normal state regardless of outcome.
        importlib.reload(base_settings)

        self.assertEqual(
            reloaded,
            VIETNAM,
            msg="a stale deployment TIME_ZONE must no longer reach settings",
        )

    def test_the_setting_is_a_literal_not_an_environment_lookup(self):
        """Belt and braces on the same guarantee, at source level: a
        literal simply cannot be overridden by a deployment value."""
        with open("joydigi/settings/base.py", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('TIME_ZONE = "Asia/Ho_Chi_Minh"', source)
        self.assertNotIn('TIME_ZONE = env("TIME_ZONE"', source)

    def test_no_manual_offset_compensation_was_introduced(self):
        with open("joydigi/settings/base.py", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in (
            "timedelta(hours=7)",
            "timedelta(hours=1, minutes=30)",
            "timedelta(minutes=90)",
            "hours=5, minutes=30",
        ):
            self.assertNotIn(forbidden, source)

    def test_localtime_is_plus_seven(self):
        """What the diagnostic page will show after deployment."""
        offset = django_timezone.localtime().utcoffset()
        self.assertEqual(offset, timedelta(hours=7))


class ClockTimesFollowTheFixedTimezoneTests(TestCase):
    """§6 — server 13:15 Vietnam must be stored as 13:15."""

    def setUp(self):
        self.company = make_company("Fixed TZ Co")
        self.user = make_user("fixedtzuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="fixedtz@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Fixed TZ Shift")
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

    def _attendance(self):
        return Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.day
        )

    def test_server_1315_vietnam_is_stored_as_1315(self):
        with at_vietnam(self._vn(13, 15)), mock.patch(
            "attendance.views.clock_in_out.validate_checkin_source",
            return_value={"allowed": True, "method": "att-time-2i test"},
        ):
            response = self.client.post(CLOCK_IN_URL, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["clock_in"], "13:15:00")
        self.assertEqual(self._attendance().attendance_clock_in, time(13, 15))
        self.assertEqual(self._attendance().attendance_date, self.day)

    def test_clock_out_follows_the_same_fixed_timezone(self):
        with at_vietnam(self._vn(8, 0)), mock.patch(
            "attendance.views.clock_in_out.validate_checkin_source",
            return_value={"allowed": True, "method": "att-time-2i test"},
        ):
            self.client.post(CLOCK_IN_URL, {}, format="json")

        with at_vietnam(self._vn(13, 15)):
            response = self.client.post(CLOCK_OUT_URL, {}, format="json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["clock_out"], "13:15:00")
        self.assertEqual(self._attendance().attendance_clock_out, time(13, 15))

    def test_the_reported_production_symptom_no_longer_reproduces(self):
        """13:15 Vietnam read through Asia/Kolkata would have been 11:45,
        the shape of the original bug report."""
        with at_vietnam(self._vn(13, 15)), mock.patch(
            "attendance.views.clock_in_out.validate_checkin_source",
            return_value={"allowed": True, "method": "att-time-2i test"},
        ):
            self.client.post(CLOCK_IN_URL, {}, format="json")

        stored = self._attendance().attendance_clock_in
        self.assertEqual(stored, time(13, 15))
        self.assertNotEqual(stored, time(11, 45), msg="Kolkata reading leaked")
