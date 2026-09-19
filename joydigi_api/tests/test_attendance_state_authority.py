"""
Phase FIX 1 — attendance server-authority state.

The incident: an employee on an ordinary day shift checks in, forgets to
check out, and comes back the next day. `check_online()` correctly stops
counting yesterday's forgotten row, so the server would accept a new
check-in — but the app worked out "checked in" for itself from the newest
row, saw yesterday's open row, and offered only check-out. The server
refused that, and nothing else was offered: the employee was stuck.

The fix makes the server say, in `GET /my-attendance/`, whether the person
is checked in right now — computed by the same `check_online()` the
clock-in and clock-out gates use, so what the app offers is always what the
server will accept.

Everything here drives the real API. Rows are created in the test database
only.
"""

from datetime import datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceActivity
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

MY_ATTENDANCE = "/api/attendance/my-attendance/"
CLOCK_IN = "/api/attendance/clock-in/"
CLOCK_OUT = "/api/attendance/clock-out/"


class AttendanceStateAuthorityBase(TestCase):
    def setUp(self):
        self.company = make_company("Authority Co")
        self.user = make_user("authorityuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="authority@test.joydigi", user=self.user
        )
        self.day_shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.day_shift
        )
        # The real calendar: `check_online()` asks about today and
        # yesterday as the server clock sees them.
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)
        for d in (self.today, self.yesterday):
            self.schedule(self.day_shift, d, night=False)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def schedule(self, shift, d, night):
        day = EmployeeShiftDay.objects.get(day=d.strftime("%A").lower())
        EmployeeShiftSchedule.objects.get_or_create(
            shift_id=shift,
            day=day,
            defaults={
                "is_night_shift": night,
                "minimum_working_hour": "08:00",
                "start_time": "22:00:00" if night else "08:00:00",
                "end_time": "06:00:00" if night else "17:00:00",
            },
        )
        return day

    def open_session_on(self, d, shift=None, at=time(8, 0)):
        shift = shift or self.day_shift
        day = EmployeeShiftDay.objects.get(day=d.strftime("%A").lower())
        clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=d,
            attendance_date=d,
            day=day,
            now=at.strftime("%H:%M"),
            shift=shift,
            minimum_hour="08:00",
            start_time=0,
            end_time=1,
            in_datetime=timezone.make_aware(datetime.combine(d, at)),
        )
        return Attendance.objects.get(employee_id=self.employee, attendance_date=d)

    def state(self):
        response = self.client.get(MY_ATTENDANCE)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["attendance_state"]["is_checked_in"]

    def snapshot(self, row):
        row.refresh_from_db()
        return (
            row.attendance_date,
            row.attendance_clock_in,
            row.attendance_clock_in_date,
            row.attendance_clock_out,
            row.attendance_clock_out_date,
            row.attendance_worked_hour,
            row.attendance_overtime,
            row.attendance_day_id,
            row.attendance_validated,
        )


class AuthoritativeStateTests(AttendanceStateAuthorityBase):
    """The six cases of the fix phase, A to F."""

    def test_A_yesterdays_open_day_shift_is_not_checked_in(self):
        self.open_session_on(self.yesterday)
        self.assertIs(self.state(), False)

    def test_B_yesterdays_open_night_shift_is_still_checked_in(self):
        night = EmployeeShift.objects.create(employee_shift="Night Shift")
        self.schedule(night, self.yesterday, night=True)
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=night
        )
        row = self.open_session_on(self.yesterday, shift=night, at=time(22, 0))
        self.assertTrue(row.is_night_shift(), "fixture must really be a night shift")

        self.assertIs(self.state(), True)

    def test_C_todays_open_session_is_checked_in(self):
        self.open_session_on(self.today)
        self.assertIs(self.state(), True)

    def test_D_todays_closed_session_is_not_checked_in(self):
        row = self.open_session_on(self.today)
        Attendance.objects.filter(pk=row.pk).update(
            attendance_clock_out=time(17, 0), attendance_clock_out_date=self.today
        )
        AttendanceActivity.objects.filter(employee_id=self.employee).update(
            clock_out=time(17, 0), clock_out_date=self.today
        )
        self.assertIs(self.state(), False)

    def test_E_no_attendance_at_all_is_not_checked_in(self):
        self.assertIs(self.state(), False)

    def test_F_an_account_with_no_employee_is_refused_cleanly(self):
        orphan = make_user("noemployeeuser", password="secret123")
        self.assertFalse(
            hasattr(orphan, "employee_get"),
            "fixture must really be an account with no employee",
        )
        client = APIClient()
        client.force_authenticate(user=orphan)

        for method, url in (
            ("get", MY_ATTENDANCE),
            ("post", CLOCK_IN),
            ("post", CLOCK_OUT),
        ):
            response = getattr(client, method)(url)
            self.assertEqual(response.status_code, 400, (url, response.status_code))
            self.assertEqual(response.data["code"], "EMPLOYEE_PROFILE_MISSING")
            self.assertTrue(response.data["message"])

    def test_the_response_is_additive(self):
        # Existing clients read these; none of them may disappear.
        self.open_session_on(self.today)
        data = self.client.get(MY_ATTENDANCE).data
        for key in ("count", "next", "previous", "results"):
            self.assertIn(key, data)
        self.assertEqual(data["results"][0]["attendance_date"], str(self.today))

    def test_the_state_matches_the_gate_the_app_will_call(self):
        # The whole point: whatever the state says, the matching action is
        # the one the server accepts.
        self.open_session_on(self.yesterday)
        self.assertIs(self.state(), False)
        self.assertEqual(self.client.post(CLOCK_OUT).data["code"], "ALREADY_CLOCKED_OUT")
        self.assertEqual(self.client.post(CLOCK_IN).status_code, 200)
        self.assertIs(self.state(), True)

    def test_reading_the_state_writes_nothing(self):
        row = self.open_session_on(self.yesterday)
        before = (
            Attendance.objects.count(),
            AttendanceActivity.objects.count(),
            self.snapshot(row),
        )
        for _ in range(3):
            self.state()
        self.assertEqual(
            (
                Attendance.objects.count(),
                AttendanceActivity.objects.count(),
                self.snapshot(row),
            ),
            before,
        )


class NextDayCheckInTests(AttendanceStateAuthorityBase):
    """Yesterday left open must not stop today from starting."""

    def test_todays_check_in_succeeds_and_leaves_yesterday_alone(self):
        yesterday_row = self.open_session_on(self.yesterday)
        before = self.snapshot(yesterday_row)

        response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 200, response.data)
        today_row = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertNotEqual(today_row.pk, yesterday_row.pk)
        self.assertEqual(response.data["attendance_id"], today_row.pk)
        # Yesterday's forgotten session is neither closed nor edited.
        self.assertEqual(self.snapshot(yesterday_row), before)
        self.assertIsNone(yesterday_row.attendance_clock_out)


class ControlledGateResponseTests(AttendanceStateAuthorityBase):
    def test_clocking_in_twice_has_a_stable_code(self):
        self.open_session_on(self.today)
        response = self.client.post(CLOCK_IN)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "ALREADY_CLOCKED_IN")
        self.assertTrue(response.data["message"])

    def test_clocking_out_while_not_checked_in_has_a_stable_code(self):
        response = self.client.post(CLOCK_OUT)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "ALREADY_CLOCKED_OUT")
        self.assertTrue(response.data["message"])


class NoFalseCheckOutSuccessTests(AttendanceStateAuthorityBase):
    """A check-out that closed nothing must never be reported as done."""

    def make_open_attendance_without_open_activity(self):
        # Inconsistent but real-world data: the Attendance row is still
        # open (so the gate lets the request through), yet no activity is
        # open to close — e.g. after a manual edit of the activity.
        row = self.open_session_on(self.today)
        AttendanceActivity.objects.filter(employee_id=self.employee).update(
            clock_out=time(9, 0), clock_out_date=self.today
        )
        return row

    def test_no_open_activity_is_never_http_200_clocked_out(self):
        self.make_open_attendance_without_open_activity()
        response = self.client.post(CLOCK_OUT)
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "NO_OPEN_ATTENDANCE")
        self.assertNotEqual(response.data.get("message"), "Clocked-Out")

    def test_no_open_activity_changes_nothing(self):
        row = self.make_open_attendance_without_open_activity()
        # Clear the day so `perform_clock_out` takes its backfill path and
        # writes it on the way through — the rollback must undo that too.
        Attendance.objects.filter(pk=row.pk).update(attendance_day=None)
        before = (
            Attendance.objects.count(),
            AttendanceActivity.objects.count(),
            self.snapshot(row),
        )

        self.client.post(CLOCK_OUT)

        self.assertEqual(
            (
                Attendance.objects.count(),
                AttendanceActivity.objects.count(),
                self.snapshot(row),
            ),
            before,
        )
        self.assertIsNone(Attendance.objects.get(pk=row.pk).attendance_day_id)
