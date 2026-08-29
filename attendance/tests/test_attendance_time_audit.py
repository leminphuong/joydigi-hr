"""Phase ATT-TIME-3A — tests for the READ-ONLY historical audit.

The audit decides which rows a later repair phase may safely touch, so
these tests care most about the boundaries: a row is only SAFE when the
reconstruction is genuinely unambiguous, and nothing is ever written.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.utils import timezone as django_timezone

from attendance.methods.attendance_time_audit import (
    ALREADY_CORRECT,
    AMBIGUOUS,
    EXACT_90_MIN_ERROR,
    MISSING_VALUE,
    NO_ACTIVITY,
    NOT_APPLICABLE,
    OTHER_DIFFERENCE,
    SAFE,
    audit_attendance,
    audit_queryset,
)
from attendance.models import Attendance, AttendanceActivity
from base.models import EmployeeShift, EmployeeShiftDay
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")
KOLKATA = ZoneInfo("Asia/Kolkata")


class AuditBase(TestCase):
    def setUp(self):
        self.company = make_company("Audit Co")
        self.user = make_user("audituser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="audit@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Audit Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )
        self.day = date.today() - timedelta(days=3)
        self.shift_day = EmployeeShiftDay.objects.get(
            day=self.day.strftime("%A").lower()
        )

    def vn_instant(self, day, hour, minute):
        """The true instant at which Vietnam's clocks read `hour:minute`."""
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=VIETNAM)

    def kolkata_reading(self, instant):
        """What the buggy server wrote: the same instant misread through
        Asia/Kolkata, i.e. 1h30m early."""
        return instant.astimezone(KOLKATA).time().replace(second=0, microsecond=0)

    def make_attendance(self, clock_in=None, clock_out=None, day=None):
        return Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=day or self.day,
            shift_id=self.shift,
            attendance_day=self.shift_day,
            attendance_clock_in_date=day or self.day,
            attendance_clock_in=clock_in,
            attendance_clock_out=clock_out,
            minimum_hour="08:00",
        )

    def make_activity(self, in_instant, out_instant=None, day=None):
        return AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=day or self.day,
            clock_in_date=(day or self.day),
            shift_day=self.shift_day,
            clock_in=django_timezone.localtime(in_instant).time(),
            in_datetime=in_instant,
            out_datetime=out_instant,
            clock_out=(
                django_timezone.localtime(out_instant).time() if out_instant else None
            ),
            clock_out_date=(day or self.day) if out_instant else None,
        )


class ReconstructionTests(AuditBase):
    def test_a_kolkata_written_row_reconstructs_to_vietnam_time(self):
        """The core case: stored 06:47, true instant 08:17."""
        instant_in = self.vn_instant(self.day, 8, 17)
        instant_out = self.vn_instant(self.day, 17, 45)
        attendance = self.make_attendance(
            clock_in=self.kolkata_reading(instant_in),
            clock_out=self.kolkata_reading(instant_out),
        )
        self.make_activity(instant_in, instant_out)

        result = audit_attendance(attendance)

        self.assertEqual(result["match"], SAFE)
        self.assertEqual(result["current_clock_in"], time(6, 47))
        self.assertEqual(result["expected_clock_in"], time(8, 17))
        self.assertEqual(result["clock_in_status"], EXACT_90_MIN_ERROR)
        self.assertEqual(result["clock_in_difference_minutes"], 90)
        self.assertEqual(result["expected_clock_out"], time(17, 45))
        self.assertEqual(result["clock_out_status"], EXACT_90_MIN_ERROR)

    def test_a_correct_row_is_not_flagged(self):
        instant_in = self.vn_instant(self.day, 8, 17)
        attendance = self.make_attendance(clock_in=time(8, 17))
        self.make_activity(instant_in)

        result = audit_attendance(attendance)

        self.assertEqual(result["match"], SAFE)
        self.assertEqual(result["clock_in_status"], ALREADY_CORRECT)
        self.assertEqual(result["clock_in_difference_minutes"], 0)

    def test_clock_in_reconstructs_from_the_earliest_activity(self):
        """`attendance_clock_in` is the day's *first* check-in (model help
        text), so a later activity must not win."""
        first = self.vn_instant(self.day, 8, 0)
        second = self.vn_instant(self.day, 13, 30)
        attendance = self.make_attendance(clock_in=self.kolkata_reading(first))
        self.make_activity(second)
        self.make_activity(first)

        result = audit_attendance(attendance)

        self.assertEqual(result["expected_clock_in"], time(8, 0))
        self.assertEqual(result["clock_in_status"], EXACT_90_MIN_ERROR)

    def test_clock_out_reconstructs_from_the_latest_activity(self):
        """`attendance_clock_out` is the day's *last* check-out."""
        morning_in = self.vn_instant(self.day, 8, 0)
        morning_out = self.vn_instant(self.day, 12, 0)
        afternoon_in = self.vn_instant(self.day, 13, 0)
        afternoon_out = self.vn_instant(self.day, 17, 45)
        attendance = self.make_attendance(
            clock_in=self.kolkata_reading(morning_in),
            clock_out=self.kolkata_reading(afternoon_out),
        )
        self.make_activity(morning_in, morning_out)
        self.make_activity(afternoon_in, afternoon_out)

        result = audit_attendance(attendance)

        self.assertEqual(result["expected_clock_out"], time(17, 45))
        self.assertEqual(result["clock_out_status"], EXACT_90_MIN_ERROR)

    def test_a_difference_that_is_not_ninety_minutes_is_reported_separately(self):
        """Not every wrong row is the timezone bug — a manually edited
        time must not be silently lumped in with it."""
        instant_in = self.vn_instant(self.day, 8, 0)
        attendance = self.make_attendance(clock_in=time(7, 30))
        self.make_activity(instant_in)

        result = audit_attendance(attendance)

        self.assertEqual(result["clock_in_status"], OTHER_DIFFERENCE)
        self.assertEqual(result["clock_in_difference_minutes"], 30)


class SafetyBoundaryTests(AuditBase):
    def test_a_row_with_no_activity_is_never_safe(self):
        attendance = self.make_attendance(clock_in=time(6, 47))

        result = audit_attendance(attendance)

        self.assertEqual(result["match"], NO_ACTIVITY)
        self.assertIsNone(result["expected_clock_in"])

    def test_an_activity_without_an_aware_instant_is_never_safe(self):
        """Imported/legacy rows carry no `in_datetime`; there is nothing
        trustworthy to rebuild from."""
        attendance = self.make_attendance(clock_in=time(6, 47))
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=self.day,
            clock_in_date=self.day,
            shift_day=self.shift_day,
            clock_in=time(6, 47),
            in_datetime=None,
        )

        result = audit_attendance(attendance)

        self.assertEqual(result["match"], AMBIGUOUS)

    def test_duplicate_activity_start_instants_are_ambiguous(self):
        instant = self.vn_instant(self.day, 8, 0)
        attendance = self.make_attendance(clock_in=self.kolkata_reading(instant))
        self.make_activity(instant)
        self.make_activity(instant)

        result = audit_attendance(attendance)

        self.assertEqual(result["match"], AMBIGUOUS)
        self.assertIn("duplicate in_datetime among activities", result["notes"])

    def test_a_stored_clock_out_with_no_activity_out_is_ambiguous(self):
        """Mirrors the real writer, which puts a clock-out on the
        employee's newest row even when the activity it closed belongs to
        an older date."""
        instant_in = self.vn_instant(self.day, 8, 0)
        attendance = self.make_attendance(
            clock_in=self.kolkata_reading(instant_in), clock_out=time(16, 15)
        )
        self.make_activity(instant_in)

        result = audit_attendance(attendance)

        self.assertEqual(result["match"], AMBIGUOUS)
        self.assertEqual(result["clock_out_status"], MISSING_VALUE)

    def test_a_null_clock_out_stays_null(self):
        """An employee still clocked in must not acquire a clock-out."""
        instant_in = self.vn_instant(self.day, 8, 0)
        attendance = self.make_attendance(clock_in=self.kolkata_reading(instant_in))
        self.make_activity(instant_in)

        result = audit_attendance(attendance)

        self.assertEqual(result["match"], SAFE)
        self.assertIsNone(result["expected_clock_out"])
        self.assertIsNone(result["current_clock_out"])
        self.assertEqual(result["clock_out_status"], NOT_APPLICABLE)


class MidnightAndOvernightTests(AuditBase):
    def test_a_late_evening_clock_in_reconstructs_correctly(self):
        """23:30 Vietnam is 22:00 in Kolkata — same calendar day, so the
        difference is still a clean +90."""
        instant = self.vn_instant(self.day, 23, 30)
        attendance = self.make_attendance(clock_in=self.kolkata_reading(instant))
        self.make_activity(instant)

        result = audit_attendance(attendance)

        self.assertEqual(result["expected_clock_in"], time(23, 30))
        self.assertEqual(result["clock_in_status"], EXACT_90_MIN_ERROR)

    def test_a_just_after_midnight_clock_in_reconstructs_across_the_day(self):
        """00:30 Vietnam is 23:00 on the *previous* day in Kolkata. A raw
        time subtraction would read that as -1350 minutes; the audit must
        still recognise a +90 error."""
        instant = self.vn_instant(self.day, 0, 30)
        stored = self.kolkata_reading(instant)
        self.assertEqual(stored, time(23, 0))

        attendance = self.make_attendance(clock_in=stored)
        self.make_activity(instant)

        result = audit_attendance(attendance)

        self.assertEqual(result["expected_clock_in"], time(0, 30))
        self.assertEqual(result["clock_in_difference_minutes"], 90)
        self.assertEqual(result["clock_in_status"], EXACT_90_MIN_ERROR)

    def test_an_overnight_shift_keeps_its_business_date(self):
        """The writer files a night shift under the *previous* business
        date while `clock_in_date` holds the real calendar day. The audit
        matches on `attendance_date` and must not force the two to agree.
        """
        business_date = self.day
        calendar_date = self.day + timedelta(days=1)
        instant_in = self.vn_instant(calendar_date, 1, 15)

        attendance = self.make_attendance(
            clock_in=self.kolkata_reading(instant_in), day=business_date
        )
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=business_date,
            clock_in_date=calendar_date,
            shift_day=self.shift_day,
            clock_in=django_timezone.localtime(instant_in).time(),
            in_datetime=instant_in,
        )

        result = audit_attendance(attendance)

        self.assertEqual(result["match"], SAFE)
        self.assertEqual(result["expected_clock_in"], time(1, 15))
        self.assertEqual(result["clock_in_status"], EXACT_90_MIN_ERROR)


class QuerysetAndWriteSafetyTests(AuditBase):
    def test_audit_queryset_summarises_a_mixed_population(self):
        good_instant = self.vn_instant(self.day, 9, 0)
        good = self.make_attendance(clock_in=time(9, 0))
        self.make_activity(good_instant)

        other_day = self.day - timedelta(days=1)
        other_shift_day = EmployeeShiftDay.objects.get(
            day=other_day.strftime("%A").lower()
        )
        bad_instant = datetime(
            other_day.year, other_day.month, other_day.day, 8, 17, tzinfo=VIETNAM
        )
        bad = self.make_attendance(
            clock_in=self.kolkata_reading(bad_instant), day=other_day
        )
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=other_day,
            clock_in_date=other_day,
            shift_day=other_shift_day,
            clock_in=django_timezone.localtime(bad_instant).time(),
            in_datetime=bad_instant,
        )

        orphan_day = self.day - timedelta(days=2)
        orphan_shift_day = EmployeeShiftDay.objects.get(
            day=orphan_day.strftime("%A").lower()
        )
        orphan = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=orphan_day,
            shift_id=self.shift,
            attendance_day=orphan_shift_day,
            attendance_clock_in_date=orphan_day,
            attendance_clock_in=time(6, 47),
            minimum_hour="08:00",
        )

        results, summary = audit_queryset()

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["safe"], 2)
        self.assertEqual(summary["no_activity"], 1)
        self.assertEqual(summary["exact_90_min_error"], 1)
        self.assertEqual(summary["already_correct"], 1)

        by_id = {r["attendance_id"]: r for r in results}
        self.assertEqual(by_id[good.id]["clock_in_status"], ALREADY_CORRECT)
        self.assertEqual(by_id[bad.id]["clock_in_status"], EXACT_90_MIN_ERROR)
        self.assertEqual(by_id[orphan.id]["match"], NO_ACTIVITY)

    def test_the_audit_never_writes_to_the_database(self):
        """The guarantee this whole phase rests on."""
        instant = self.vn_instant(self.day, 8, 17)
        attendance = self.make_attendance(clock_in=self.kolkata_reading(instant))
        activity = self.make_activity(instant)

        before = (
            attendance.attendance_clock_in,
            attendance.attendance_clock_out,
            activity.in_datetime,
            activity.out_datetime,
        )

        audit_attendance(attendance)
        audit_queryset()

        attendance.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(
            (
                attendance.attendance_clock_in,
                attendance.attendance_clock_out,
                activity.in_datetime,
                activity.out_datetime,
            ),
            before,
        )

    def test_the_audit_module_contains_no_write_operations(self):
        for path in (
            "attendance/methods/attendance_time_audit.py",
            "attendance/management/commands/audit_attendance_times.py",
        ):
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            for forbidden in (
                ".save(",
                ".update(",
                ".bulk_update(",
                ".delete(",
                ".create(",
                "raw(",
                "executemany",
            ):
                self.assertNotIn(forbidden, source, msg=f"{forbidden} found in {path}")

    def test_the_command_exposes_no_write_option(self):
        """Asserted against the real argument parser rather than the
        source text — the docstring legitimately names the flags it
        refuses to offer."""
        from attendance.management.commands.audit_attendance_times import Command

        parser = Command().create_parser("manage.py", "audit_attendance_times")
        options = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        for forbidden in ("--apply", "--fix", "--write", "--commit", "--repair"):
            self.assertNotIn(forbidden, options)
        self.assertIn("--limit", options)

    def test_the_command_reports_without_changing_anything(self):
        """End-to-end dry run over a known-bad row."""
        from io import StringIO

        from django.core.management import call_command

        instant = self.vn_instant(self.day, 8, 17)
        attendance = self.make_attendance(clock_in=self.kolkata_reading(instant))
        self.make_activity(instant)

        out = StringIO()
        call_command("audit_attendance_times", stdout=out)
        printed = out.getvalue()

        self.assertIn("DRY RUN", printed)
        self.assertIn("Exact 90-minute errors", printed)
        self.assertIn(str(attendance.id), printed)

        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_in, time(6, 47))

    def test_no_fixed_offset_arithmetic_is_used_to_repair(self):
        """90 minutes may be used to *label* a difference, never to
        produce an expected value."""
        with open(
            "attendance/methods/attendance_time_audit.py", encoding="utf-8"
        ) as handle:
            source = handle.read()
        for forbidden in (
            "timedelta(minutes=90)",
            "timedelta(hours=1, minutes=30)",
            "+ timedelta",
        ):
            self.assertNotIn(forbidden, source)
