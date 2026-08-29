"""Phase ATT-TIME-2 — attendance wall-clock times must be Vietnam time.

Background (proved in ATT-TIME-1): `Attendance.attendance_clock_in` is a
naive `TimeField`, so whatever wall clock the server reads at write time
is what every reader — web template and Flutter alike — displays
verbatim. That clock used to come from `datetime.now()`, i.e. the
*process* clock, which on a POSIX host Django derives from `TIME_ZONE`
(`os.environ["TZ"]` + `time.tzset()`, django/conf/__init__.py). With
`TIME_ZONE` left at the inherited `Asia/Kolkata` default, every stamp
landed 1h30m early: a real 08:17 was stored and shown as 06:47.

This phase fixed the cause in two places, with no arithmetic anywhere:
  * `joydigi/settings/base.py` — default `TIME_ZONE` is now
    `Asia/Ho_Chi_Minh`;
  * the clock-in/clock-out write paths now derive the business date and
    time from a single `timezone.localtime()` instant instead of three
    independent, timezone-unaware clock reads.

### How these tests drive the clock
`timezone.localtime()` calls `django.utils.timezone.now()` and converts
its result into the active timezone. Patching that single function with
a fixed **UTC** instant therefore exercises the real conversion rather
than a passthrough: if the code ever leaked a UTC or Kolkata reading,
the asserted Vietnam wall clock would not match. The midnight cases
below depend on exactly that — Vietnam 00:00:01 is still the *previous*
day in both UTC and Kolkata.
"""

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceActivity, AttendanceGeneralSetting
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

CLOCK_IN_URL = "/api/attendance/clock-in/"
CLOCK_OUT_URL = "/api/attendance/clock-out/"
MY_ATTENDANCE_URL = "/api/attendance/my-attendance/"

VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")


@contextmanager
def at_vietnam(wall_clock):
    """Runs the block at the instant Vietnam's clocks read `wall_clock`.

    The patched value is deliberately expressed in **UTC** so the test
    never hands the production code a pre-converted answer.
    """
    instant_utc = wall_clock.replace(tzinfo=VIETNAM).astimezone(dt_timezone.utc)
    with mock.patch("django.utils.timezone.now", return_value=instant_utc):
        yield instant_utc


class TimezoneSettingsTests(TestCase):
    def test_time_zone_default_is_vietnam(self):
        """§13.1"""
        self.assertEqual(settings.TIME_ZONE, "Asia/Ho_Chi_Minh")

    def test_use_tz_remains_enabled(self):
        """§13.2 — the fix must not have switched timezone support off."""
        self.assertTrue(settings.USE_TZ)

    def test_no_manual_offset_compensation_exists_in_the_write_path(self):
        """§13.12 — guards against anyone ever 'fixing' this with
        arithmetic. A +1:30 or +7:00 fudge would make the displayed time
        look right while leaving the underlying instant wrong, and would
        silently double-correct once the timezone config is right."""
        for path in (
            "attendance/views/clock_in_out.py",
            "joydigi_api/api_views/attendance/views.py",
        ):
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in (
                "timedelta(hours=7)",
                "timedelta(hours=1, minutes=30)",
                "timedelta(minutes=90)",
                "hours=5, minutes=30",
            ):
                self.assertNotIn(
                    forbidden,
                    source,
                    msg=f"manual timezone compensation found in {path}",
                )


class AttendanceClockBase(TestCase):
    """Shared fixture: one employee on a plain 08:00-17:00 day shift,
    with a schedule for every weekday so any calendar date works."""

    def setUp(self):
        self.company = make_company("VN Time Co")
        self.user = make_user("vntimeuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="vntime@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="VN Shift")
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

    def clock_in_at(self, wall_clock):
        with at_vietnam(wall_clock), mock.patch(
            "attendance.views.clock_in_out.validate_checkin_source",
            return_value={"allowed": True, "method": "att-time-2 test"},
        ):
            return self.client.post(CLOCK_IN_URL)

    def clock_out_at(self, wall_clock):
        with at_vietnam(wall_clock):
            return self.client.post(CLOCK_OUT_URL)

    def attendance_on(self, attendance_date):
        return Attendance.objects.get(
            employee_id=self.employee, attendance_date=attendance_date
        )


class ControlledVietnamTimeTests(AttendanceClockBase):
    """§7 / §13.3-4 — controlled Vietnam times, stored with no offset."""

    def setUp(self):
        super().setUp()
        # Anchored to a real past date so the model's
        # `attendance_date_validate` (no future dates) can never be the
        # reason a case fails.
        self.day = date.today() - timedelta(days=1)

    def _assert_stored(self, hour, minute):
        Attendance.objects.filter(employee_id=self.employee).delete()
        response = self.clock_in_at(
            datetime(self.day.year, self.day.month, self.day.day, hour, minute, 0)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.attendance_on(self.day).attendance_clock_in, time(hour, minute)
        )
        self.assertEqual(response.data["clock_in"], f"{hour:02d}:{minute:02d}:00")

    def test_0800_vietnam_is_stored_as_0800(self):
        self._assert_stored(8, 0)

    def test_0815_vietnam_is_stored_as_0815(self):
        self._assert_stored(8, 15)

    def test_1230_vietnam_is_stored_as_1230(self):
        self._assert_stored(12, 30)

    def test_1745_vietnam_is_stored_as_1745(self):
        self._assert_stored(17, 45)

    def test_the_originally_reported_time_is_now_correct(self):
        """The exact regression: 08:17 Vietnam used to be stored as
        06:47 (its Asia/Kolkata reading). It must now be 08:17."""
        Attendance.objects.filter(employee_id=self.employee).delete()
        self.clock_in_at(
            datetime(self.day.year, self.day.month, self.day.day, 8, 17, 0)
        )
        stored = self.attendance_on(self.day).attendance_clock_in
        self.assertEqual(stored, time(8, 17))
        self.assertNotEqual(stored, time(6, 47), msg="Kolkata reading leaked")


class ClockOutTimeTests(AttendanceClockBase):
    """§5 / §13.5 — clock-out must use the same authoritative source."""

    def setUp(self):
        super().setUp()
        self.day = date.today() - timedelta(days=1)

    def test_clock_out_stores_vietnam_time(self):
        self.clock_in_at(
            datetime(self.day.year, self.day.month, self.day.day, 8, 0, 0)
        )
        response = self.clock_out_at(
            datetime(self.day.year, self.day.month, self.day.day, 17, 45, 0)
        )
        self.assertEqual(response.status_code, 200)
        attendance = self.attendance_on(self.day)
        self.assertEqual(attendance.attendance_clock_out, time(17, 45))
        self.assertEqual(response.data["clock_out"], "17:45:00")


class ApiRoundTripTests(AttendanceClockBase):
    """§8 / §13.6-7 — what Flutter actually reads back."""

    def setUp(self):
        super().setUp()
        self.day = date.today() - timedelta(days=1)

    def test_my_attendance_returns_the_same_vietnam_wall_clock(self):
        self.clock_in_at(
            datetime(self.day.year, self.day.month, self.day.day, 8, 15, 0)
        )
        self.clock_out_at(
            datetime(self.day.year, self.day.month, self.day.day, 17, 45, 0)
        )

        response = self.client.get(MY_ATTENDANCE_URL)
        self.assertEqual(response.status_code, 200)
        row = next(
            r
            for r in response.data["results"]
            if r["attendance_date"] == self.day.isoformat()
        )
        # Exactly the strings the Flutter DTO splits on and the web
        # template renders — no conversion happens on either side.
        self.assertEqual(row["attendance_clock_in"], "08:15:00")
        self.assertEqual(row["attendance_clock_out"], "17:45:00")


class MidnightBoundaryTests(AttendanceClockBase):
    """§6 / §13.8-9 — the date must follow Vietnam, not UTC or Kolkata.

    Vietnam 00:00:01 is 17:00:01 the *previous* day in UTC and 22:30:01
    the previous day in Asia/Kolkata, so a wrong clock would file the
    record under yesterday.
    """

    def setUp(self):
        super().setUp()
        self.late_day = date.today() - timedelta(days=2)
        self.next_day = self.late_day + timedelta(days=1)

    def test_235959_vietnam_stays_on_the_same_day(self):
        response = self.clock_in_at(
            datetime(
                self.late_day.year, self.late_day.month, self.late_day.day,
                23, 59, 59,
            )
        )
        self.assertEqual(response.status_code, 200)
        attendance = self.attendance_on(self.late_day)
        self.assertEqual(attendance.attendance_date, self.late_day)
        self.assertEqual(attendance.attendance_clock_in, time(23, 59))

    def test_000001_vietnam_rolls_over_to_the_next_day(self):
        response = self.clock_in_at(
            datetime(
                self.next_day.year, self.next_day.month, self.next_day.day,
                0, 0, 1,
            )
        )
        self.assertEqual(response.status_code, 200)
        attendance = self.attendance_on(self.next_day)
        self.assertEqual(
            attendance.attendance_date,
            self.next_day,
            msg="UTC/Kolkata date leaked — record filed under the wrong day",
        )
        self.assertEqual(attendance.attendance_clock_in, time(0, 0))
        # The previous Vietnam day must not have been touched at all.
        self.assertFalse(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.late_day
            ).exists()
        )


class PreservedBehaviourTests(AttendanceClockBase):
    """§10 / §13.10-11 — rules this phase must NOT have changed."""

    def setUp(self):
        super().setUp()
        self.day = date.today() - timedelta(days=1)

    def test_duplicate_clock_in_still_never_overwrites_the_first_time(self):
        """ATT-TIME-1 found that only the row-creating branch sets
        `attendance_clock_in`. That rule is deliberately untouched."""
        self.clock_in_at(
            datetime(self.day.year, self.day.month, self.day.day, 8, 0, 0)
        )
        self.clock_out_at(
            datetime(self.day.year, self.day.month, self.day.day, 12, 0, 0)
        )
        self.clock_in_at(
            datetime(self.day.year, self.day.month, self.day.day, 13, 0, 0)
        )

        self.assertEqual(
            self.attendance_on(self.day).attendance_clock_in,
            time(8, 0),
            msg="the first clock-in of the day must survive a re-clock-in",
        )

    def test_attendance_activity_keeps_a_correct_aware_instant(self):
        """`AttendanceActivity.in_datetime` is a DateTimeField, so unlike
        the naive TimeField it stores a real instant. It must line up
        with the same Vietnam wall clock."""
        wall_clock = datetime(
            self.day.year, self.day.month, self.day.day, 8, 15, 0
        )
        self.clock_in_at(wall_clock)

        activity = AttendanceActivity.objects.filter(
            employee_id=self.employee, attendance_date=self.day
        ).latest("id")
        self.assertEqual(
            activity.in_datetime.astimezone(VIETNAM).replace(
                second=0, microsecond=0, tzinfo=None
            ),
            wall_clock,
        )
        self.assertEqual(activity.clock_in_date, self.day)
