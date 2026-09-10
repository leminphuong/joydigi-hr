"""
Worked-time arithmetic for the check-out path.

The company's working day is 08:00-12:00 and 13:00-17:00: the 12:00-13:00
lunch hour is unpaid and must not count as worked time. Before it was
excluded, a standard 08:30-17:30 day was recorded as 9h against an 8h
`minimum_working_hour` and produced a phantom hour of overtime every day.

Phase ATTENDANCE-CHECKOUT-SAFE-ROLLBACK-1: the corrected-checkout feature
that once lived alongside this rule has been rolled back — there is no
second check-out, no `checkout_count`, no 30-minute lock. The lunch rule
stayed: it is a separate business rule, it was not what made check-out
fail, and `attendance.methods.worktime` is now shared with the
weekend-overtime summary.
"""

from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from attendance.methods.utils import Request
from attendance.methods.worktime import (
    activities_worked_seconds,
    lunch_overlap_seconds,
    worked_seconds,
)
from attendance.models import Attendance, AttendanceActivity
from attendance.views.clock_in_out import perform_clock_in, perform_clock_out
from base.models import (
    Company,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation
from joydigi.joydigi_middlewares import set_selected_company

HOUR = 3600


def _dt(day, hour, minute=0, second=0):
    """Naive datetime, for the pure worked-time helpers."""
    return datetime.combine(day, time(hour, minute, second))


class LunchExclusionTests(TestCase):
    """
    The exact table the business specified. These are the numbers a payroll
    dispute would be settled with, so each is asserted on its own rather
    than looped — a failure names the case.
    """

    day = date(2026, 3, 2)

    def _worked(self, start, end):
        return worked_seconds(_dt(self.day, *start), _dt(self.day, *end))

    def test_full_day_spanning_lunch_is_eight_hours(self):
        self.assertEqual(self._worked((8, 0), (17, 0)), 8 * HOUR)

    def test_late_start_still_loses_the_whole_lunch_hour(self):
        self.assertEqual(self._worked((9, 0), (17, 0)), 7 * HOUR)

    def test_early_finish_still_loses_the_whole_lunch_hour(self):
        self.assertEqual(self._worked((8, 0), (16, 0)), 7 * HOUR)

    def test_morning_only_never_reaches_lunch(self):
        self.assertEqual(self._worked((8, 0), (11, 0)), 3 * HOUR)

    def test_partial_overlap_loses_only_the_overlapping_half_hour(self):
        # The case a flat "subtract one hour" would get wrong twice over:
        # it would wipe out the entire span and clamp at zero.
        self.assertEqual(self._worked((11, 30), (12, 30)), HOUR // 2)

    def test_a_span_entirely_inside_lunch_is_worth_nothing(self):
        self.assertEqual(self._worked((12, 0), (13, 0)), 0)

    def test_starting_mid_lunch_only_counts_from_thirteen(self):
        self.assertEqual(self._worked((12, 30), (17, 0)), 4 * HOUR)

    def test_starting_after_lunch_is_untouched(self):
        self.assertEqual(self._worked((13, 0), (17, 0)), 4 * HOUR)

    def test_the_shift_actually_configured_locally_comes_to_eight_hours(self):
        # Local `EmployeeShiftSchedule` is 08:30-17:30 against an 08:00
        # minimum. Before lunch exclusion that read as 9h and manufactured
        # an hour of overtime every single day.
        self.assertEqual(self._worked((8, 30), (17, 30)), 8 * HOUR)

    def test_a_reversed_span_contributes_nothing_rather_than_going_negative(self):
        self.assertEqual(self._worked((17, 0), (8, 0)), 0)

    def test_overlap_is_measured_not_assumed(self):
        self.assertEqual(
            lunch_overlap_seconds(_dt(self.day, 11, 30), _dt(self.day, 12, 30)),
            HOUR // 2,
        )
        self.assertEqual(
            lunch_overlap_seconds(_dt(self.day, 8, 0), _dt(self.day, 11, 0)), 0
        )

    def test_a_shift_crossing_midnight_loses_each_day_s_lunch(self):
        # Two calendar days spanned, so two lunch windows — a single
        # flat subtraction would under-deduct by an hour.
        start = _dt(self.day, 8, 0)
        end = _dt(self.day + timedelta(days=1), 17, 0)
        self.assertEqual(lunch_overlap_seconds(start, end), 2 * HOUR)


class ActivityAggregationTests(TestCase):
    """`activities_worked_seconds` over the rows a real day produces."""

    class _Activity:
        def __init__(self, day, start, end):
            self.clock_in_date = day
            self.clock_in = time(*start)
            self.clock_out_date = day if end else None
            self.clock_out = time(*end) if end else None

    day = date(2026, 3, 2)

    def test_split_sessions_each_lose_only_their_own_lunch_overlap(self):
        activities = [
            self._Activity(self.day, (8, 0), (11, 30)),
            self._Activity(self.day, (12, 30), (17, 0)),
        ]
        # 3h30 (no overlap) + 4h00 (4h30 raw, less its own 12:30-13:00
        # overlap) = 7h30. Each session is charged only the lunch it
        # actually spans, and the 11:30-12:30 gap between them — never
        # worked — is not charged to anyone.
        self.assertEqual(activities_worked_seconds(activities), 7 * HOUR + 1800)

    def test_an_open_session_contributes_nothing(self):
        activities = [
            self._Activity(self.day, (8, 0), (11, 0)),
            self._Activity(self.day, (13, 0), None),
        ]
        self.assertEqual(activities_worked_seconds(activities), 3 * HOUR)


class CheckOutFlowTests(TestCase):
    """
    The restored single check-out, through `perform_clock_out` — the shared
    entry point web, mobile, face and the scheduler all use.

    Times are pinned explicitly on the request (the `Request` shim's
    `date`/`time`/`datetime`) instead of depending on when the suite runs.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            company="Worktime Corp", hq=True, address="1 Test St",
            country="VN", state="HN", city="HN", zip="10000",
        )
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        cls.shift.company_id.add(cls.company)
        cls.work_type = WorkType.objects.create(work_type="Office")
        cls.work_type.company_id.add(cls.company)
        Department.objects.create(department="Engineering").company_id.add(cls.company)

        cls.today = timezone.localtime().date()
        cls.shift_day = EmployeeShiftDay.objects.filter(
            day=cls.today.strftime("%A").lower()
        ).first()
        for day in EmployeeShiftDay.objects.all():
            schedule = EmployeeShiftSchedule.objects.create(
                day=day, shift_id=cls.shift, minimum_working_hour="08:00",
                start_time=time(8, 0), end_time=time(17, 0),
            )
            schedule.company_id.add(cls.company)

        cls.employee = Employee.objects.create(
            employee_first_name="Worktime", employee_last_name="Tester",
            email="worktime@test.local", phone="9999999999",
        )
        EmployeeWorkInformation.objects.filter(employee_id=cls.employee).update(
            company_id_id=cls.company.pk, shift_id_id=cls.shift.pk,
            work_type_id_id=cls.work_type.pk,
        )
        cls.user = cls.employee.employee_user_id

    def at(self, hour, minute=0, second=0, day=None):
        return timezone.make_aware(
            datetime.combine(day or self.today, time(hour, minute, second))
        )

    def check_in(self, moment, day=None):
        day = day or self.today
        AttendanceActivity.objects.create(
            employee_id=self.employee, attendance_date=day,
            shift_day=self.shift_day, clock_in_date=day,
            clock_in=moment.time(), in_datetime=moment,
        )
        return Attendance.objects.create(
            employee_id=self.employee, attendance_date=day, shift_id=self.shift,
            attendance_day=self.shift_day, attendance_clock_in_date=day,
            attendance_clock_in=moment.time(), minimum_hour="08:00",
        )

    def check_out(self, moment):
        user = type(self.user).objects.get(pk=self.user.pk)
        return perform_clock_out(
            Request(
                user=user, date=moment.date(), time=moment.time(),
                datetime=moment,
                # Attendance-source verification (GPS/WiFi/QR) is a
                # different feature with its own tests.
                trusted_device=True,
            )
        )

    def test_a_standard_day_is_eight_hours_and_produces_no_overtime(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(17, 0))
        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "08:00")
        # OT formula untouched: max(0, worked - minimum_hour), and worked
        # now equals the 08:00 minimum exactly.
        self.assertEqual(row.attendance_overtime, "00:00")
        self.assertEqual(row.overtime_second, 0)
        self.assertEqual(row.at_work_second, 8 * HOUR)

    def test_a_short_day_records_worked_time_net_of_lunch(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "07:00")
        self.assertEqual(row.attendance_clock_out, time(16, 0))

    def test_overtime_still_appears_when_the_day_genuinely_runs_long(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(18, 30))
        row.refresh_from_db()
        # 10h30 raw - 1h lunch = 9h30 worked; 1h30 over the 08:00 minimum.
        self.assertEqual(row.attendance_worked_hour, "09:30")
        self.assertEqual(row.attendance_overtime, "01:30")

    def test_checking_out_closes_the_activity_without_adding_one(self):
        self.check_in(self.at(8, 0))
        before = AttendanceActivity.objects.filter(employee_id=self.employee).count()
        self.check_out(self.at(17, 0))
        activities = AttendanceActivity.objects.filter(employee_id=self.employee)
        self.assertEqual(activities.count(), before)
        self.assertEqual(activities.get().clock_out, time(17, 0))

    def test_checking_out_immediately_after_arriving_is_refused(self):
        # Phase ATTENDANCE-CHECKOUT-30MIN-SAFE-IMPLEMENT-1 reinstates a
        # minimum wait, this time as a plain read-and-subtract with no row
        # lock. The boundaries live in `test_checkout_min_duration`.
        row = self.check_in(self.at(8, 0))
        _attendance, allowed, reason = self.check_out(self.at(8, 5))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_TOO_SOON")
        row.refresh_from_db()
        self.assertIsNone(row.attendance_clock_out)


class CheckOutCompanyIsolationTests(TestCase):
    """
    One company must never be able to close another company's attendance.
    The employee is derived from the authenticated user, never the payload.
    """

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localtime().date()
        cls.shift_day = EmployeeShiftDay.objects.filter(
            day=cls.today.strftime("%A").lower()
        ).first()

    def _company(self, name, email):
        company = Company.objects.create(
            company=name, hq=True, address="x", country="VN",
            state="HN", city="HN", zip="10000",
        )
        shift = EmployeeShift.objects.create(employee_shift="Shift " + name)
        shift.company_id.add(company)
        work_type = WorkType.objects.create(work_type="WT " + name)
        work_type.company_id.add(company)
        schedule = EmployeeShiftSchedule.objects.create(
            day=self.shift_day, shift_id=shift, minimum_working_hour="08:00",
            start_time=time(8, 0), end_time=time(17, 0),
        )
        schedule.company_id.add(company)
        employee = Employee.objects.create(
            employee_first_name=name, employee_last_name="Emp",
            email=email, phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=employee)
        info.company_id = company
        info.shift_id = shift
        info.work_type_id = work_type
        info.save()
        return company, shift, employee

    def _open_day(self, employee, shift):
        moment = timezone.make_aware(datetime.combine(self.today, time(8, 0)))
        AttendanceActivity.objects.create(
            employee_id=employee, attendance_date=self.today,
            shift_day=self.shift_day, clock_in_date=self.today,
            clock_in=time(8, 0), in_datetime=moment,
        )
        return Attendance.objects.create(
            employee_id=employee, attendance_date=self.today, shift_id=shift,
            attendance_day=self.shift_day, attendance_clock_in_date=self.today,
            attendance_clock_in=time(8, 0), minimum_hour="08:00",
        )

    def test_one_company_cannot_check_out_anothers_attendance(self):
        company_a, shift_a, emp_a = self._company("Alpha", "alpha@test.local")
        _company_b, shift_b, emp_b = self._company("Beta", "beta@test.local")
        att_a = self._open_day(emp_a, shift_a)
        att_b = self._open_day(emp_b, shift_b)

        set_selected_company(str(company_a.pk))
        self.addCleanup(set_selected_company, None)

        user = type(emp_a.employee_user_id).objects.get(
            pk=emp_a.employee_user_id.pk
        )
        moment = timezone.make_aware(datetime.combine(self.today, time(17, 0)))
        result, allowed, _reason = perform_clock_out(
            Request(
                user=user, date=moment.date(), time=moment.time(),
                datetime=moment, trusted_device=True,
            )
        )

        # Alpha's own day closed...
        self.assertTrue(allowed)
        self.assertEqual(result.pk, att_a.pk)
        # ...and Beta's is untouched.
        att_b.refresh_from_db()
        self.assertIsNone(att_b.attendance_clock_out)
