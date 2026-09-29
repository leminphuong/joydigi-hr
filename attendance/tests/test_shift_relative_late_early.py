"""
Phase FUTURE-ATTENDANCE-RULE-AND-PUSH-SOUND.

Lateness and early departure are decided against the employee's own shift —
`start_time`/`end_time` from the schedule for that weekday, plus whatever
grace is configured — and not against a company-wide clock time. A shift
that begins at 13:00 cannot be judged by a morning boundary, and a 07:50
arrival at an 08:00 shift is early, not late.

These tests drive the real check-in and check-out paths, so what they hold is
the behaviour of a genuine clock event, not a helper in isolation. Every
expectation is derived from the fixture's own shift: no boundary is written
here that the shift does not imply.

Nothing in this phase touches history. These are about what gets recorded
from now on.
"""

import uuid
from datetime import datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from attendance.methods.utils import Request
from attendance.models import Attendance, AttendanceLateComeEarlyOut, GraceTime
from attendance.views.clock_in_out import early_out, perform_clock_in, perform_clock_out
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


class ShiftClockTestCase(TestCase):
    """Harness: one employee on one shift, driven through the real paths."""

    #: Overridden per subclass — the shift under test.
    shift_start = time(8, 0)
    shift_end = time(17, 0)
    grace_minutes = None
    grace_on_clock_out = False
    late_threshold_minutes = None

    @classmethod
    def setUpTestData(cls):
        tag = uuid.uuid4().hex[:8]
        cls.company = Company.objects.create(
            company="Shift Corp %s" % tag,
            hq=False,
            address="x",
            country="VN",
            state="HN",
            city="HN",
            zip="10000",
        )
        if cls.late_threshold_minutes is not None:
            CheckInPolicy.objects.create(
                company_id=cls.company,
                late_threshold_minutes=cls.late_threshold_minutes,
            )
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca %s" % tag)
        cls.shift.company_id.add(cls.company)

        cls.work_type = WorkType.objects.create(work_type="Office %s" % tag)
        cls.work_type.company_id.add(cls.company)
        Department.objects.create(department="Eng %s" % tag).company_id.add(
            cls.company
        )

        cls.today = timezone.localtime().date()
        while cls.today.weekday() > 4:  # keep it a weekday
            cls.today -= timedelta(days=1)
        cls.shift_day = EmployeeShiftDay.objects.filter(
            day=cls.today.strftime("%A").lower()
        ).first()
        schedule = EmployeeShiftSchedule.objects.create(
            day=cls.shift_day,
            shift_id=cls.shift,
            minimum_working_hour="08:00",
            start_time=cls.shift_start,
            end_time=cls.shift_end,
        )
        schedule.company_id.add(cls.company)

    def setUp(self):
        # Built here rather than in `setUpTestData`: creating it at class
        # level tripped SQLite's "database schema is locked" on this schema,
        # and a per-test row is rolled back with everything else anyway.
        if self.grace_minutes is not None:
            # HH:MM:SS, because `GraceTime.save()` splits it into three and
            # recomputes `allowed_time_in_secs` from the parts itself.
            grace = GraceTime.objects.create(
                allowed_time="00:%02d:00" % self.grace_minutes,
                allowed_time_in_secs=self.grace_minutes * 60,
                is_active=True,
                allowed_clock_in=True,
                allowed_clock_out=self.grace_on_clock_out,
            )
            self.assertEqual(grace.allowed_time_in_secs, self.grace_minutes * 60)
            self.shift.grace_time_id = grace
            self.shift.save()

        tag = uuid.uuid4().hex[:10]
        self.employee = Employee.objects.create(
            employee_first_name="Shift",
            employee_last_name=tag,
            email="shift%s@test.local" % tag,
            phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        info.company_id = self.company
        info.shift_id = self.shift
        info.work_type_id = self.work_type
        info.save()

    # ------------------------------------------------------------- helpers
    def at(self, hour, minute=0, second=0):
        return timezone.make_aware(
            datetime.combine(self.today, time(hour, minute, second))
        )

    def request_at(self, moment):
        user = type(self.employee.employee_user_id).objects.get(
            pk=self.employee.employee_user_id.pk
        )
        return Request(
            user=user,
            date=moment.date(),
            time=moment.time(),
            datetime=moment,
            trusted_device=True,
        )

    def check_in(self, moment):
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

    def day(self, check_in, check_out):
        """Work one whole day and return the flags it recorded."""
        row = self.check_in(check_in)
        self.check_out(check_out)
        return self.flags(row)


class OfficeShiftTests(ShiftClockTestCase):
    """A 09:00-18:00 shift, no grace anywhere — the rule at its plainest."""

    shift_start = time(9, 0)
    shift_end = time(18, 0)

    def test_a_arriving_before_the_shift_starts_is_not_late(self):
        self.assertNotIn("late_come", self.day(self.at(8, 45), self.at(18, 0)))

    def test_b_arriving_exactly_at_the_shift_start_is_not_late(self):
        self.assertNotIn("late_come", self.day(self.at(9, 0), self.at(18, 0)))

    def test_d_arriving_after_the_shift_start_is_late(self):
        self.assertIn("late_come", self.day(self.at(9, 1), self.at(18, 0)))

    def test_e_leaving_exactly_at_the_shift_end_is_not_early(self):
        self.assertNotIn("early_out", self.day(self.at(9, 0), self.at(18, 0)))

    def test_f_leaving_after_the_shift_end_is_not_early(self):
        self.assertNotIn("early_out", self.day(self.at(9, 0), self.at(18, 30)))

    def test_g_leaving_before_the_shift_end_is_early(self):
        self.assertIn("early_out", self.day(self.at(9, 0), self.at(17, 59)))

    def test_the_morning_boundary_of_another_shift_means_nothing_here(self):
        """
        08:31 was the company-wide "late" line before this phase. For a shift
        that has not begun yet it is simply an early arrival.
        """
        self.assertNotIn("late_come", self.day(self.at(8, 31), self.at(18, 0)))

    def test_the_old_afternoon_boundary_means_nothing_here_either(self):
        """16:30 was the old "not early" line; this shift ends at 18:00."""
        self.assertIn("early_out", self.day(self.at(9, 0), self.at(16, 30)))


class AfternoonShiftTests(ShiftClockTestCase):
    """H: a shift nowhere near 08:00-17:00 is judged by its own hours."""

    shift_start = time(13, 0)
    shift_end = time(22, 0)

    def test_arriving_before_an_afternoon_shift_is_not_late(self):
        self.assertNotIn("late_come", self.day(self.at(12, 50), self.at(22, 0)))

    def test_arriving_after_an_afternoon_shift_starts_is_late(self):
        self.assertIn("late_come", self.day(self.at(13, 5), self.at(22, 0)))

    def test_a_morning_arrival_is_never_late_for_an_afternoon_shift(self):
        # Under a fixed 08:31 rule this would have been recorded as late.
        self.assertNotIn("late_come", self.day(self.at(9, 0), self.at(22, 0)))

    def test_leaving_at_the_afternoon_shift_end_is_not_early(self):
        self.assertNotIn("early_out", self.day(self.at(13, 0), self.at(22, 0)))

    def test_leaving_in_the_afternoon_is_early_for_this_shift(self):
        # And under a fixed 16:30 rule this would have been recorded as fine.
        self.assertIn("early_out", self.day(self.at(13, 0), self.at(17, 0)))


class GraceTests(ShiftClockTestCase):
    """C/D: the shift's own grace, on both ends of the day."""

    shift_start = time(8, 0)
    shift_end = time(17, 0)
    grace_minutes = 15
    grace_on_clock_out = True

    def test_c_arriving_inside_the_grace_is_not_late(self):
        self.assertNotIn("late_come", self.day(self.at(8, 15), self.at(17, 0)))

    def test_d_arriving_past_the_grace_is_late(self):
        self.assertIn("late_come", self.day(self.at(8, 16), self.at(17, 0)))

    def test_leaving_inside_the_clock_out_grace_is_not_early(self):
        self.assertNotIn("early_out", self.day(self.at(8, 0), self.at(16, 45)))

    def test_leaving_before_the_clock_out_grace_is_early(self):
        self.assertIn("early_out", self.day(self.at(8, 0), self.at(16, 44)))

    def test_the_reported_shape_of_day_records_nothing(self):
        """
        The day that prompted all of this: in before the shift, out after it.
        Both answers are read from the shift, not asserted as constants.
        """
        flags = self.day(self.at(7, 50), self.at(17, 4))

        self.assertEqual(flags, set())

    def test_a_long_day_records_nothing_either(self):
        self.assertEqual(self.day(self.at(7, 30), self.at(18, 0)), set())


class NightShiftTests(TestCase):
    """
    I: a shift crossing midnight keeps its own branch.

    Driven at the function rather than through a whole simulated overnight
    clock event: what matters is that the day-shift comparison never reaches
    a night shift. A 21:00 departure is flagged by the night branch, and
    would not have been by either of the day rules — the old fixed 16:30 one
    or the new shift-relative one — so this fails loudly if the branch is
    ever skipped.
    """

    def setUp(self):
        tag = uuid.uuid4().hex[:8]
        self.company = Company.objects.create(
            company="Night Corp %s" % tag,
            hq=False,
            address="x",
            country="VN",
            state="HN",
            city="HN",
            zip="10000",
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Ca dem %s" % tag)
        self.shift.company_id.add(self.company)
        self.employee = Employee.objects.create(
            employee_first_name="Night",
            employee_last_name=tag,
            email="night%s@test.local" % tag,
            phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        info.company_id = self.company
        info.shift_id = self.shift
        info.save()

        self.work_date = timezone.localtime().date()
        self.shift_day = EmployeeShiftDay.objects.filter(
            day=self.work_date.strftime("%A").lower()
        ).first()
        schedule = EmployeeShiftSchedule.objects.create(
            day=self.shift_day,
            shift_id=self.shift,
            minimum_working_hour="08:00",
            start_time=time(22, 0),
            end_time=time(6, 0),
            is_night_shift=True,
        )
        schedule.company_id.add(self.company)

        # 22:00 -> 06:00 in seconds, as `shift_schedule_today` yields them.
        self.start_secs = 22 * 3600
        self.end_secs = 6 * 3600

    def attendance_with(self, clock_out):
        row = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=self.work_date,
            attendance_day=self.shift_day,
            shift_id=self.shift,
            attendance_clock_in_date=self.work_date,
            attendance_clock_in=time(22, 0),
            attendance_clock_out_date=self.work_date,
            attendance_clock_out=clock_out,
            minimum_hour="08:00",
        )
        return row

    def flags(self, attendance):
        return set(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=attendance
            ).values_list("type", flat=True)
        )

    def test_leaving_before_a_night_shift_ends_is_early(self):
        row = self.attendance_with(time(5, 0))

        early_out(
            attendance=row,
            start_time=self.start_secs,
            end_time=self.end_secs,
            shift=self.shift,
        )

        self.assertIn("early_out", self.flags(row))

    def test_working_a_night_shift_to_its_end_is_not_early(self):
        row = self.attendance_with(time(6, 30))

        early_out(
            attendance=row,
            start_time=self.start_secs,
            end_time=self.end_secs,
            shift=self.shift,
        )

        self.assertEqual(self.flags(row), set())

    def test_an_evening_departure_is_still_judged_by_the_night_branch(self):
        """
        21:00 — before the shift even starts. The night branch records it,
        and neither day rule ever would: the old one because 21:00 is past
        16:30, the new one because it is past the 06:00 end. Unchanged
        behaviour, pinned so the branch cannot be quietly bypassed.
        """
        row = self.attendance_with(time(21, 0))

        early_out(
            attendance=row,
            start_time=self.start_secs,
            end_time=self.end_secs,
            shift=self.shift,
        )

        self.assertIn("early_out", self.flags(row))
