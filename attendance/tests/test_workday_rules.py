"""
Phase ATTENDANCE-WORKDAY-RULES-SAFE-IMPLEMENT-1.

Three clock-time rules for an ordinary working day:

* arriving through 08:30:59 is on time, 08:31:00 is late;
* leaving before 16:30 is early, 16:30 onwards is not;
* a day that ends before noon is worth half a day.

The official shift is still 08:00-17:00. The allowances are the rule, and
the shift rows are deliberately left alone rather than edited to fake them.

The boundaries are asserted to the second, because a rule written as "08:30"
can mean either 08:30:00 or the whole 08:30 minute, and a payroll dispute
turns on which. Every assertion below fixes the intended reading: the whole
minute is still on time.
"""

from datetime import date, datetime, time, timedelta
from unittest import mock

from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from attendance.methods.utils import Request
from attendance.methods.workday_rules import (
    day_credit,
    is_early_out,
    is_half_day,
    is_late,
)
from attendance.models import Attendance, AttendanceActivity, AttendanceLateComeEarlyOut
from attendance.period import attendance_day_value, build_period_context
from attendance.views.clock_in_out import perform_clock_in, perform_clock_out
from attendance.views.summary import build_monthly_summary
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

HOUR = 3600


class LateRuleTests(TestCase):
    """08:00-08:30 inclusive is on time; 08:31 onwards is late."""

    def test_arriving_before_the_official_start_is_on_time(self):
        self.assertFalse(is_late(time(7, 59)))

    def test_arriving_exactly_at_the_official_start_is_on_time(self):
        self.assertFalse(is_late(time(8, 0)))

    def test_arriving_within_the_allowance_is_on_time(self):
        self.assertFalse(is_late(time(8, 10)))
        self.assertFalse(is_late(time(8, 29)))

    def test_the_whole_of_the_half_past_minute_is_still_on_time(self):
        self.assertFalse(is_late(time(8, 30)))
        self.assertFalse(is_late(time(8, 30, 59)))

    def test_the_next_minute_is_late(self):
        self.assertTrue(is_late(time(8, 31)))
        self.assertTrue(is_late(time(8, 31, 0)))

    def test_arriving_well_after_the_allowance_is_late(self):
        self.assertTrue(is_late(time(9, 0)))
        self.assertTrue(is_late(time(14, 0)))

    def test_a_datetime_is_accepted_as_well_as_a_time(self):
        self.assertFalse(is_late(datetime(2026, 9, 9, 8, 30, 59)))
        self.assertTrue(is_late(datetime(2026, 9, 9, 8, 31)))

    def test_an_aware_datetime_is_read_in_its_own_local_time(self):
        aware = timezone.make_aware(datetime(2026, 9, 9, 8, 31))
        self.assertTrue(is_late(aware))
        aware_ok = timezone.make_aware(datetime(2026, 9, 9, 8, 30))
        self.assertFalse(is_late(aware_ok))

    def test_a_string_is_accepted(self):
        self.assertFalse(is_late("08:30:00"))
        self.assertTrue(is_late("08:31:00"))

    def test_a_missing_or_unreadable_time_is_not_treated_as_late(self):
        # Absence of evidence is not evidence of lateness.
        self.assertFalse(is_late(None))
        self.assertFalse(is_late("not a time"))
        self.assertFalse(is_late(12345))


class EarlyOutRuleTests(TestCase):
    """Leaving before 16:30 is early; 16:30 onwards is not."""

    def test_leaving_well_before_the_allowance_is_early(self):
        self.assertTrue(is_early_out(time(16, 0)))
        self.assertTrue(is_early_out(time(12, 0)))

    def test_the_last_second_before_half_past_four_is_early(self):
        self.assertTrue(is_early_out(time(16, 29)))
        self.assertTrue(is_early_out(time(16, 29, 59)))

    def test_half_past_four_exactly_is_not_early(self):
        self.assertFalse(is_early_out(time(16, 30)))
        self.assertFalse(is_early_out(time(16, 30, 0)))

    def test_leaving_between_the_allowance_and_the_official_end_is_not_early(self):
        self.assertFalse(is_early_out(time(16, 59)))

    def test_leaving_at_or_after_the_official_end_is_not_early(self):
        self.assertFalse(is_early_out(time(17, 0)))
        self.assertFalse(is_early_out(time(17, 30)))

    def test_a_missing_time_is_not_treated_as_early(self):
        self.assertFalse(is_early_out(None))


class HalfDayRuleTests(TestCase):
    """A day that ends before noon is worth half a day."""

    def test_a_morning_departure_is_half_a_day(self):
        self.assertTrue(is_half_day(time(11, 0)))
        self.assertTrue(is_half_day(time(11, 59)))
        self.assertTrue(is_half_day(time(11, 59, 59)))

    def test_noon_exactly_is_not_covered_by_the_rule(self):
        self.assertFalse(is_half_day(time(12, 0)))
        self.assertFalse(is_half_day(time(12, 0, 0)))

    def test_after_noon_is_not_covered_by_the_rule(self):
        self.assertFalse(is_half_day(time(12, 1)))
        self.assertFalse(is_half_day(time(17, 0)))

    def test_day_credit_is_half_before_noon_and_untouched_after(self):
        self.assertEqual(day_credit(time(11, 0)), 0.5)
        self.assertEqual(day_credit(time(12, 0)), 1.0)
        # Whatever the worked-time rules already decided survives noon.
        self.assertEqual(day_credit(time(12, 0), otherwise=0.0), 0.0)
        self.assertEqual(day_credit(time(12, 0), otherwise=0.5), 0.5)

    def test_a_missing_check_out_leaves_the_existing_credit_alone(self):
        self.assertEqual(day_credit(None, otherwise=1.0), 1.0)
        self.assertEqual(day_credit(None, otherwise=0.0), 0.0)


class DayValueIntegrationTests(TestCase):
    """`attendance_day_value` — the function the summaries actually call."""

    def test_a_full_day_is_unchanged(self):
        self.assertEqual(
            attendance_day_value(8 * HOUR, "08:00", 0, check_out=time(17, 0)), 1.0
        )

    def test_a_morning_only_day_is_half_regardless_of_worked_time(self):
        # 08:00-11:50 is 3h50 worked, short of half the 08:00 minimum, so the
        # worked-time rule alone would score it 0.0. The morning rule credits
        # it as half a day.
        worked = 3 * HOUR + 50 * 60
        self.assertEqual(
            attendance_day_value(worked, "08:00", 0, check_out=time(11, 50)), 0.5
        )

    def test_the_worked_time_rules_still_apply_after_noon(self):
        # A short afternoon day is judged the old way, not given a free half.
        self.assertEqual(
            attendance_day_value(30 * 60, "08:00", 0, check_out=time(13, 0)), 0.0
        )
        self.assertEqual(
            attendance_day_value(4 * HOUR, "08:00", 0, check_out=time(13, 0)), 0.5
        )

    def test_noon_is_the_boundary(self):
        worked = 4 * HOUR
        self.assertEqual(
            attendance_day_value(worked, "08:00", 0, check_out=time(11, 59, 59)), 0.5
        )
        self.assertEqual(
            attendance_day_value(worked, "08:00", 0, check_out=time(12, 0)), 0.5
        )
        # ...and a full worked day at noon is still a full day.
        self.assertEqual(
            attendance_day_value(8 * HOUR, "08:00", 0, check_out=time(12, 0)), 1.0
        )

    def test_omitting_the_check_out_keeps_the_previous_behaviour(self):
        # Callers that predate the rule must be unaffected.
        self.assertEqual(attendance_day_value(8 * HOUR, "08:00", 0), 1.0)
        self.assertEqual(attendance_day_value(4 * HOUR, "08:00", 0), 0.5)
        self.assertEqual(attendance_day_value(0, "08:00", 0), 0.0)


class WorkdayRulesThroughCheckOutTests(TestCase):
    """
    The rules as the real check-out path applies them, end to end.

    Also the guard for the outage this baseline was restored from: the
    check-out must take no row lock. SQLite ignores `select_for_update`
    entirely, so proving its *absence* is the one thing a SQLite test can do
    honestly — and it fails immediately if anyone reintroduces it.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            company="Workday Corp", hq=True, address="x", country="VN",
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
        cls.shift_day = EmployeeShiftDay.objects.filter(
            day=cls.today.strftime("%A").lower()
        ).first()
        # The official shift, untouched by these rules.
        schedule = EmployeeShiftSchedule.objects.create(
            day=cls.shift_day, shift_id=cls.shift, minimum_working_hour="08:00",
            start_time=time(8, 0), end_time=time(17, 0),
        )
        schedule.company_id.add(cls.company)

    def setUp(self):
        import uuid

        tag = uuid.uuid4().hex[:10]
        self.employee = Employee.objects.create(
            employee_first_name="Rule", employee_last_name=tag,
            email="rule%s@test.local" % tag, phone="9999999999",
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
            user=user, date=moment.date(), time=moment.time(),
            datetime=moment,
            # Attendance-source verification (GPS/WiFi/QR) is a different
            # feature with its own tests.
            trusted_device=True,
        )

    def check_in(self, moment):
        # The real check-in path, because that is where lateness is decided.
        attendance, allowed, reason = perform_clock_in(self.request_at(moment))
        self.assertTrue(allowed, reason)
        return attendance

    def check_out(self, moment):
        return perform_clock_out(self.request_at(moment))

    def flags(self, attendance):
        return set(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance
            ).values_list("type", flat=True)
        )

    def summary_row(self):
        rows, _tw, _t = build_monthly_summary(
            self.today, self.today, Employee.objects.filter(pk=self.employee.pk)
        )
        return rows[0]

    # ---------- late ----------

    def test_arriving_at_half_past_eight_records_no_lateness(self):
        row = self.check_in(self.at(8, 30))
        self.check_out(self.at(17, 0))
        self.assertNotIn("late_come", self.flags(row))

    def test_arriving_a_minute_later_records_lateness(self):
        row = self.check_in(self.at(8, 31))
        self.check_out(self.at(17, 0))
        self.assertIn("late_come", self.flags(row))

    def test_arriving_at_the_last_second_of_the_allowance_records_no_lateness(self):
        row = self.check_in(self.at(8, 30, 59))
        self.check_out(self.at(17, 0))
        self.assertNotIn("late_come", self.flags(row))

    def test_arriving_at_the_first_second_of_the_next_minute_records_lateness(self):
        row = self.check_in(self.at(8, 31, 0))
        self.check_out(self.at(17, 0))
        self.assertIn("late_come", self.flags(row))

    def test_arriving_on_the_hour_records_no_lateness(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(17, 0))
        self.assertNotIn("late_come", self.flags(row))

    # ---------- early out ----------

    def test_leaving_before_half_past_four_records_an_early_out(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 29))
        self.assertIn("early_out", self.flags(row))

    def test_leaving_a_second_before_half_past_four_records_an_early_out(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 29, 59))
        self.assertIn("early_out", self.flags(row))

    def test_leaving_at_half_past_four_exactly_records_no_early_out(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 30, 0))
        self.assertNotIn("early_out", self.flags(row))

    def test_leaving_at_half_past_four_records_no_early_out(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 30))
        self.assertNotIn("early_out", self.flags(row))

    def test_leaving_before_the_official_end_but_after_the_allowance_is_fine(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 59))
        self.assertNotIn("early_out", self.flags(row))

    def test_leaving_at_the_official_end_records_no_early_out(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(17, 0))
        self.assertNotIn("early_out", self.flags(row))

    # ---------- half day + worked hours ----------

    def test_a_morning_only_day_is_half_a_day_but_keeps_its_real_hours(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(11, 50))
        row.refresh_from_db()

        # Worked time is the real 3h50 — never rewritten to 4h to match the
        # half-day credit.
        self.assertEqual(row.attendance_worked_hour, "03:50")
        self.assertEqual(row.at_work_second, 3 * HOUR + 50 * 60)

        summary = self.summary_row()
        self.assertEqual(summary["present"], 0.5)

    def test_a_late_morning_start_still_credits_half_a_day(self):
        row = self.check_in(self.at(9, 0))
        self.check_out(self.at(11, 30))
        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "02:30")
        self.assertEqual(self.summary_row()["present"], 0.5)

    def test_a_short_morning_is_still_half_a_day(self):
        # The credit rule is deliberately literal: any check-out before noon
        # is worth half a day, with no minimum worked time — the business
        # decision confirmed in ATTENDANCE-WORKDAY-RULES-FINAL-VERIFY-COMMIT-1.
        # Thirty minutes here only because a check-out any sooner is now
        # refused outright by ATTENDANCE-CHECKOUT-30MIN-SAFE-IMPLEMENT-1 and
        # never reaches the credit question at all; the two rules are
        # independent, and `test_checkout_min_duration` covers that one.
        row = self.check_in(self.at(8, 30))
        self.check_out(self.at(9, 0))
        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "00:30")
        self.assertEqual(row.at_work_second, 30 * 60)
        self.assertEqual(self.summary_row()["present"], 0.5)

    def test_the_last_second_before_noon_is_half_a_day(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(11, 59, 59))
        self.assertEqual(self.summary_row()["present"], 0.5)

    def test_a_day_ending_at_noon_is_not_covered_by_the_morning_rule(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(12, 0))
        # 4h worked against an 08:00 minimum is half a day by the existing
        # worked-time rule — not by the morning rule.
        self.assertEqual(self.summary_row()["present"], 0.5)

    def test_a_short_span_across_noon_is_judged_on_worked_time(self):
        row = self.check_in(self.at(11, 30))
        self.check_out(self.at(12, 30))
        row.refresh_from_db()
        # 11:30-12:30 is one hour raw, half of it inside the unpaid lunch.
        self.assertEqual(row.attendance_worked_hour, "00:30")
        # Check-out is 12:30, so the morning rule does not apply.
        self.assertEqual(self.summary_row()["present"], 0.0)

    def test_a_full_day_is_still_a_full_day_with_eight_hours(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(17, 0))
        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "08:00")
        self.assertEqual(row.attendance_overtime, "00:00")
        self.assertEqual(self.summary_row()["present"], 1.0)

    # ---------- the outage guard ----------

    def test_checking_out_takes_no_row_lock(self):
        """
        Production went down when check-out locked an Attendance row:
        `JoydigiCompanyManager` adds DISTINCT and `Attendance.Meta.ordering`
        forces a LEFT OUTER JOIN on a nullable FK, and PostgreSQL refuses
        FOR UPDATE with either. SQLite ignores `select_for_update`, so this
        watches for the call itself rather than the SQL.
        """
        self.check_in(self.at(8, 0))

        calls = []
        original = QuerySet.select_for_update

        def spy(self, *args, **kwargs):
            calls.append(self.model.__name__)
            return original(self, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            _attendance, allowed, reason = self.check_out(self.at(17, 0))

        self.assertTrue(allowed, reason)
        self.assertEqual(
            calls, [], "check-out must take no row lock — see the docstring"
        )

    def test_checking_out_stays_a_single_check_out(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(17, 0))
        row.refresh_from_db()
        self.assertEqual(row.attendance_clock_out, time(17, 0))
        # One activity, closed in place; no second session invented.
        activities = AttendanceActivity.objects.filter(employee_id=self.employee)
        self.assertEqual(activities.count(), 1)
        self.assertEqual(activities.get().clock_out, time(17, 0))


class WeekendUnaffectedTests(TestCase):
    """The new weekday rules must not reach into weekend classification."""

    def test_a_weekend_with_no_attendance_is_still_a_week_off(self):
        company = Company.objects.create(
            company="Weekend Guard Corp", hq=True, address="x", country="VN",
            state="HN", city="HN", zip="10000",
        )
        shift = EmployeeShift.objects.create(employee_shift="Guard Shift")
        shift.company_id.add(company)
        employee = Employee.objects.create(
            employee_first_name="Guard", employee_last_name="Emp",
            email="guard@test.local", phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=employee)
        info.company_id = company
        info.shift_id = shift
        info.save()

        saturday = timezone.localtime().date()
        while saturday.weekday() != 5:
            saturday -= timedelta(days=1)

        ctx = build_period_context(saturday, saturday, [employee.pk])
        self.assertIn(saturday, ctx.employee_off_dates(employee.pk))
