"""
Phase ATTENDANCE-WEEKEND-OT-REQUEST-IMPLEMENT-1.

Saturday and Sunday are week-off days, so nobody checks in on them and no
Attendance row exists. An employee who works a weekend files an overtime
request instead, and once it is approved those hours must appear in the
"Làm thêm giờ" column's HN bucket — while the day itself stays a week-off,
never turning into a present day or an absence.

The total is *derived* from the approved requests every time the summary is
built rather than written into a synthetic Attendance row. That is what these
tests lean on: approving twice changes nothing because nothing accumulates,
and a cancellation drops out on its own because the total is recomputed from
whatever is still approved.

Weekdays are deliberately excluded. There, an approved request stays what it
has always been — a granted permission — and NT keeps coming from real
attendance alone, so the two can never be counted twice for the same hour.
"""

from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.methods.utils import Request
from attendance.methods.worktime import (
    approved_overtime_seconds,
    merge_time_windows,
    overtime_request_seconds,
)
from attendance.models import Attendance, AttendanceActivity, OvertimeRequest
from attendance.views.clock_in_out import perform_clock_in, perform_clock_out
from attendance.views.summary import build_monthly_summary
from base.models import (
    CheckInPolicy,
    Company,
    CompanyLeaves,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    Holidays,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation

HOUR = 3600
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


class OvertimeDurationTests(TestCase):
    """The pure arithmetic, straight from the approved business rules."""

    def _secs(self, start, end):
        return overtime_request_seconds(time(*start), time(*end))

    def test_a_morning_window_owes_no_lunch(self):
        self.assertEqual(self._secs((9, 0), (12, 0)), 3 * HOUR)

    def test_a_window_that_is_exactly_lunch_is_worth_nothing(self):
        self.assertEqual(self._secs((12, 0), (13, 0)), 0)

    def test_a_window_half_inside_lunch_loses_only_that_half(self):
        self.assertEqual(self._secs((11, 30), (12, 30)), HOUR // 2)

    def test_a_window_starting_mid_lunch_counts_from_thirteen(self):
        self.assertEqual(self._secs((12, 30), (17, 0)), 4 * HOUR)

    def test_a_window_starting_after_lunch_is_untouched(self):
        self.assertEqual(self._secs((13, 0), (17, 0)), 4 * HOUR)

    def test_a_full_day_window_loses_the_whole_lunch_hour(self):
        self.assertEqual(self._secs((9, 0), (17, 0)), 7 * HOUR)

    def test_a_reversed_window_is_worth_nothing_rather_than_negative(self):
        self.assertEqual(self._secs((17, 0), (9, 0)), 0)


class OvertimeMergeTests(TestCase):
    """Overlaps are merged before measuring, never summed blindly."""

    def _total(self, *windows):
        return approved_overtime_seconds(
            [(time(*s), time(*e)) for s, e in windows]
        )

    def test_two_separate_windows_add_up(self):
        self.assertEqual(self._total(((9, 0), (12, 0)), ((13, 0), (17, 0))), 7 * HOUR)

    def test_adjacent_windows_are_measured_as_one_stretch(self):
        # 09-12 then 12-13: merging gives 09-13, less the lunch hour = 3h —
        # the same answer as measuring them separately (3h + 0h).
        self.assertEqual(self._total(((9, 0), (12, 0)), ((12, 0), (13, 0))), 3 * HOUR)
        self.assertEqual(self._total(((9, 0), (12, 0)), ((12, 0), (17, 0))), 7 * HOUR)

    def test_an_overlapping_hour_is_counted_once(self):
        # Rejected at creation, but data written before that check existed
        # must still not be double-counted.
        self.assertEqual(self._total(((9, 0), (12, 0)), ((10, 0), (12, 0))), 3 * HOUR)

    def test_a_window_fully_inside_another_adds_nothing(self):
        self.assertEqual(self._total(((9, 0), (17, 0)), ((10, 0), (11, 0))), 7 * HOUR)

    def test_merging_is_order_independent(self):
        self.assertEqual(
            self._total(((13, 0), (17, 0)), ((9, 0), (12, 0))), 7 * HOUR
        )

    def test_merge_returns_disjoint_spans(self):
        merged = merge_time_windows(
            [(time(9, 0), time(12, 0)), (time(10, 0), time(14, 0))]
        )
        self.assertEqual(merged, [(9 * HOUR, 14 * HOUR)])


class WeekendOvertimeBaseTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            company="OT Corp", hq=True, address="x", country="VN",
            state="HN", city="HN", zip="10000",
        )
        CheckInPolicy.objects.create(company_id=cls.company, late_threshold_minutes=10)
        # Saturday + Sunday as recurring weekly off days — the same
        # configuration production runs.
        for code in ("5", "6"):
            leave = CompanyLeaves.objects.create(
                based_on_week=None, based_on_week_day=code
            )
            leave.company_id.add(cls.company)

        cls.shift = EmployeeShift.objects.create(
            employee_shift="Ca hành chính 08:00-17:00", weekly_full_time="40:00"
        )
        cls.shift.company_id.add(cls.company)
        cls.work_type = WorkType.objects.create(work_type="Office")
        cls.work_type.company_id.add(cls.company)
        Department.objects.create(department="Eng").company_id.add(cls.company)

        # Monday-Friday only: a weekend has no schedule, which is exactly why
        # it cannot be checked into.
        for name in WEEKDAYS:
            day = EmployeeShiftDay.objects.filter(day=name).first()
            schedule = EmployeeShiftSchedule.objects.create(
                day=day, shift_id=cls.shift, minimum_working_hour="08:00",
                start_time=time(8, 0), end_time=time(17, 0),
            )
            schedule.company_id.add(cls.company)

    def setUp(self):
        import uuid

        tag = uuid.uuid4().hex[:10]
        self.employee = Employee.objects.create(
            employee_first_name="OT", employee_last_name=tag,
            email="ot%s@test.local" % tag, phone="9999999999",
        )
        work_info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        work_info.company_id = self.company
        work_info.shift_id = self.shift
        work_info.work_type_id = self.work_type
        work_info.save()

    # ---------- helpers ----------

    def saturday(self):
        day = timezone.localtime().date()
        while day.weekday() != 5:
            day -= timedelta(days=1)
        return day

    def weekday(self):
        day = timezone.localtime().date()
        while day.weekday() > 4:
            day -= timedelta(days=1)
        return day

    def request_for(self, day, start, end, approved=True, employee=None):
        return OvertimeRequest.objects.create(
            employee_id=employee or self.employee,
            request_date=day,
            start_time=time(*start),
            end_time=time(*end),
            approved=approved,
        )

    def summary(self, day, until=None):
        rows, _total, _extra = build_monthly_summary(
            day, until or day, Employee.objects.filter(pk=self.employee.pk)
        )
        return rows[0]

    def clock(self, day, start, end):
        def _req(hour, minute):
            user = self.employee.employee_user_id
            user = type(user).objects.get(pk=user.pk)
            moment = timezone.make_aware(
                datetime.combine(day, time(hour, minute))
            )
            return Request(
                user=user, date=moment.date(), time=moment.time(),
                datetime=moment, trusted_device=True,
            )

        perform_clock_in(_req(*start))
        perform_clock_out(_req(*end))


class WeekendOvertimeDisplayTests(WeekendOvertimeBaseTest):
    def test_an_approved_saturday_request_shows_without_anyone_checking_in(self):
        saturday = self.saturday()
        self.request_for(saturday, (9, 0), (12, 0))

        row = self.summary(saturday)

        self.assertEqual(row["ot_week_off_seconds"], 3 * HOUR)
        self.assertEqual(row["overtime_seconds"], 3 * HOUR)
        # The day is still a week-off. Not a day worked, not an absence.
        self.assertEqual(row["week_off"], 1)
        self.assertEqual(row["present"], 0.0)
        self.assertEqual(row["absent"], 0.0)
        # ...and nothing was written to make that happen.
        self.assertFalse(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=saturday
            ).exists()
        )
        self.assertFalse(
            AttendanceActivity.objects.filter(employee_id=self.employee).exists()
        )

    def test_a_full_saturday_request_is_seven_hours_not_eight(self):
        saturday = self.saturday()
        self.request_for(saturday, (9, 0), (17, 0))
        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], 7 * HOUR)

    def test_a_request_covering_only_lunch_adds_nothing(self):
        saturday = self.saturday()
        self.request_for(saturday, (12, 0), (13, 0))
        row = self.summary(saturday)
        self.assertEqual(row["ot_week_off_seconds"], 0)
        self.assertEqual(row["week_off"], 1)

    def test_a_half_hour_straddling_lunch(self):
        saturday = self.saturday()
        self.request_for(saturday, (11, 30), (12, 30))
        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], HOUR // 2)

    def test_an_afternoon_request_starting_inside_lunch(self):
        saturday = self.saturday()
        self.request_for(saturday, (12, 30), (17, 0))
        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], 4 * HOUR)

    def test_two_approved_requests_on_one_day_add_up(self):
        saturday = self.saturday()
        self.request_for(saturday, (9, 0), (12, 0))
        self.request_for(saturday, (13, 0), (17, 0))
        row = self.summary(saturday)
        self.assertEqual(row["ot_week_off_seconds"], 7 * HOUR)
        self.assertEqual(row["week_off"], 1)

    def test_a_pending_request_shows_nothing_until_it_is_approved(self):
        saturday = self.saturday()
        pending = self.request_for(saturday, (9, 0), (12, 0), approved=False)
        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], 0)

        pending.approved = True
        pending.save()
        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], 3 * HOUR)

    def test_the_day_stays_a_week_off_and_regular_hours_stay_zero(self):
        saturday = self.saturday()
        self.request_for(saturday, (9, 0), (12, 0))
        row = self.summary(saturday)
        # Everything the weekend produced is overtime — none of it is
        # ordinary time, so regular must not go negative or absorb any of it.
        self.assertEqual(row["worked_seconds"], 3 * HOUR)
        self.assertEqual(row["regular_seconds"], 0)
        self.assertEqual(row["ot_regular_seconds"], 0)
        self.assertEqual(row["ot_holiday_seconds"], 0)


class ApprovalIdempotenceTests(WeekendOvertimeBaseTest):
    def test_approving_the_same_request_twice_changes_nothing(self):
        saturday = self.saturday()
        overtime = self.request_for(saturday, (9, 0), (12, 0))
        first = self.summary(saturday)["ot_week_off_seconds"]

        # Re-running the approval action, exactly as a double-clicked button
        # would. The total is derived, so there is nothing to accumulate.
        overtime.approved = True
        overtime.canceled = False
        overtime.save()
        overtime.save()

        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], first)
        self.assertEqual(first, 3 * HOUR)

    def test_rebuilding_the_summary_repeatedly_gives_the_same_answer(self):
        saturday = self.saturday()
        self.request_for(saturday, (9, 0), (12, 0))
        self.request_for(saturday, (13, 0), (17, 0))
        totals = {self.summary(saturday)["ot_week_off_seconds"] for _ in range(3)}
        self.assertEqual(totals, {7 * HOUR})


class CancellationTests(WeekendOvertimeBaseTest):
    def test_cancelling_one_of_two_recomputes_the_rest(self):
        saturday = self.saturday()
        morning = self.request_for(saturday, (9, 0), (12, 0))
        self.request_for(saturday, (13, 0), (17, 0))
        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], 7 * HOUR)

        morning.canceled = True
        morning.approved = False
        morning.save()

        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], 4 * HOUR)

    def test_cancelling_the_last_request_leaves_a_plain_week_off(self):
        saturday = self.saturday()
        only = self.request_for(saturday, (9, 0), (12, 0))
        only.canceled = True
        only.approved = False
        only.save()

        row = self.summary(saturday)
        self.assertEqual(row["ot_week_off_seconds"], 0)
        self.assertEqual(row["overtime_seconds"], 0)
        self.assertEqual(row["week_off"], 1)
        self.assertEqual(row["absent"], 0.0)
        self.assertEqual(row["present"], 0.0)

    def test_cancelling_never_touches_a_real_attendance_row(self):
        saturday = self.saturday()
        self.clock(saturday, (9, 0), (12, 0))
        attendance = Attendance.objects.get(
            employee_id=self.employee, attendance_date=saturday
        )
        overtime = self.request_for(saturday, (13, 0), (17, 0))

        overtime.canceled = True
        overtime.approved = False
        overtime.save()

        attendance.refresh_from_db()
        self.assertEqual(attendance.at_work_second, 3 * HOUR)
        self.assertTrue(
            Attendance.objects.filter(pk=attendance.pk).exists(),
            "a real, physically recorded attendance must survive a cancellation",
        )
        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], 3 * HOUR)


class RealAttendanceCollisionTests(WeekendOvertimeBaseTest):
    def test_a_request_and_real_attendance_are_never_summed(self):
        # Someone filed a request AND physically checked in on the same
        # Saturday. They worked one day, not two.
        saturday = self.saturday()
        self.clock(saturday, (9, 0), (12, 0))     # 3h real
        self.request_for(saturday, (9, 0), (12, 0))  # 3h approved

        row = self.summary(saturday)
        self.assertEqual(row["ot_week_off_seconds"], 3 * HOUR)
        self.assertNotEqual(row["ot_week_off_seconds"], 6 * HOUR)

    def test_the_larger_of_the_two_is_credited_so_real_work_is_never_hidden(self):
        saturday = self.saturday()
        self.clock(saturday, (9, 0), (17, 0))        # 7h real, lunch excluded
        self.request_for(saturday, (9, 0), (12, 0))  # only 3h approved

        # An approved request can add to what is visible; it must not erase
        # hours the employee is actually recorded as having worked.
        self.assertEqual(self.summary(saturday)["ot_week_off_seconds"], 7 * HOUR)

    def test_real_attendance_alone_is_unchanged_by_this_feature(self):
        saturday = self.saturday()
        self.clock(saturday, (9, 0), (12, 0))
        row = self.summary(saturday)
        self.assertEqual(row["ot_week_off_seconds"], 3 * HOUR)
        self.assertEqual(row["week_off"], 1)


class WeekdayIsolationTests(WeekendOvertimeBaseTest):
    def test_an_approved_weekday_request_adds_nothing_to_overtime(self):
        day = self.weekday()
        self.clock(day, (8, 0), (18, 0))   # 9h worked, 1h over the minimum
        before = self.summary(day)

        self.request_for(day, (17, 0), (18, 0))
        after = self.summary(day)

        self.assertEqual(after["ot_regular_seconds"], before["ot_regular_seconds"])
        self.assertEqual(after["overtime_seconds"], before["overtime_seconds"])
        self.assertEqual(after["overtime_seconds"], HOUR)
        self.assertEqual(after["ot_week_off_seconds"], 0)

    def test_a_weekday_request_on_a_day_with_no_attendance_stays_invisible(self):
        # A permission to work overtime is not a record of having worked.
        day = self.weekday()
        self.request_for(day, (17, 0), (18, 0))
        row = self.summary(day)
        self.assertEqual(row["overtime_seconds"], 0)
        self.assertEqual(row["ot_regular_seconds"], 0)
        self.assertEqual(row["present"], 0.0)

    def test_weekday_overtime_from_real_attendance_is_untouched(self):
        day = self.weekday()
        self.clock(day, (8, 0), (18, 30))
        row = self.summary(day)
        # 10h30 raw - 1h lunch = 9h30 worked; 1h30 beyond the 08:00 minimum.
        self.assertEqual(row["ot_regular_seconds"], HOUR + 1800)
        self.assertEqual(row["present"], 1.0)
        self.assertEqual(row["ot_week_off_seconds"], 0)


class HolidayPrecedenceTests(WeekendOvertimeBaseTest):
    def test_a_saturday_that_is_also_a_public_holiday_keeps_holiday_precedence(self):
        # Not a new rule: real attendance on such a date already counted as
        # holiday rather than week-off, and an approved request follows the
        # same existing precedence rather than inventing its own.
        saturday = self.saturday()
        Holidays.objects.create(
            name="Ngày lễ", start_date=saturday, end_date=saturday,
            is_specific=False, company_id=self.company,
        )
        self.request_for(saturday, (9, 0), (12, 0))

        row = self.summary(saturday)
        self.assertEqual(row["ot_holiday_seconds"], 3 * HOUR)
        self.assertEqual(row["ot_week_off_seconds"], 0)
        self.assertEqual(row["overtime_seconds"], 3 * HOUR)


class HistoricalSafetyTests(WeekendOvertimeBaseTest):
    def test_building_the_summary_writes_nothing(self):
        saturday = self.saturday()
        self.request_for(saturday, (9, 0), (12, 0))

        before_attendance = Attendance.objects.count()
        before_activity = AttendanceActivity.objects.count()
        self.summary(saturday - timedelta(days=30), saturday)
        self.summary(saturday)

        self.assertEqual(Attendance.objects.count(), before_attendance)
        self.assertEqual(AttendanceActivity.objects.count(), before_activity)

    def test_an_old_attendance_row_is_not_rewritten(self):
        old_day = self.weekday() - timedelta(days=45)
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=old_day,
            shift_id=self.shift,
            attendance_clock_in_date=old_day,
            attendance_clock_in=time(8, 0),
            attendance_clock_out_date=old_day,
            attendance_clock_out=time(17, 0),
            attendance_worked_hour="09:00",
            minimum_hour="08:00",
        )
        self.summary(old_day, self.weekday())
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_worked_hour, "09:00")


class OvertimeRequestAPIValidationTests(WeekendOvertimeBaseTest):
    """Creation-time duplicate/overlap rejection, through the real endpoint."""

    URL = "/api/attendance/overtime-requests/"

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.employee.employee_user_id)

    def post(self, day, start, end):
        return self.client.post(
            self.URL,
            {
                "request_date": day.isoformat(),
                "start_time": "%02d:%02d:00" % start,
                "end_time": "%02d:%02d:00" % end,
            },
            format="json",
        )

    def test_a_first_request_is_accepted(self):
        response = self.post(self.saturday(), (9, 0), (12, 0))
        self.assertEqual(response.status_code, 201, response.data)

    def test_an_exact_duplicate_is_rejected(self):
        saturday = self.saturday()
        self.assertEqual(self.post(saturday, (9, 0), (12, 0)).status_code, 201)
        second = self.post(saturday, (9, 0), (12, 0))
        self.assertEqual(second.status_code, 400)
        self.assertEqual(
            OvertimeRequest.objects.filter(employee_id=self.employee).count(), 1
        )

    def test_an_overlapping_request_is_rejected(self):
        saturday = self.saturday()
        self.post(saturday, (9, 0), (12, 0))
        self.assertEqual(self.post(saturday, (11, 0), (13, 0)).status_code, 400)
        self.assertEqual(self.post(saturday, (10, 0), (11, 0)).status_code, 400)
        self.assertEqual(self.post(saturday, (8, 0), (17, 0)).status_code, 400)

    def test_an_adjacent_request_is_allowed(self):
        saturday = self.saturday()
        self.assertEqual(self.post(saturday, (9, 0), (12, 0)).status_code, 201)
        # Touching at a single instant is two stretches, not a conflict.
        self.assertEqual(self.post(saturday, (12, 0), (13, 0)).status_code, 201)

    def test_a_cancelled_request_does_not_block_a_new_one(self):
        saturday = self.saturday()
        self.post(saturday, (9, 0), (12, 0))
        OvertimeRequest.objects.filter(employee_id=self.employee).update(canceled=True)
        self.assertEqual(self.post(saturday, (9, 0), (12, 0)).status_code, 201)

    def test_a_request_on_a_different_date_is_unaffected(self):
        saturday = self.saturday()
        self.post(saturday, (9, 0), (12, 0))
        other = saturday - timedelta(days=7)
        self.assertEqual(self.post(other, (9, 0), (12, 0)).status_code, 201)

    def test_end_before_start_is_still_rejected(self):
        self.assertEqual(self.post(self.saturday(), (12, 0), (9, 0)).status_code, 400)

    def test_another_employees_request_never_blocks_or_leaks(self):
        saturday = self.saturday()
        other = Employee.objects.create(
            employee_first_name="Other", employee_last_name="Emp",
            email="other-ot@test.local", phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=other)
        info.company_id = self.company
        info.shift_id = self.shift
        info.save()
        self.request_for(saturday, (9, 0), (12, 0), employee=other)

        # The same window is still free for this employee.
        self.assertEqual(self.post(saturday, (9, 0), (12, 0)).status_code, 201)

    def test_the_employee_is_taken_from_the_session_not_the_payload(self):
        saturday = self.saturday()
        other = Employee.objects.create(
            employee_first_name="Victim", employee_last_name="Emp",
            email="victim-ot@test.local", phone="9999999999",
        )
        response = self.client.post(
            self.URL,
            {
                "employee_id": other.pk,
                "request_date": saturday.isoformat(),
                "start_time": "09:00:00",
                "end_time": "12:00:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        created = OvertimeRequest.objects.get(pk=response.data["id"])
        self.assertEqual(created.employee_id_id, self.employee.pk)
        self.assertFalse(
            OvertimeRequest.objects.filter(employee_id=other).exists()
        )
