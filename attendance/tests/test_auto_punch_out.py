"""Phase FIX A.1 — the existing Auto Check Out job, finally under test.

`attendance.scheduler.auto_punch_out` has been running on production
every five minutes, writing check-outs into the attendance table, with
no test of its own. This file pins the behaviour it has *now*, before
anything nearby changes it, so that FIX A's date-scoped selection and
FIX A.1's separate reconciliation job can be shown not to have altered
it.

Nothing here changes the setting's meaning. `is_auto_punch_out_enabled`
still means "close this shift's days at the time an administrator
nominated", and the tests assert exactly that — including that switching
it off makes this job do nothing at all, which is what makes the
separate forgotten-session job necessary rather than redundant.
"""

from datetime import datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from attendance.models import Attendance, AttendanceActivity
from attendance.scheduler import auto_punch_out
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user


class AutoPunchOutBase(TestCase):
    NIGHT = False
    #: 17:30 for a day shift ending 17:00 — the form requires the
    #: automatic time to be at or after the shift end.
    AUTO_TIME = time(17, 30)
    START = time(8, 0)
    END = time(17, 0)

    def setUp(self):
        self.company = make_company("Auto Co")
        self.user = make_user("autouser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="auto@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Ca kiểm thử")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )

        self.day2 = timezone.localdate()
        self.day1 = self.day2 - timedelta(days=1)
        for on in (self.day1, self.day2):
            EmployeeShiftSchedule.objects.update_or_create(
                shift_id=self.shift,
                day=EmployeeShiftDay.objects.get(day=on.strftime("%A").lower()),
                defaults={
                    "is_night_shift": self.NIGHT,
                    "minimum_working_hour": "08:00",
                    "start_time": self.START,
                    "end_time": self.END,
                },
            )

    def enable_auto(self, at=None):
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_auto_punch_out_enabled=True, auto_punch_out_time=at or self.AUTO_TIME
        )

    def day_of(self, on):
        return EmployeeShiftDay.objects.get(day=on.strftime("%A").lower())

    def open_session(self, on, clock_in=None):
        clock_in = clock_in or self.START
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=on,
            attendance_day=self.day_of(on),
            shift_id=self.shift,
            attendance_clock_in=clock_in,
            attendance_clock_in_date=on,
            minimum_hour="08:00",
        )
        activity = AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=on,
            clock_in_date=on,
            shift_day=self.day_of(on),
            clock_in=clock_in,
            in_datetime=timezone.make_aware(datetime.combine(on, clock_in)),
        )
        return attendance, activity

    def snapshot(self, *rows):
        result = []
        for row in rows:
            row.refresh_from_db()
            if isinstance(row, Attendance):
                result.append(
                    (
                        row.attendance_clock_out,
                        row.attendance_clock_out_date,
                        row.attendance_worked_hour,
                        row.attendance_overtime,
                    )
                )
            else:
                result.append((row.clock_out, row.clock_out_date, row.out_datetime))
        return tuple(result)


class EnabledTests(AutoPunchOutBase):
    """A: enabled, day shift — closes at the nominated time."""

    def test_it_closes_the_session_at_the_configured_auto_time(self):
        self.enable_auto()
        attendance, activity = self.open_session(self.day1)

        auto_punch_out()

        attendance.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, self.AUTO_TIME)
        self.assertEqual(attendance.attendance_clock_out_date, self.day1)
        self.assertIsNotNone(activity.clock_out)
        self.assertEqual(activity.clock_out_date, self.day1)

    def test_it_writes_both_columns_never_only_one(self):
        self.enable_auto()
        attendance, _activity = self.open_session(self.day1)
        auto_punch_out()
        attendance.refresh_from_db()
        # A row with one column set and the other empty is the malformed
        # state the rest of the system cannot reason about.
        self.assertIsNotNone(attendance.attendance_clock_out)
        self.assertIsNotNone(attendance.attendance_clock_out_date)

    def test_it_uses_the_nominated_time_not_the_shift_end(self):
        # The distinction between this feature and forgotten-session
        # finalization: Auto Check Out honours the administrator's time.
        self.enable_auto(at=time(18, 45))
        attendance, _activity = self.open_session(self.day1)
        auto_punch_out()
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, time(18, 45))
        self.assertNotEqual(attendance.attendance_clock_out, self.END)

    def test_it_does_not_act_before_the_nominated_time_has_passed(self):
        # Today's session, with an automatic time still in the future.
        future = (timezone.localtime() + timedelta(hours=2)).time()
        self.enable_auto(at=future)
        attendance, activity = self.open_session(self.day2)
        before = self.snapshot(attendance, activity)

        auto_punch_out()

        self.assertEqual(self.snapshot(attendance, activity), before)


class DisabledTests(AutoPunchOutBase):
    """B: disabled — this job does nothing, which is the whole point."""

    def test_a_forgotten_session_is_left_completely_alone(self):
        attendance, activity = self.open_session(self.day1)
        before = self.snapshot(attendance, activity)

        auto_punch_out()

        self.assertEqual(self.snapshot(attendance, activity), before)
        attendance.refresh_from_db()
        self.assertIsNone(attendance.attendance_clock_out)
        self.assertIsNone(attendance.attendance_clock_out_date)

    def test_disabled_is_the_default_for_a_new_schedule(self):
        for schedule in EmployeeShiftSchedule.objects.filter(shift_id=self.shift):
            self.assertFalse(schedule.is_auto_punch_out_enabled)
            self.assertIsNone(schedule.auto_punch_out_time)


class NightShiftTests(AutoPunchOutBase):
    """C: enabled night shift — closes on the following calendar day."""

    NIGHT = True
    START = time(22, 0)
    END = time(6, 0)
    AUTO_TIME = time(6, 30)

    def test_it_closes_on_the_next_day_for_a_shift_crossing_midnight(self):
        self.enable_auto()
        attendance, activity = self.open_session(self.day1, clock_in=time(22, 0))

        auto_punch_out()

        attendance.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, self.AUTO_TIME)
        # The session belongs to the night it began…
        self.assertEqual(attendance.attendance_date, self.day1)
        # …and the check-out lands on the morning it ended.
        self.assertEqual(attendance.attendance_clock_out_date, self.day2)
        self.assertEqual(activity.clock_out_date, self.day2)


class PairingTests(AutoPunchOutBase):
    """D, E: the right pair, or nothing."""

    def test_it_closes_the_pair_belonging_to_one_session(self):
        self.enable_auto()
        older, older_activity = self.open_session(self.day1 - timedelta(days=1))
        target, target_activity = self.open_session(self.day1)

        auto_punch_out()

        target.refresh_from_db()
        target_activity.refresh_from_db()
        self.assertEqual(target.attendance_clock_out_date, self.day1)
        self.assertEqual(target_activity.attendance_date, target.attendance_date)

        # The older session is a different day and gets its own closure,
        # dated to itself — never closed against `target`'s date.
        older.refresh_from_db()
        older_activity.refresh_from_db()
        if older.attendance_clock_out_date is not None:
            self.assertEqual(older.attendance_clock_out_date, older.attendance_date)
            self.assertEqual(
                older_activity.attendance_date, older.attendance_date
            )

    def test_a_malformed_row_is_not_written_to(self):
        self.enable_auto()
        attendance, activity = self.open_session(self.day1)
        # One column set, the other empty: the job's own queryset requires
        # both to be null, so it must not be a candidate at all.
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        before = self.snapshot(attendance, activity)

        auto_punch_out()

        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_a_session_with_no_open_activity_is_not_closed(self):
        self.enable_auto()
        attendance, activity = self.open_session(self.day1)
        AttendanceActivity.objects.filter(pk=activity.pk).update(
            clock_out=time(17, 0), clock_out_date=self.day1
        )
        before = self.snapshot(attendance)

        auto_punch_out()

        self.assertEqual(self.snapshot(attendance), before)

    def test_it_never_raises_on_unreasonable_data(self):
        # The job wraps each attempt; a broken row must not stop the loop.
        self.enable_auto()
        attendance, _activity = self.open_session(self.day1)
        Attendance.objects.filter(pk=attendance.pk).update(attendance_day=None)
        auto_punch_out()  # must not raise
