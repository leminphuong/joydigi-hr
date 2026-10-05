"""The company's standard working week, as the seed establishes it.

Phase COMPANY-STANDARD-SHIFT-0800-1700. The default shift used to be
08:30-17:30 with a schedule row on all seven days, and that one piece of
configuration produced three separate complaints at once:

* check-in reminders arrived "around 08:30", because the engine is
  shift-relative and correctly fired at start-5m and start+5m — 08:25 and 08:35;
* reminders arrived on Saturday and Sunday, because a shift with a weekend
  schedule row is a shift that works weekends;
* a day with a 08:25 arrival and a 17:16 departure showed orange with
  `Đi muộn = 0`, because against an 08:30-17:30 shift that arrival is on time
  and that departure is early.

Nothing in the reminder engine, the late/early rule or the calendar needed
changing — they all read the shift. These tests pin the shift, and the four
reminder moments and both attendance verdicts that follow from it, so the
configuration cannot drift back without a failure.

The moments are asserted as clock times AND rederived from the shift, so the
test states the company rule (07:55 / 08:05 / 16:55 / 17:05) without hardcoding
it as the engine's input.
"""

from datetime import date, datetime, time, timedelta

from django.test import TestCase
from django.utils import timezone

from attendance.methods.reminders import (
    REMINDER_GRACE,
    REMINDER_LEAD,
    STAGE_START_MINUS_5,
    STAGE_START_PLUS_5,
    stage_due,
)
from attendance.models import Attendance, AttendanceLateComeEarlyOut, GraceTime
from base.demo_data.modules.checkin import (
    STANDARD_SHIFT_END,
    STANDARD_SHIFT_START,
    STANDARD_WORKING_DAYS,
    WEEKEND_WEEK_DAYS,
    seed_joydigi_checkin_demo,
)
from base.models import (
    CheckInPolicy,
    Company,
    CompanyLeaves,
    EmployeeShift,
    EmployeeShiftSchedule,
)
from employee.models import Employee

#: A Monday, so the weekday names below are unambiguous.
SEED_DAY = date(2026, 8, 20)


class StandardShiftSeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_joydigi_checkin_demo(today=SEED_DAY)
        cls.company = Company.objects.get(company="JOYDIGI")
        cls.shift = EmployeeShift.objects.entire().get(employee_shift="Ca hành chính")

    def schedule(self, weekday):
        return (
            EmployeeShiftSchedule.objects.entire()
            .filter(shift_id=self.shift, day__day=weekday)
            .first()
        )

    # ---------------------------------------------------- the five working days

    def test_monday_to_friday_run_eight_to_five(self):
        for weekday in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            with self.subTest(weekday=weekday):
                row = self.schedule(weekday)
                self.assertIsNotNone(row, "no schedule for %s" % weekday)
                self.assertEqual(row.start_time, time(8, 0))
                self.assertEqual(row.end_time, time(17, 0))
                self.assertFalse(row.is_night_shift)

    def test_the_constants_and_the_rows_cannot_disagree(self):
        self.assertEqual(STANDARD_SHIFT_START, time(8, 0))
        self.assertEqual(STANDARD_SHIFT_END, time(17, 0))
        self.assertEqual(
            tuple(STANDARD_WORKING_DAYS),
            ("monday", "tuesday", "wednesday", "thursday", "friday"),
        )

    # ------------------------------------------------------------- the weekend

    def test_saturday_and_sunday_have_no_schedule_at_all(self):
        for weekday in ("saturday", "sunday"):
            with self.subTest(weekday=weekday):
                self.assertIsNone(
                    self.schedule(weekday),
                    "a weekend schedule row is what makes the weekend a working "
                    "day, whatever else is configured",
                )

    def test_the_weekday_reference_rows_still_exist_for_every_day(self):
        # The schedule is gone; the day lookup table must not be, because every
        # date resolution goes through it.
        from base.models import EmployeeShiftDay

        for weekday in (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ):
            with self.subTest(weekday=weekday):
                self.assertTrue(
                    EmployeeShiftDay.objects.entire().filter(day=weekday).exists()
                )

    def test_the_weekend_is_declared_as_a_company_off_day(self):
        """The calendar and the working-day denominator read this, not the shift."""
        for weekday in WEEKEND_WEEK_DAYS:
            with self.subTest(weekday=weekday):
                rows = CompanyLeaves.objects.entire().filter(
                    based_on_week_day=weekday, based_on_week=None
                )
                self.assertTrue(rows.exists(), "weekday %s not declared off" % weekday)
                self.assertIn(
                    self.company.pk,
                    list(rows.first().company_id.values_list("pk", flat=True)),
                )

    def test_the_weekend_days_are_saturday_and_sunday(self):
        # `base.models.WEEK_DAYS` is Monday=0 … Sunday=6, the same as
        # `date.weekday()`, so 5 and 6 are Saturday and Sunday.
        self.assertEqual(tuple(WEEKEND_WEEK_DAYS), ("5", "6"))
        self.assertEqual(date(2026, 10, 3).weekday(), 5)
        self.assertEqual(date(2026, 10, 4).weekday(), 6)

    # -------------------------------------------------------- reminder moments

    def test_the_four_reminder_moments_are_0755_0805_1655_1705(self):
        """The company rule, stated as clock times and derived from the shift.

        Both halves matter: the literals are what the company asked for, and the
        derivation is what proves the engine is still shift-relative rather than
        carrying four hardcoded times of its own.
        """
        day = date(2026, 10, 5)  # a Monday
        start = timezone.make_aware(datetime.combine(day, STANDARD_SHIFT_START))
        end = timezone.make_aware(datetime.combine(day, STANDARD_SHIFT_END))

        self.assertEqual((start - REMINDER_LEAD).strftime("%H:%M"), "07:55")
        self.assertEqual((start + REMINDER_GRACE).strftime("%H:%M"), "08:05")
        self.assertEqual((end - REMINDER_LEAD).strftime("%H:%M"), "16:55")
        self.assertEqual((end + REMINDER_GRACE).strftime("%H:%M"), "17:05")

    def test_the_engine_agrees_about_those_moments(self):
        day = date(2026, 10, 5)
        start = timezone.make_aware(datetime.combine(day, STANDARD_SHIFT_START))

        def at(hour, minute, second=0):
            return timezone.make_aware(
                datetime.combine(day, time(hour, minute, second))
            )

        self.assertIsNone(stage_due(at(7, 54, 59), start))
        self.assertEqual(stage_due(at(7, 55), start), STAGE_START_MINUS_5)
        self.assertIsNone(stage_due(at(8, 0), start))
        self.assertIsNone(stage_due(at(8, 4, 59), start))
        self.assertEqual(stage_due(at(8, 5), start), STAGE_START_PLUS_5)
        self.assertIsNone(
            stage_due(at(8, 30), start),
            "08:30 is past both windows — the old shift's reminder time must not "
            "be reachable on the new one",
        )

    # ------------------------------------------------------- attendance verdicts

    def test_the_grace_that_decides_lateness_is_ten_minutes_and_nothing_else(self):
        policy = CheckInPolicy.objects.filter(company_id=self.company).first()
        self.assertIsNotNone(policy)
        self.assertEqual(policy.late_threshold_minutes, 10)
        self.assertFalse(
            GraceTime.objects.filter(is_default=True, is_active=True).exists(),
            "a default GraceTime would silently change both verdicts",
        )
        self.assertIsNone(
            self.shift.grace_time_id,
            "a shift GraceTime takes priority over the policy threshold",
        )

    def test_the_seeded_on_time_arrivals_are_not_flagged_late(self):
        """The regression this phase's seed change exists to prevent.

        The old on-time arrival was 08:18-08:27 — inside an 08:30 start, but late
        against 08:00 plus ten minutes. If the times had been left alone, every
        ordinary demo day would now carry a `late_come` row.
        """
        employees = Employee.objects.entire().filter(
            employee_work_info__company_id=self.company
        )
        late_dates = set(
            AttendanceLateComeEarlyOut.objects.entire()
            .filter(employee_id__in=employees, type="late_come")
            .values_list("attendance_id__attendance_date", flat=True)
        )
        rows = Attendance.objects.entire().filter(employee_id__in=employees)
        self.assertTrue(rows.exists())

        for row in rows.exclude(attendance_date__in=late_dates):
            with self.subTest(date=row.attendance_date, employee=row.employee_id_id):
                if row.attendance_clock_in is None:
                    continue
                self.assertLessEqual(
                    row.attendance_clock_in,
                    time(8, 10),
                    "an arrival after 08:00 + 10m grace would be late",
                )

    def test_no_attendance_is_fabricated_on_a_weekend(self):
        employees = Employee.objects.entire().filter(
            employee_work_info__company_id=self.company
        )
        weekend = [
            row.attendance_date
            for row in Attendance.objects.entire().filter(employee_id__in=employees)
            if row.attendance_date.weekday() >= 5
        ]
        self.assertEqual(weekend, [], "the weekend is not a working day")

    def test_the_seeded_departures_are_never_early(self):
        employees = Employee.objects.entire().filter(
            employee_work_info__company_id=self.company
        )
        early = (
            AttendanceLateComeEarlyOut.objects.entire()
            .filter(employee_id__in=employees, type="early_out")
            .count()
        )
        self.assertEqual(
            early,
            0,
            "17:32-17:44 is after a 17:00 end, so nothing should read as early",
        )

    def test_re_seeding_keeps_the_standard_shift(self):
        seed_joydigi_checkin_demo(today=SEED_DAY + timedelta(days=1))

        self.assertEqual(self.schedule("monday").start_time, time(8, 0))
        self.assertEqual(self.schedule("friday").end_time, time(17, 0))
        self.assertIsNone(self.schedule("saturday"))
        self.assertIsNone(self.schedule("sunday"))
