"""Correcting an attendance must correct its verdict with it.

Phase ATTENDANCE-STATUS-AND-ADMIN-DATA-HARDENING, section 2.

`late_come` and `early_out` only ever create a flag. That is right for a live
check-in, but every later path that changes a recorded time — the Admin edit
form, the two attendance-request approvals — left the old flag in place. An
arrival corrected from 08:30 to 08:00 kept its `late_come` row, so the employee
went on being shown "Đi muộn" for a check-in that is on time, and the timesheet
calendar painted the day orange with 08:00 printed next to it.

`sync_late_early` is the one function those paths now call. These tests hold
both halves of it: it removes a verdict that no longer holds, and it refuses to
touch a day that is not its to judge.
"""

import uuid
from datetime import date, datetime, time, timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from attendance.models import Attendance, AttendanceLateComeEarlyOut
from attendance.views.clock_in_out import sync_late_early
from base.models import (
    CheckInPolicy,
    Company,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    TrackLateComeEarlyOut,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation

RULE_IN_FORCE = override_settings(
    ATTENDANCE_LATE_EARLY_RULE_EFFECTIVE_DATE="2000-01-01"
)


class SyncBase(TestCase):
    """One employee on a Monday-to-Friday 08:00-17:00 shift, ten minutes grace."""

    @classmethod
    def setUpTestData(cls):
        tag = uuid.uuid4().hex[:8]
        cls.company = Company.objects.create(
            company="Sync Corp %s" % tag,
            hq=False,
            address="x",
            country="VN",
            state="HN",
            city="HN",
            zip="10000",
        )
        CheckInPolicy.objects.create(company_id=cls.company, late_threshold_minutes=10)
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca %s" % tag)
        cls.shift.company_id.add(cls.company)
        cls.work_type = WorkType.objects.create(work_type="Office %s" % tag)
        cls.work_type.company_id.add(cls.company)
        Department.objects.create(department="Eng %s" % tag).company_id.add(cls.company)

        for name in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            schedule = EmployeeShiftSchedule.objects.create(
                day=EmployeeShiftDay.objects.filter(day=name).first(),
                shift_id=cls.shift,
                minimum_working_hour="08:00",
                start_time=time(8, 0),
                end_time=time(17, 0),
            )
            schedule.company_id.add(cls.company)

        # The most recent Monday, so the day is in the past whenever this runs.
        cls.monday = timezone.localtime().date()
        while cls.monday.weekday() != 0:
            cls.monday -= timedelta(days=1)

    def setUp(self):
        tag = uuid.uuid4().hex[:10]
        self.employee = Employee.objects.create(
            employee_first_name="Sync",
            employee_last_name=tag,
            email="sync%s@test.local" % tag,
            phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        info.company_id = self.company
        info.shift_id = self.shift
        info.work_type_id = self.work_type
        info.save()

    # ---------------------------------------------------------------- helpers
    def record(self, check_in, check_out=None, day=None, employee=None):
        day = day or self.monday
        row = Attendance(
            employee_id=employee or self.employee,
            attendance_date=day,
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_day=EmployeeShiftDay.objects.filter(
                day=day.strftime("%A").lower()
            ).first(),
            attendance_clock_in=check_in,
            attendance_clock_in_date=day if check_in else None,
            attendance_clock_out=check_out,
            attendance_clock_out_date=day if check_out else None,
            minimum_hour="08:00",
        )
        row.save()
        return row

    def flags(self, attendance):
        return set(
            AttendanceLateComeEarlyOut.objects.entire()
            .filter(attendance_id=attendance)
            .values_list("type", flat=True)
        )

    def mark(self, attendance, flag_type):
        """A flag as a previous check-in would have left it."""
        row = AttendanceLateComeEarlyOut(attendance_id=attendance, type=flag_type)
        row.employee_id = attendance.employee_id
        row.save()
        return row


@RULE_IN_FORCE
class StaleVerdictTests(SyncBase):
    def test_correcting_a_late_arrival_to_an_on_time_one_clears_the_flag(self):
        row = self.record(time(8, 30), time(17, 0))
        self.mark(row, "late_come")
        self.assertEqual(self.flags(row), {"late_come"})

        row.attendance_clock_in = time(8, 0)
        row.save()
        sync_late_early(row)

        self.assertEqual(
            self.flags(row),
            set(),
            "08:00 is on time, so the day must stop reporting a late arrival",
        )

    def test_a_correction_that_is_still_late_keeps_exactly_one_flag(self):
        row = self.record(time(9, 0), time(17, 0))
        self.mark(row, "late_come")

        row.attendance_clock_in = time(8, 45)
        row.save()
        sync_late_early(row)

        self.assertEqual(self.flags(row), {"late_come"})
        self.assertEqual(
            AttendanceLateComeEarlyOut.objects.entire()
            .filter(attendance_id=row, type="late_come")
            .count(),
            1,
            "reconciling must not leave a second row behind",
        )

    def test_correcting_an_early_departure_clears_the_early_flag(self):
        row = self.record(time(8, 0), time(16, 0))
        self.mark(row, "early_out")

        row.attendance_clock_out = time(17, 0)
        row.save()
        sync_late_early(row)

        self.assertEqual(self.flags(row), set())

    def test_a_correction_can_also_introduce_a_flag_that_was_not_there(self):
        row = self.record(time(8, 0), time(17, 0))
        self.assertEqual(self.flags(row), set())

        row.attendance_clock_in = time(8, 40)
        row.save()
        sync_late_early(row)

        self.assertEqual(self.flags(row), {"late_come"})

    def test_removing_the_checkout_removes_the_early_verdict(self):
        row = self.record(time(8, 0), time(16, 0))
        self.mark(row, "early_out")

        row.attendance_clock_out = None
        row.attendance_clock_out_date = None
        row.save()
        sync_late_early(row)

        self.assertEqual(
            self.flags(row),
            set(),
            "with no departure recorded there is no departure to call early",
        )

    def test_moving_the_day_is_judged_against_the_new_days_schedule(self):
        """`attendance_day` goes stale; the date is the source of truth.

        `Attendance.save()` fills `attendance_day` only when it is empty, so a
        row moved to another date still names the weekday it was created on.
        Judging against that would compare the arrival with a different day's
        schedule, which is why the day is resolved from `attendance_date`.

        Wednesday is given an afternoon schedule here so the two answers differ:
        09:30 is late for Monday's 08:00 start and early for Wednesday's 13:00
        one. Reading the stale `attendance_day` would keep the late flag.
        """
        wednesday_schedule = EmployeeShiftSchedule.objects.entire().get(
            shift_id=self.shift, day__day="wednesday"
        )
        wednesday_schedule.start_time = time(13, 0)
        wednesday_schedule.end_time = time(22, 0)
        wednesday_schedule.save()
        wednesday = self.monday + timedelta(days=2)

        row = self.record(time(9, 30))
        self.mark(row, "late_come")
        self.assertEqual(row.attendance_day.day, "monday")

        row.attendance_date = wednesday  # attendance_day still says monday
        row.save()
        sync_late_early(row)

        self.assertEqual(row.attendance_day.day, "monday", "still stale, by design")
        self.assertEqual(
            self.flags(row),
            set(),
            "09:30 is before Wednesday's 13:00 start, so the moved day is not "
            "late — unless the stale weekday is what gets read",
        )


@RULE_IN_FORCE
class SyncRefusalTests(SyncBase):
    """What `sync_late_early` must leave exactly as it found it."""

    def test_a_day_on_an_unscheduled_weekday_is_left_alone(self):
        saturday = self.monday + timedelta(days=5)
        row = self.record(time(9, 0), time(12, 0), day=saturday)
        self.mark(row, "late_come")

        sync_late_early(row)

        self.assertEqual(
            self.flags(row),
            {"late_come"},
            "with no schedule there is no rule to re-derive from, so whatever "
            "is recorded stands rather than being quietly deleted",
        )

    def test_tracking_switched_off_changes_nothing(self):
        TrackLateComeEarlyOut.objects.create(is_enable=False)
        row = self.record(time(8, 0), time(17, 0))
        self.mark(row, "late_come")

        sync_late_early(row)

        self.assertEqual(self.flags(row), {"late_come"})

    def test_an_attendance_with_no_shift_is_left_alone(self):
        row = self.record(time(9, 0), time(17, 0))
        self.mark(row, "late_come")
        row.shift_id = None
        row.save()

        sync_late_early(row)

        self.assertEqual(self.flags(row), {"late_come"})


class HistoricalDayTests(SyncBase):
    """Section 6: the effective date decides, and history is not re-judged.

    No `RULE_IN_FORCE` override here on purpose — this class runs against the
    real boundary, so a day before it must come out untouched.
    """

    def test_a_day_before_the_effective_date_keeps_its_recorded_verdict(self):
        from attendance.methods.workday_rules import late_early_rule_effective_date

        historical = late_early_rule_effective_date() - timedelta(days=1)
        while historical.weekday() > 4:
            historical -= timedelta(days=1)

        row = self.record(time(8, 0), time(17, 0), day=historical)
        self.mark(row, "late_come")

        sync_late_early(row)

        self.assertEqual(
            self.flags(row),
            {"late_come"},
            "08:00 is on time under today's shift, but this day was recorded "
            "under the old one and is not this rule's to re-judge",
        )

    def test_a_day_on_the_effective_date_itself_is_reconciled(self):
        from attendance.methods.workday_rules import late_early_rule_effective_date

        boundary = late_early_rule_effective_date()
        while boundary.weekday() > 4:
            boundary += timedelta(days=1)

        row = self.record(time(8, 0), time(17, 0), day=boundary)
        self.mark(row, "late_come")

        sync_late_early(row)

        self.assertEqual(
            self.flags(row),
            set(),
            "the boundary day is governed by the rule, so an on-time arrival "
            "must not keep a late verdict",
        )
