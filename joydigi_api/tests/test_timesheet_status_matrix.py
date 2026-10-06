"""The timesheet's own verdict for a day, end to end.

Phase ATTENDANCE-STATUS-AND-ADMIN-DATA-HARDENING, section 14. Each case drives
the real rule — a stored `Attendance` row reconciled by `sync_late_early`, the
same function the Admin edit form and the request approvals call — and then
reads `GET /api/attendance/timesheet/`, so a passing test means the rule, the
stored flags and what the app is told all agree.

The shift under test is the company standard week: Monday to Friday
08:00-17:00, no schedule at the weekend, ten minutes of check-in grace from
`CheckInPolicy` and no clock-out grace. That makes the boundaries 08:10 for a
late arrival and 17:00 for an early departure, and both are asserted as literal
clock times because they are what the business asked for.

Dates are chosen relative to today rather than written down, so the suite does
not start failing on a particular calendar day, and the late/early effective
date is overridden so the rule governs the month under test whenever it runs.
"""

from datetime import date, time, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceLateComeEarlyOut
from attendance.views.clock_in_out import sync_late_early
from base.models import (
    CheckInPolicy,
    CompanyLeaves,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    Holidays,
    WorkType,
)
from joydigi.testkit import make_company, make_employee, make_user
from leave.models import LeaveRequest, LeaveType

WORKING_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


@override_settings(ATTENDANCE_LATE_EARLY_RULE_EFFECTIVE_DATE="2000-01-01")
class TimesheetStatusMatrixTests(TestCase):
    """One employee, the standard week, one month entirely in the past."""

    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Matrix Co")
        self.user = make_user("matrix_user", password="secret123")
        self.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        self.shift.company_id.add(self.company)
        self.work_type = WorkType.objects.create(work_type="Office")
        self.work_type.company_id.add(self.company)

        # Monday-Friday only. The absence of a Saturday/Sunday row is what makes
        # the weekend a non-working day, so it is configured by omission here
        # exactly as it is in production.
        for name in WORKING_DAYS:
            day = EmployeeShiftDay.objects.get(day=name)
            schedule = EmployeeShiftSchedule.objects.create(
                day=day,
                shift_id=self.shift,
                minimum_working_hour="08:00",
                start_time=time(8, 0),
                end_time=time(17, 0),
            )
            schedule.company_id.add(self.company)

        CheckInPolicy.objects.create(
            company_id=self.company, late_threshold_minutes=10
        )

        self.employee = make_employee(
            company=self.company,
            email="matrix_user@test.joydigi",
            user=self.user,
            shift=self.shift,
            work_type=self.work_type,
        )
        # Re-read the account. `Employee.save()` populates the reverse
        # `employee_get` cache on this very user object, so the view would
        # otherwise be handed the Employee instance as it looked before
        # `make_employee` attached the shift — and report a month of days off.
        # A real request loads the user from the database, which is what this
        # restores.
        self.user = type(self.user).objects.get(pk=self.user.pk)
        self.client.force_authenticate(user=self.user)

        # A whole month in the past, so "has this day finished" is never a
        # question of what time the suite happens to run at.
        today = timezone.localdate()
        first_of_this_month = today.replace(day=1)
        self.month_end = first_of_this_month - timedelta(days=1)
        self.month_start = self.month_end.replace(day=1)
        self.year = self.month_start.year
        self.month = self.month_start.month

    # ------------------------------------------------------------- helpers
    def a_day(self, weekday):
        """The first date in the month under test falling on `weekday` (0=Mon)."""
        current = self.month_start
        while current <= self.month_end:
            if current.weekday() == weekday:
                return current
            current += timedelta(days=1)
        raise AssertionError("no %s in the month under test" % weekday)

    def record(self, day, check_in=None, check_out=None, validated=False):
        """Store a day and let the real rule decide its late/early flags."""
        row = Attendance(
            employee_id=self.employee,
            attendance_date=day,
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_day=EmployeeShiftDay.objects.get(
                day=day.strftime("%A").lower()
            ),
            attendance_clock_in=check_in,
            attendance_clock_in_date=day if check_in else None,
            attendance_clock_out=check_out,
            attendance_clock_out_date=day if check_out else None,
            minimum_hour="08:00",
            attendance_validated=validated,
        )
        row.save()
        sync_late_early(row)
        return row

    def payload(self):
        response = self.client.get(
            "/api/attendance/timesheet/",
            {"year": self.year, "month": self.month},
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def day_of(self, data, day):
        for entry in data["days"]:
            if entry["date"] == day.isoformat():
                return entry
        raise AssertionError("%s missing from the month" % day)

    def verdict(self, day):
        return self.day_of(self.payload(), day)

    # ------------------------------------------- A-F: the late/early boundary
    def test_a_0759_to_1700_is_a_complete_on_time_day(self):
        day = self.a_day(0)
        self.record(day, time(7, 59), time(17, 0))
        entry = self.verdict(day)
        self.assertFalse(entry["isLate"], "arriving before the shift is not late")
        self.assertFalse(entry["isEarly"])
        self.assertTrue(entry["isComplete"])
        self.assertEqual(entry["status"], "complete")

    def test_b_0800_to_1700_is_a_complete_on_time_day(self):
        day = self.a_day(1)
        self.record(day, time(8, 0), time(17, 0))
        entry = self.verdict(day)
        self.assertFalse(entry["isLate"], "arriving exactly at the start is not late")
        self.assertFalse(entry["isEarly"])
        self.assertEqual(entry["status"], "complete")

    def test_c_0805_is_inside_the_ten_minute_grace(self):
        day = self.a_day(2)
        self.record(day, time(8, 5), time(17, 0))
        entry = self.verdict(day)
        self.assertFalse(entry["isLate"])
        self.assertEqual(entry["status"], "complete")

    def test_the_last_on_time_minute_is_0810_and_the_first_late_second_is_after_it(self):
        """The grace boundary itself, stated as clock times."""
        on_time = self.a_day(3)
        self.record(on_time, time(8, 10), time(17, 0))
        self.assertFalse(self.verdict(on_time)["isLate"], "08:10 is still on time")

        late = self.a_day(4)
        self.record(late, time(8, 10, 1), time(17, 0))
        self.assertTrue(
            self.verdict(late)["isLate"],
            "08:10:01 is past the grace — truncating the seconds used to round "
            "this down to 08:10 and call it on time",
        )

    def test_d_0811_is_late_and_paints_the_day_late(self):
        day = self.a_day(0)
        self.record(day, time(8, 11), time(17, 0))
        entry = self.verdict(day)
        self.assertTrue(entry["isLate"])
        self.assertTrue(entry["isComplete"], "a late day is still a finished day")
        self.assertEqual(
            entry["status"],
            "late_early",
            "late must win over complete, or the day paints green",
        )

    def test_0825_is_late(self):
        day = self.a_day(1)
        self.record(day, time(8, 25), time(17, 0))
        self.assertTrue(self.verdict(day)["isLate"])

    def test_e_leaving_at_1650_is_early_and_wins_over_complete(self):
        day = self.a_day(2)
        self.record(day, time(8, 0), time(16, 50))
        entry = self.verdict(day)
        self.assertTrue(entry["isEarly"])
        self.assertFalse(entry["isLate"])
        self.assertTrue(entry["isComplete"])
        self.assertEqual(entry["status"], "late_early")

    def test_1659_is_early_but_1700_and_1701_and_1730_are_not(self):
        early = self.a_day(3)
        self.record(early, time(8, 0), time(16, 59))
        self.assertTrue(self.verdict(early)["isEarly"], "16:59 is before 17:00")

        # The three not-early cases, each on a Friday of its own so the rows
        # cannot interfere with one another.
        fridays = [
            self.a_day(4) + timedelta(days=7 * week)
            for week in range(3)
            if self.a_day(4) + timedelta(days=7 * week) <= self.month_end
        ]
        self.assertGreaterEqual(len(fridays), 3, "need three Fridays in the month")
        for day, clock in zip(fridays, (time(17, 0), time(17, 1), time(17, 30))):
            with self.subTest(clock=clock):
                self.record(day, time(8, 0), clock)
                self.assertFalse(
                    self.verdict(day)["isEarly"],
                    "%s is at or after the shift end" % clock,
                )

    def test_f_late_and_early_on_the_same_day_is_one_flagged_day(self):
        day = self.a_day(0)
        self.record(day, time(8, 11), time(16, 50))
        entry = self.verdict(day)
        self.assertTrue(entry["isLate"])
        self.assertTrue(entry["isEarly"])
        self.assertEqual(entry["status"], "late_early")

    # ------------------------------------------------------ G: in progress
    def test_g_checked_in_with_no_checkout_is_in_progress_not_complete(self):
        day = self.a_day(1)
        self.record(day, time(8, 0), None)
        entry = self.verdict(day)
        self.assertFalse(
            entry["isComplete"],
            "a day with no check-out is not a full day, however long ago it "
            "started",
        )
        self.assertTrue(entry["isInProgress"])
        self.assertEqual(entry["status"], "in_progress")
        self.assertFalse(entry["isAbsent"], "someone who checked in is not absent")

    def test_a_validated_day_without_a_checkout_is_complete_because_an_admin_said_so(
        self,
    ):
        """Manual validation stays meaningful — it is an explicit decision.

        `attendance_validated` is set by the Admin validate action, which
        notifies the employee that the day was accepted. Treating it as a
        finished day is therefore right. What it must not do is make a row with
        nothing in it count, which the next test pins.
        """
        day = self.a_day(2)
        self.record(day, time(8, 0), None, validated=True)
        entry = self.verdict(day)
        self.assertTrue(entry["isComplete"])
        self.assertEqual(entry["status"], "complete")

    def test_a_validated_row_with_no_check_in_at_all_is_not_a_complete_day(self):
        day = self.a_day(3)
        self.record(day, None, None, validated=True)
        entry = self.verdict(day)
        self.assertFalse(
            entry["isComplete"],
            "there is nothing recorded in this day to call complete",
        )

    # ----------------------------------------------------------- H: weekend
    def test_h_the_weekend_is_off_not_absent_and_never_late(self):
        for weekday in (5, 6):
            day = self.a_day(weekday)
            with self.subTest(day=day):
                entry = self.verdict(day)
                self.assertFalse(entry["isWorkingDay"])
                self.assertFalse(entry["isAbsent"], "nobody is absent on a day off")
                self.assertFalse(entry["isLate"])
                self.assertEqual(entry["status"], "off")

    def test_working_a_saturday_records_no_late_arrival(self):
        """The regression the production shift change exposed.

        With no Saturday schedule row, `shift_schedule_today` reports 0/0, and
        the late rule read that as a shift starting at 00:00 — so every weekend
        check-in was marked late.
        """
        saturday = self.a_day(5)
        row = self.record(saturday, time(9, 30), time(12, 0))
        self.assertEqual(
            list(
                AttendanceLateComeEarlyOut.objects.filter(
                    attendance_id=row
                ).values_list("type", flat=True)
            ),
            [],
            "a day the shift does not schedule has no start time to be late for",
        )
        self.assertFalse(self.verdict(saturday)["isLate"])

    # ------------------------------------------------------------- I: leave
    def test_i_an_approved_leave_day_reads_as_leave(self):
        day = self.a_day(0)
        leave_type = LeaveType.objects.create(name="Annual", payment="paid")
        LeaveRequest.objects.create(
            employee_id=self.employee,
            leave_type_id=leave_type,
            start_date=day,
            end_date=day,
            requested_days=1,
            status="approved",
        )
        entry = self.verdict(day)
        self.assertTrue(entry["isLeave"])
        self.assertEqual(entry["status"], "leave")
        self.assertFalse(entry["isAbsent"], "approved leave is not an absence")

    def test_a_holiday_is_off_and_not_an_absence(self):
        day = self.a_day(1)
        Holidays.objects.create(name="Lễ", start_date=day, end_date=day)
        entry = self.verdict(day)
        self.assertTrue(entry["isHoliday"])
        self.assertFalse(entry["isWorkingDay"])
        self.assertFalse(entry["isAbsent"])
        self.assertEqual(entry["status"], "off")

    # ------------------------------------------------------------ J: future
    def test_j_a_future_working_day_is_not_an_absence(self):
        today = timezone.localdate()
        response = self.client.get(
            "/api/attendance/timesheet/",
            {"year": today.year, "month": today.month},
        )
        self.assertEqual(response.status_code, 200)
        future = [
            entry
            for entry in response.data["days"]
            if date.fromisoformat(entry["date"]) > today
        ]
        self.assertTrue(future, "pick a month with a future day in it")
        for entry in future:
            with self.subTest(date=entry["date"]):
                self.assertFalse(
                    entry["isAbsent"],
                    "a day that has not happened cannot be a missed day",
                )

    def test_today_is_never_reported_as_an_absence(self):
        today = timezone.localdate()
        response = self.client.get(
            "/api/attendance/timesheet/",
            {"year": today.year, "month": today.month},
        )
        entry = [e for e in response.data["days"] if e["date"] == today.isoformat()][0]
        self.assertFalse(entry["isAbsent"], "today is still in progress")

    # --------------------------------------------------- absence, when real
    def test_a_past_working_day_with_nothing_recorded_is_an_absence(self):
        day = self.a_day(4)
        entry = self.verdict(day)
        self.assertTrue(entry["isWorkingDay"])
        self.assertTrue(entry["isAbsent"])
        self.assertEqual(entry["status"], "absent")

    # ------------------------------------------------- summary consistency
    def test_every_summary_counter_equals_the_days_it_claims_to_count(self):
        """The invariant that keeps the KPI row honest.

        Each counter is recomputed here from the day entries the same response
        carries, so a summary can never drift away from the calendar beneath it.
        """
        self.record(self.a_day(0), time(8, 0), time(17, 0))  # on time
        self.record(self.a_day(1), time(8, 30), time(17, 0))  # late
        self.record(self.a_day(2), time(8, 0), time(16, 0))  # early
        self.record(self.a_day(3), time(8, 0), None)  # in progress

        data = self.payload()
        days, summary = data["days"], data["summary"]

        self.assertEqual(summary["presentDays"], sum(1 for d in days if d["checkIn"] or d["workedHour"]))
        self.assertEqual(summary["lateCount"], sum(1 for d in days if d["isLate"]))
        self.assertEqual(summary["earlyCount"], sum(1 for d in days if d["isEarly"]))
        self.assertEqual(summary["completeDays"], sum(1 for d in days if d["isComplete"]))
        self.assertEqual(summary["absentDays"], sum(1 for d in days if d["isAbsent"]))
        self.assertEqual(
            summary["inProgressDays"], sum(1 for d in days if d["isInProgress"])
        )
        self.assertEqual(
            summary["onTimeDays"], sum(1 for d in days if d["status"] == "complete")
        )
        self.assertEqual(
            summary["workingDays"], sum(1 for d in days if d["isWorkingDay"])
        )

    def test_a_late_day_counts_as_a_day_with_attendance_but_not_as_an_on_time_one(self):
        """"Ngày công" and "Đủ công" are different numbers, and both are sent."""
        self.record(self.a_day(0), time(8, 30), time(17, 0))
        summary = self.payload()["summary"]
        self.assertEqual(summary["presentDays"], 1, "the day was worked")
        self.assertEqual(summary["lateCount"], 1)
        self.assertEqual(summary["completeDays"], 1, "and it was finished")
        self.assertEqual(
            summary["onTimeDays"], 0, "but it is not one of the on-time days"
        )

    def test_the_weekend_is_not_counted_as_a_working_day(self):
        summary = self.payload()["summary"]
        weekdays_in_month = sum(
            1
            for offset in range((self.month_end - self.month_start).days + 1)
            if (self.month_start + timedelta(days=offset)).weekday() < 5
        )
        self.assertEqual(summary["workingDays"], weekdays_in_month)


@override_settings(ATTENDANCE_LATE_EARLY_RULE_EFFECTIVE_DATE="2000-01-01")
class WeeklyOffDayScopeTests(TestCase):
    """A weekly off day belongs to the company that declared it."""

    def setUp(self):
        self.client = APIClient()
        self.ours = make_company("Ours")
        self.theirs = make_company("Theirs")
        self.user = make_user("scope_user", password="secret123")
        self.shift = EmployeeShift.objects.create(employee_shift="Ca")
        self.shift.company_id.add(self.ours)
        for name in WORKING_DAYS + ("saturday", "sunday"):
            day = EmployeeShiftDay.objects.get(day=name)
            EmployeeShiftSchedule.objects.create(
                day=day,
                shift_id=self.shift,
                minimum_working_hour="08:00",
                start_time=time(8, 0),
                end_time=time(17, 0),
            ).company_id.add(self.ours)
        self.employee = make_employee(
            company=self.ours,
            email="scope_user@test.joydigi",
            user=self.user,
            shift=self.shift,
        )
        self.user = type(self.user).objects.get(pk=self.user.pk)
        self.client.force_authenticate(user=self.user)
        today = timezone.localdate()
        self.month_end = today.replace(day=1) - timedelta(days=1)
        self.month_start = self.month_end.replace(day=1)

    def saturday(self):
        current = self.month_start
        while current.weekday() != 5:
            current += timedelta(days=1)
        return current

    def entry_for(self, day):
        response = self.client.get(
            "/api/attendance/timesheet/",
            {"year": self.month_start.year, "month": self.month_start.month},
        )
        self.assertEqual(response.status_code, 200)
        return [e for e in response.data["days"] if e["date"] == day.isoformat()][0]

    def test_another_companys_weekly_off_day_is_not_ours(self):
        rule = CompanyLeaves.objects.create(based_on_week=None, based_on_week_day="5")
        rule.company_id.add(self.theirs)
        entry = self.entry_for(self.saturday())
        self.assertFalse(
            entry["isCompanyLeave"],
            "a weekly off day attached to another company is not this "
            "employee's day off",
        )

    def test_our_own_weekly_off_day_is_ours(self):
        rule = CompanyLeaves.objects.create(based_on_week=None, based_on_week_day="5")
        rule.company_id.add(self.ours)
        self.assertTrue(self.entry_for(self.saturday())["isCompanyLeave"])

    def test_a_rule_attached_to_nobody_applies_everywhere(self):
        CompanyLeaves.objects.create(based_on_week=None, based_on_week_day="5")
        self.assertTrue(
            self.entry_for(self.saturday())["isCompanyLeave"],
            "a rule with no company is a tenant-wide rule, as the reminder "
            "engine also reads it",
        )
