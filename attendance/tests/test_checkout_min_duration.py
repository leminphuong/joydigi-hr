"""
Phase ATTENDANCE-CHECKOUT-30MIN-SAFE-IMPLEMENT-1.

Someone must stay checked in for a full 30 minutes before they may check
out. 08:00:00 to 08:29:59 is refused; 08:30:00 exactly is allowed.

Two things are asserted to the second here, because both have been wrong
before in this codebase:

* the elapsed time is a real duration, not a clock-face subtraction — a
  check-in at 08:00:30 is still short at 08:30:29;
* a refusal writes nothing at all. The tests below count every row the
  check-out path could touch, before and after, rather than checking only
  that the response said no.

The check-out path takes no row lock, and this file keeps watching for
that too: the PostgreSQL `FOR UPDATE` outage began as a well-meant
addition to exactly this function.
"""

import uuid
from datetime import datetime, time, timedelta
from unittest import mock

from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from attendance.methods.utils import Request
from attendance.methods.workday_rules import (
    MIN_CHECKOUT_MINUTES,
    can_check_out_yet,
    seconds_since_check_in,
)
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceLateComeEarlyOut,
    WorkRecords,
)
from attendance.views.clock_in_out import perform_clock_in, perform_clock_out
from base.models import (
    CheckInPolicy,
    Company,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation


class MinimumCheckOutDurationHelperTests(TestCase):
    """The rule as arithmetic, before any database is involved."""

    def at(self, hour, minute, second=0):
        return timezone.make_aware(
            datetime(2026, 9, 10, hour, minute, second)
        )

    def test_the_threshold_is_half_an_hour(self):
        self.assertEqual(MIN_CHECKOUT_MINUTES, 30)

    def test_a_second_short_of_half_an_hour_is_refused(self):
        self.assertFalse(
            can_check_out_yet(self.at(8, 0, 0), self.at(8, 29, 59))
        )

    def test_exactly_half_an_hour_is_allowed(self):
        self.assertTrue(can_check_out_yet(self.at(8, 0, 0), self.at(8, 30, 0)))

    def test_a_second_past_half_an_hour_is_allowed(self):
        self.assertTrue(can_check_out_yet(self.at(8, 0, 0), self.at(8, 30, 1)))

    def test_the_seconds_of_the_check_in_count(self):
        # 08:00:30 -> 08:30:29 is 1799 seconds: a clock-face "08:00 to 08:30"
        # reading would wrongly allow this.
        check_in = self.at(8, 0, 30)
        self.assertFalse(can_check_out_yet(check_in, self.at(8, 30, 29)))
        self.assertTrue(can_check_out_yet(check_in, self.at(8, 30, 30)))

    def test_elapsed_is_a_real_duration(self):
        self.assertEqual(
            seconds_since_check_in(self.at(8, 0, 30), self.at(8, 30, 29)), 1799
        )
        self.assertEqual(
            seconds_since_check_in(self.at(8, 0, 30), self.at(8, 30, 30)), 1800
        )

    def test_a_check_in_on_the_previous_day_is_long_enough(self):
        night = timezone.make_aware(datetime(2026, 9, 9, 22, 0))
        self.assertTrue(can_check_out_yet(night, self.at(6, 0)))

    def test_a_clock_running_backwards_is_refused(self):
        self.assertFalse(can_check_out_yet(self.at(9, 0), self.at(8, 0)))

    def test_a_naive_check_in_is_read_in_the_current_timezone(self):
        # Legacy rows may hold a naive stamp. It must not be compared as if
        # it were UTC, which would shift it by the offset and wave through a
        # check-out seconds after arriving.
        naive = datetime(2026, 9, 10, 8, 0, 0)
        self.assertFalse(can_check_out_yet(naive, self.at(8, 29, 59)))
        self.assertTrue(can_check_out_yet(naive, self.at(8, 30, 0)))

    def test_an_unusable_check_in_leaves_the_check_out_alone(self):
        # No opinion rather than a new way for check-out to fail.
        for value in (None, "", "08:00", 123):
            self.assertTrue(can_check_out_yet(value, self.at(9, 0)))
            self.assertIsNone(seconds_since_check_in(value, self.at(9, 0)))


class MinimumCheckOutDurationThroughCheckOutTests(TestCase):
    """The rule as the real check-in/check-out path applies it."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            company="Min Duration Corp", hq=True, address="x", country="VN",
            state="HN", city="HN", zip="10000",
        )
        CheckInPolicy.objects.create(company_id=cls.company, late_threshold_minutes=10)
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        cls.shift.company_id.add(cls.company)
        cls.work_type = WorkType.objects.create(work_type="Office")
        cls.work_type.company_id.add(cls.company)
        Department.objects.create(department="Eng").company_id.add(cls.company)

        cls.today = timezone.localtime().date()
        while cls.today.weekday() > 4:  # keep it a weekday
            cls.today -= timedelta(days=1)
        shift_day = EmployeeShiftDay.objects.filter(
            day=cls.today.strftime("%A").lower()
        ).first()
        schedule = EmployeeShiftSchedule.objects.create(
            day=shift_day, shift_id=cls.shift, minimum_working_hour="08:00",
            start_time=time(8, 0), end_time=time(17, 0),
        )
        schedule.company_id.add(cls.company)

    def setUp(self):
        tag = uuid.uuid4().hex[:10]
        self.employee = Employee.objects.create(
            employee_first_name="Min", employee_last_name=tag,
            email="min%s@test.local" % tag, phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        info.company_id = self.company
        info.shift_id = self.shift
        info.work_type_id = self.work_type
        info.save()

    def at(self, hour, minute=0, second=0):
        return timezone.make_aware(
            datetime.combine(self.today, time(hour, minute, second))
        )

    def request_at(self, moment):
        user = type(self.employee.employee_user_id).objects.get(
            pk=self.employee.employee_user_id.pk
        )
        return Request(
            user=user, date=moment.date(), time=moment.time(), datetime=moment,
            trusted_device=True,
        )

    def check_in(self, moment):
        attendance, allowed, reason = perform_clock_in(self.request_at(moment))
        self.assertTrue(allowed, reason)
        return attendance

    def check_out(self, moment):
        return perform_clock_out(self.request_at(moment))

    def snapshot(self):
        """Every row the check-out path could touch, plus the fields it writes."""
        attendance = Attendance.objects.filter(employee_id=self.employee).last()
        activity = AttendanceActivity.objects.filter(
            employee_id=self.employee
        ).order_by("attendance_date", "id").last()
        return {
            "attendance_rows": Attendance.objects.count(),
            "activity_rows": AttendanceActivity.objects.count(),
            "late_early_rows": AttendanceLateComeEarlyOut.objects.count(),
            "work_records": WorkRecords.objects.count(),
            "clock_out": attendance.attendance_clock_out if attendance else None,
            "clock_out_date": (
                attendance.attendance_clock_out_date if attendance else None
            ),
            "worked_hour": attendance.attendance_worked_hour if attendance else None,
            "overtime": attendance.attendance_overtime if attendance else None,
            "activity_clock_out": activity.clock_out if activity else None,
            "activity_out_datetime": activity.out_datetime if activity else None,
        }

    # ---------- A / B / C: the stated boundaries ----------

    def test_checking_out_a_second_early_is_refused(self):
        self.check_in(self.at(8, 0, 0))
        attendance, allowed, reason = self.check_out(self.at(8, 29, 59))

        self.assertFalse(allowed)
        self.assertIsNone(attendance)
        self.assertEqual(reason["code"], "CHECKOUT_TOO_SOON")
        self.assertIn("30", reason["message"])

    def test_checking_out_at_exactly_half_an_hour_is_allowed(self):
        self.check_in(self.at(8, 0, 0))
        attendance, allowed, reason = self.check_out(self.at(8, 30, 0))

        self.assertTrue(allowed, reason)
        self.assertIsNotNone(attendance)
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, time(8, 30))

    def test_checking_out_a_second_later_is_allowed(self):
        self.check_in(self.at(8, 0, 0))
        _attendance, allowed, reason = self.check_out(self.at(8, 30, 1))
        self.assertTrue(allowed, reason)

    # ---------- D / E: the seconds of the check-in count ----------

    def test_a_check_in_with_seconds_is_measured_to_the_second(self):
        self.check_in(self.at(8, 0, 30))
        _attendance, allowed, _reason = self.check_out(self.at(8, 30, 29))
        self.assertFalse(allowed)

    def test_a_check_in_with_seconds_is_allowed_once_the_half_hour_passes(self):
        self.check_in(self.at(8, 0, 30))
        _attendance, allowed, reason = self.check_out(self.at(8, 30, 30))
        self.assertTrue(allowed, reason)

    # ---------- F: a refusal writes nothing ----------

    def test_a_refused_check_out_changes_nothing_at_all(self):
        row = self.check_in(self.at(8, 0, 0))
        before = self.snapshot()

        _attendance, allowed, _reason = self.check_out(self.at(8, 29, 59))
        self.assertFalse(allowed)

        self.assertEqual(self.snapshot(), before)

        row.refresh_from_db()
        self.assertIsNone(row.attendance_clock_out)
        # The activity is still open, so the employee can check out later.
        open_activities = AttendanceActivity.objects.filter(
            employee_id=self.employee, clock_out__isnull=True
        )
        self.assertEqual(open_activities.count(), 1)
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=row, type="early_out"
            ).exists()
        )

    def test_a_refusal_does_not_prevent_checking_out_later(self):
        row = self.check_in(self.at(8, 0, 0))
        _a, allowed, _r = self.check_out(self.at(8, 10, 0))
        self.assertFalse(allowed)

        _a, allowed, reason = self.check_out(self.at(17, 0, 0))
        self.assertTrue(allowed, reason)
        row.refresh_from_db()
        self.assertEqual(row.attendance_clock_out, time(17, 0))

    # ---------- the ordinary flow is untouched beyond the threshold ----------

    def test_a_full_day_still_checks_out_normally(self):
        row = self.check_in(self.at(8, 0))
        _a, allowed, reason = self.check_out(self.at(17, 0))
        self.assertTrue(allowed, reason)

        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "08:00")
        self.assertEqual(row.attendance_overtime, "00:00")
        activities = AttendanceActivity.objects.filter(employee_id=self.employee)
        self.assertEqual(activities.count(), 1)
        self.assertEqual(activities.get().clock_out, time(17, 0))

    def test_early_out_still_applies_beyond_the_threshold(self):
        row = self.check_in(self.at(8, 0))
        _a, allowed, reason = self.check_out(self.at(16, 29))
        self.assertTrue(allowed, reason)
        self.assertTrue(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=row, type="early_out"
            ).exists()
        )

    def test_the_lunch_hour_is_still_excluded(self):
        row = self.check_in(self.at(11, 30))
        _a, allowed, reason = self.check_out(self.at(12, 30))
        self.assertTrue(allowed, reason)
        row.refresh_from_db()
        # One hour raw, half of it inside the unpaid 12:00-13:00 lunch.
        self.assertEqual(row.attendance_worked_hour, "00:30")

    def test_a_morning_that_is_long_enough_still_earns_half_a_day(self):
        # The 30-minute rule and the half-day credit are independent: this
        # one clears the threshold, so it reaches the credit rule and a
        # before-noon check-out is worth half a day.
        row = self.check_in(self.at(8, 30))
        _a, allowed, reason = self.check_out(self.at(9, 0))
        self.assertTrue(allowed, reason)
        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "00:30")
        self.assertEqual(row.attendance_clock_out, time(9, 0))

    def test_a_five_minute_morning_is_now_refused_before_it_is_credited(self):
        # Previously this was credited half a day. The 30-minute rule stops
        # it earlier, so the credit question is never reached.
        row = self.check_in(self.at(8, 30))
        _a, allowed, reason = self.check_out(self.at(8, 35))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_TOO_SOON")
        row.refresh_from_db()
        self.assertIsNone(row.attendance_clock_out)

    # ---------- the outage guard, again ----------

    def test_the_new_rule_takes_no_row_lock(self):
        """
        The 30-minute check reads one row and subtracts two datetimes. It
        must not lock anything: `select_for_update` on this path is what
        took production down, because PostgreSQL refuses FOR UPDATE with
        the manager's DISTINCT and with `Meta.ordering`'s outer join.
        SQLite ignores the call, so the call itself is what is watched.
        """
        self.check_in(self.at(8, 0))

        calls = []
        original = QuerySet.select_for_update

        def spy(self, *args, **kwargs):
            calls.append(self.model.__name__)
            return original(self, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            # Both the refused and the accepted path.
            _a, refused, _r = self.check_out(self.at(8, 10))
            _a, allowed, reason = self.check_out(self.at(17, 0))

        self.assertFalse(refused)
        self.assertTrue(allowed, reason)
        self.assertEqual(calls, [], "check-out must take no row lock")
