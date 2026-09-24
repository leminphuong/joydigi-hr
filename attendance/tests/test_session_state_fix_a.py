"""Phase FIX A — every workday stands on its own.

The production shape this exists to make impossible: an employee checked
in on Tuesday, forgot to check out, and on Wednesday their phone said
they were checked in — from Tuesday. It offered check-out; the server
refused, because by Wednesday Tuesday's day shift no longer counted.
Wednesday was unusable, and nothing in the data was corrupt.

So the tests below are mostly about what a *previous* day must not do to
*this* one, and — equally — about what must not happen to that previous
day while this one is fixed. Tuesday's row is evidence. It is not closed,
not guessed at, not normalised. Several tests assert that by snapshotting
it and comparing afterwards.

The night-shift tests are the counterweight: a session that legitimately
runs past midnight must keep working exactly as it did, so the rule being
introduced is "yesterday's *day* shift is not today's session", never
"yesterday is never today's session".
"""

from datetime import time, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.methods.session import (
    NIGHT_SHIFT_OPEN,
    NO_SESSION,
    STALE_PREVIOUS,
    TODAY_CLOSED,
    TODAY_MALFORMED,
    TODAY_OPEN,
    attendance_is_open,
    resolve_session,
)
from attendance.models import Attendance, AttendanceActivity
from base.models import (
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
)
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

CLOCK_IN = "/api/attendance/clock-in/"
CLOCK_OUT = "/api/attendance/clock-out/"
MY_ATTENDANCE = "/api/attendance/my-attendance/"


class SessionBase(TestCase):
    """One employee, an ordinary day shift, and two consecutive days."""

    NIGHT = False

    def setUp(self):
        self.company = make_company("Session Co")
        self.user = make_user("sessionuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="session@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )

        self.day2 = timezone.localdate()
        self.day1 = self.day2 - timedelta(days=1)
        for on in (self.day1, self.day2):
            EmployeeShiftSchedule.objects.get_or_create(
                shift_id=self.shift,
                day=EmployeeShiftDay.objects.get(day=on.strftime("%A").lower()),
                defaults={
                    "is_night_shift": self.NIGHT,
                    "minimum_working_hour": "08:00",
                    "start_time": time(22, 0) if self.NIGHT else time(8, 0),
                    "end_time": time(6, 0) if self.NIGHT else time(17, 0),
                },
            )
        if self.NIGHT:
            EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
                is_night_shift=True
            )

        self.client = APIClient()
        self.client.force_authenticate(user=self.fresh_user())

        # Phase FIX A.1B: automatic finalization only acts inside the
        # policy period, and with no cutoff configured it does nothing at
        # all. These tests assert the finalization behaviour, so they
        # declare a policy that covers their fixture dates. The cutoff
        # boundary itself is tested in
        # `attendance.tests.test_finalization_policy_cutoff`.
        cutoff = override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=(
                self.day1 - timedelta(days=1)
            ).isoformat()
        )
        cutoff.enable()
        self.addCleanup(cutoff.disable)

    def fresh_user(self):
        return type(self.user).objects.get(pk=self.user.pk)

    def day_of(self, on):
        return EmployeeShiftDay.objects.get(day=on.strftime("%A").lower())

    def open_row(self, on, *, clock_in=time(8, 0)):
        """An attendance + activity pair left open on `on`."""
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=on,
            attendance_day=self.day_of(on),
            shift_id=self.shift,
            attendance_clock_in=clock_in,
            attendance_clock_in_date=on,
            minimum_hour="08:00",
        )
        # The activity's instant is pushed at least three hours into the
        # past. `attendance_clock_in` stays the nominal shift time because
        # the day-shift rules read it, but the *activity* is what the
        # 30-minute minimum measures from — and pinning that to 08:00
        # wall-clock made these tests pass or fail depending on what time
        # of day the suite ran. The 30-minute rule has its own tests; it
        # must not be the thing that decides these.
        started = timezone.make_aware(timezone.datetime.combine(on, clock_in))
        long_enough_ago = timezone.localtime() - timedelta(hours=3)
        activity = AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=on,
            clock_in_date=on,
            shift_day=self.day_of(on),
            clock_in=clock_in,
            in_datetime=min(started, long_enough_ago),
        )
        return attendance, activity

    def snapshot(self, attendance, activity):
        attendance.refresh_from_db()
        activity.refresh_from_db()
        return (
            attendance.attendance_clock_in,
            attendance.attendance_clock_in_date,
            attendance.attendance_clock_out,
            attendance.attendance_clock_out_date,
            attendance.attendance_worked_hour,
            attendance.attendance_validated,
            activity.clock_in,
            activity.clock_out,
            activity.clock_out_date,
            activity.out_datetime,
        )

    def state(self):
        response = self.client.get(MY_ATTENDANCE)
        self.assertEqual(response.status_code, 200)
        return response.data["attendance_state"]["is_checked_in"]


class OpennessTests(TestCase):
    """The canonical rule, on its own."""

    def test_both_columns_empty_is_open(self):
        row = Attendance(attendance_clock_out=None, attendance_clock_out_date=None)
        self.assertIs(attendance_is_open(row), True)

    def test_both_columns_set_is_closed(self):
        row = Attendance(
            attendance_clock_out=time(17, 0),
            attendance_clock_out_date=timezone.localdate(),
        )
        self.assertIs(attendance_is_open(row), False)

    def test_one_column_set_is_neither(self):
        # Not a boolean on purpose: returning one would force this to
        # pick a column to believe, and picking is how a diagnosis turns
        # into a silent repair.
        self.assertIsNone(
            attendance_is_open(
                Attendance(
                    attendance_clock_out=time(17, 0), attendance_clock_out_date=None
                )
            )
        )
        self.assertIsNone(
            attendance_is_open(
                Attendance(
                    attendance_clock_out=None,
                    attendance_clock_out_date=timezone.localdate(),
                )
            )
        )

    def test_no_row_is_neither(self):
        self.assertIsNone(attendance_is_open(None))


class ResolverTests(SessionBase):
    """Which session, per day."""

    def test_nothing_at_all(self):
        self.assertEqual(resolve_session(self.employee, self.day2).state, NO_SESSION)

    def test_todays_open_row(self):
        self.open_row(self.day2)
        session = resolve_session(self.employee, self.day2)
        self.assertEqual(session.state, TODAY_OPEN)
        self.assertEqual(session.session_date, self.day2)
        self.assertTrue(session.is_online)
        self.assertTrue(session.can_check_out)

    def test_todays_finished_row(self):
        attendance, _activity = self.open_row(self.day2)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0), attendance_clock_out_date=self.day2
        )
        session = resolve_session(self.employee, self.day2)
        self.assertEqual(session.state, TODAY_CLOSED)
        self.assertFalse(session.is_online)
        self.assertTrue(session.can_check_in)

    def test_yesterdays_forgotten_day_shift_is_stale_not_current(self):
        self.open_row(self.day1)
        session = resolve_session(self.employee, self.day2)
        self.assertEqual(session.state, STALE_PREVIOUS)
        self.assertFalse(session.is_online)
        self.assertFalse(session.can_check_out)
        self.assertTrue(session.can_check_in)

    def test_a_half_written_row_is_neither_open_nor_closed(self):
        attendance, _activity = self.open_row(self.day2)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out_date=self.day2
        )
        session = resolve_session(self.employee, self.day2)
        self.assertEqual(session.state, TODAY_MALFORMED)
        # Online, so nothing offers to check in on top of it…
        self.assertTrue(session.is_online)
        # …and not checkoutable, so nothing writes to it either.
        self.assertFalse(session.can_check_out)


class NewDayIsNotBlockedTests(SessionBase):
    """Step 15 — the production shape, end to end through the API."""

    def setUp(self):
        super().setUp()
        self.stale_attendance, self.stale_activity = self.open_row(self.day1)
        self.stale_before = self.snapshot(self.stale_attendance, self.stale_activity)

    def test_yesterdays_open_day_shift_does_not_make_today_checked_in(self):
        self.assertFalse(self.state())

    def test_today_can_still_check_in(self):
        response = self.client.post(CLOCK_IN)
        self.assertEqual(response.status_code, 200, response.data)

    def test_checking_in_creates_todays_own_rows(self):
        self.client.post(CLOCK_IN)
        todays = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.day2
        )
        self.assertIsNotNone(todays.attendance_clock_in)
        self.assertIsNone(todays.attendance_clock_out)
        self.assertTrue(
            AttendanceActivity.objects.filter(
                employee_id=self.employee, attendance_date=self.day2
            ).exists()
        )

    def test_checking_in_finalizes_yesterday_at_its_shift_end(self):
        # Phase FIX A.1 changed this deliberately. FIX A left yesterday
        # open forever; the business decision since is that a forgotten
        # day shift must not survive into a later workday, so check-in
        # closes it at its own configured end time. What must still be
        # untouched is the employee's real arrival.
        self.client.post(CLOCK_IN)

        self.stale_attendance.refresh_from_db()
        self.stale_activity.refresh_from_db()
        self.assertEqual(self.stale_attendance.attendance_clock_out, time(17, 0))
        self.assertEqual(
            self.stale_attendance.attendance_clock_out_date, self.day1
        )
        self.assertIsNotNone(self.stale_activity.clock_out)
        # The arrival is the employee's own record and is never rewritten.
        self.assertEqual(self.stale_attendance.attendance_clock_in, time(8, 0))
        self.assertEqual(
            self.stale_attendance.attendance_clock_in_date, self.day1
        )

    def test_after_checking_in_the_state_is_checked_in(self):
        self.client.post(CLOCK_IN)
        self.assertTrue(self.state())

    def test_todays_checkout_closes_only_today(self):
        self.client.post(CLOCK_IN)
        # Reach back past the 30-minute rule without changing it.
        started = timezone.localtime() - timedelta(hours=3)
        AttendanceActivity.objects.filter(
            employee_id=self.employee, attendance_date=self.day2
        ).update(clock_in=started.time(), in_datetime=started)

        response = self.client.post(CLOCK_OUT)
        self.assertEqual(response.status_code, 200, response.data)

        todays = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.day2
        )
        self.assertIsNotNone(todays.attendance_clock_out)
        self.assertEqual(todays.attendance_clock_out_date, self.day2)

        # Yesterday was closed by the check-in above (Phase FIX A.1), at
        # its own shift end — never by today's check-out, and never with
        # today's date.
        self.stale_attendance.refresh_from_db()
        self.assertEqual(self.stale_attendance.attendance_clock_out, time(17, 0))
        self.assertEqual(
            self.stale_attendance.attendance_clock_out_date, self.day1
        )

    def test_after_todays_checkout_the_state_is_not_checked_in(self):
        self.client.post(CLOCK_IN)
        started = timezone.localtime() - timedelta(hours=3)
        AttendanceActivity.objects.filter(
            employee_id=self.employee, attendance_date=self.day2
        ).update(clock_in=started.time(), in_datetime=started)
        self.client.post(CLOCK_OUT)
        self.assertFalse(self.state())

    def test_checking_out_with_only_yesterday_open_is_refused_and_writes_nothing(self):
        # No check-in today. Yesterday's row must not be closed with a
        # time nobody observed.
        response = self.client.post(CLOCK_OUT)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "ALREADY_CLOCKED_OUT")
        self.assertEqual(
            self.snapshot(self.stale_attendance, self.stale_activity),
            self.stale_before,
        )


class NightShiftIsPreservedTests(SessionBase):
    """Step 16 — the counterweight. A real night shift still works."""

    NIGHT = True

    def setUp(self):
        super().setUp()
        self.night_attendance, self.night_activity = self.open_row(
            self.day1, clock_in=time(22, 0)
        )

    def test_last_nights_session_is_still_current_after_midnight(self):
        session = resolve_session(self.employee, self.day2)
        self.assertEqual(session.state, NIGHT_SHIFT_OPEN)
        self.assertEqual(session.session_date, self.day1)
        self.assertTrue(session.is_online)
        self.assertTrue(session.can_check_out)

    def test_the_employee_reads_as_checked_in_after_midnight(self):
        self.assertTrue(self.state())

    def test_checking_out_closes_the_night_pair_and_creates_no_new_row(self):
        before = Attendance.objects.filter(employee_id=self.employee).count()
        response = self.client.post(CLOCK_OUT)
        self.assertEqual(response.status_code, 200, response.data)

        self.assertEqual(
            Attendance.objects.filter(employee_id=self.employee).count(), before
        )
        self.night_attendance.refresh_from_db()
        self.night_activity.refresh_from_db()
        self.assertIsNotNone(self.night_attendance.attendance_clock_out)
        self.assertIsNotNone(self.night_attendance.attendance_clock_out_date)
        self.assertIsNotNone(self.night_activity.clock_out)
        # Closed against the day it began on, not against today.
        self.assertEqual(self.night_attendance.attendance_date, self.day1)

    def test_a_second_check_in_after_midnight_reuses_the_night_shift_day(self):
        # Existing night-shift behaviour, asserted so neither FIX A nor
        # FIX A.1 can quietly change which day a night check-in belongs
        # to. `perform_clock_in` treats a night shift as noon-to-noon: a
        # check-in before midday belongs to the night that started
        # yesterday, and one after midday starts tonight. The original
        # version of this test assumed the latter and so passed or failed
        # depending on what time of day the suite happened to run.
        self.client.post(CLOCK_OUT)
        self.client.post(CLOCK_IN)

        before_midday = timezone.localtime().hour < 12
        expected_date = self.day1 if before_midday else self.day2
        self.assertTrue(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=expected_date
            ).exists(),
            f"expected a row dated {expected_date}",
        )
        # Either way there is exactly one row per date — no duplicate.
        self.assertEqual(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=expected_date
            ).count(),
            1,
        )


class MalformedStateTests(SessionBase):
    """Step 17 — ambiguity is refused, never resolved by guessing."""

    def assert_conflict(self, response):
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertEqual(response.data["code"], "ATTENDANCE_STATE_CONFLICT")
        self.assertIn("quản trị viên", response.data["message"])
        # No row id, no column name, no internals.
        for leak in ("Attendance", "attendance_clock_out", "id=", "None"):
            self.assertNotIn(leak, response.data["message"], leak)

    def test_a_clock_out_without_its_date_is_refused_and_left_alone(self):
        attendance, activity = self.open_row(self.day2)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        before = self.snapshot(attendance, activity)
        self.assert_conflict(self.client.post(CLOCK_OUT))
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_a_clock_out_date_without_its_time_is_refused_and_left_alone(self):
        attendance, activity = self.open_row(self.day2)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out_date=self.day2
        )
        before = self.snapshot(attendance, activity)
        self.assert_conflict(self.client.post(CLOCK_OUT))
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_two_open_activities_on_the_same_day_are_refused(self):
        attendance, activity = self.open_row(self.day2)
        started = timezone.localtime() - timedelta(hours=3)
        AttendanceActivity.objects.filter(pk=activity.pk).update(
            clock_in=started.time(), in_datetime=started
        )
        second = AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=self.day2,
            clock_in_date=self.day2,
            shift_day=self.day_of(self.day2),
            clock_in=started.time(),
            in_datetime=started,
        )
        before = self.snapshot(attendance, activity)
        second_before = (second.clock_out, second.clock_out_date)

        self.assert_conflict(self.client.post(CLOCK_OUT))

        self.assertEqual(self.snapshot(attendance, activity), before)
        second.refresh_from_db()
        self.assertEqual((second.clock_out, second.clock_out_date), second_before)

    def test_a_malformed_row_never_reports_a_false_success(self):
        attendance, _activity = self.open_row(self.day2)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        self.assertNotEqual(self.client.post(CLOCK_OUT).status_code, 200)


class CrossDaySelectionTests(SessionBase):
    """The selection bug itself: two days, and only one may be touched."""

    def test_checkout_never_reaches_back_to_an_older_open_day(self):
        stale_attendance, stale_activity = self.open_row(self.day1)
        stale_before = self.snapshot(stale_attendance, stale_activity)

        started = timezone.localtime() - timedelta(hours=3)
        todays_attendance, todays_activity = self.open_row(self.day2)
        AttendanceActivity.objects.filter(pk=todays_activity.pk).update(
            clock_in=started.time(), in_datetime=started
        )

        response = self.client.post(CLOCK_OUT)
        self.assertEqual(response.status_code, 200, response.data)

        todays_attendance.refresh_from_db()
        todays_activity.refresh_from_db()
        self.assertIsNotNone(todays_attendance.attendance_clock_out)
        self.assertIsNotNone(todays_activity.clock_out)
        self.assertEqual(
            self.snapshot(stale_attendance, stale_activity), stale_before
        )

    def test_the_closed_row_is_the_one_whose_activity_was_closed(self):
        started = timezone.localtime() - timedelta(hours=3)
        _stale_attendance, _stale_activity = self.open_row(self.day1)
        todays_attendance, todays_activity = self.open_row(self.day2)
        AttendanceActivity.objects.filter(pk=todays_activity.pk).update(
            clock_in=started.time(), in_datetime=started
        )

        self.client.post(CLOCK_OUT)

        todays_attendance.refresh_from_db()
        todays_activity.refresh_from_db()
        self.assertEqual(
            todays_attendance.attendance_date, todays_activity.attendance_date
        )


class NoRowLockTests(SessionBase):
    """The transaction must not reintroduce what broke production."""

    def test_checking_out_issues_no_select_for_update(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        started = timezone.localtime() - timedelta(hours=3)
        _attendance, activity = self.open_row(self.day2)
        AttendanceActivity.objects.filter(pk=activity.pk).update(
            clock_in=started.time(), in_datetime=started
        )

        with CaptureQueriesContext(connection) as captured:
            self.client.post(CLOCK_OUT)

        for query in captured.captured_queries:
            self.assertNotIn("FOR UPDATE", query["sql"].upper())
