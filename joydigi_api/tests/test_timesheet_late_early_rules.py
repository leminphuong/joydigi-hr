"""
Phase FUTURE-ATTENDANCE-RULE-AND-PUSH-SOUND.

`GET /api/attendance/timesheet/` reports the late/early flags **exactly as
they are stored**. The previous phase had it re-check each stored flag
against the company clock rule before reporting it, which silently corrected
days recorded under an older rule; this phase reverses that by instruction —
history is left as it was recorded, and only new check-ins and check-outs get
the corrected rule (`attendance/tests/test_shift_relative_late_early.py`).

So what is held here is: the payload mirrors the stored rows, and the month
summary is counted from the days it reports, so the calendar and the numbers
above it can never disagree.
"""

from datetime import date, time

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceLateComeEarlyOut
from joydigi.testkit import make_company, make_employee, make_user

WORK_DATE = date(2026, 9, 7)


class TimesheetFlagReportingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Flags Co")
        self.user = make_user("flags_user", password="secret123")
        self.employee = make_employee(
            company=self.company, email="flags@test.joydigi", user=self.user
        )
        self.client.force_authenticate(user=self.user)

    # ------------------------------------------------------------- helpers
    def attendance(self, *, check_in, check_out, day=WORK_DATE):
        return Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=day,
            attendance_clock_in_date=day,
            attendance_clock_in=check_in,
            attendance_clock_out_date=day,
            attendance_clock_out=check_out,
            minimum_hour="08:00",
        )

    def flag(self, attendance, kind):
        """
        Built the way production builds it (`attendance.views.clock_in_out
        .late_come_create`): the model's own `save()` calls `super().save()`
        twice, so `objects.create()` — which passes `force_insert=True` —
        tries to insert the same row again and raises.
        """
        row = AttendanceLateComeEarlyOut()
        row.type = kind
        row.attendance_id = attendance
        row.employee_id = self.employee
        row.save()
        return row

    def payload(self, day=WORK_DATE):
        response = self.client.get(
            "/api/attendance/timesheet/", {"year": day.year, "month": day.month}
        )
        self.assertEqual(response.status_code, 200)
        return next(
            row for row in response.data["days"] if row["date"] == day.isoformat()
        ), response.data["summary"]

    # --------------------------------------------------------------- tests
    def test_a_stored_late_flag_is_reported(self):
        row = self.attendance(check_in=time(9, 30), check_out=time(17, 0))
        self.flag(row, "late_come")

        day, summary = self.payload()

        self.assertTrue(day["isLate"])
        self.assertFalse(day["isEarly"])
        self.assertEqual(summary["lateCount"], 1)

    def test_a_stored_early_flag_is_reported(self):
        row = self.attendance(check_in=time(8, 0), check_out=time(15, 0))
        self.flag(row, "early_out")

        day, summary = self.payload()

        self.assertTrue(day["isEarly"])
        self.assertFalse(day["isLate"])
        self.assertEqual(summary["earlyCount"], 1)

    def test_a_day_with_no_stored_flag_is_reported_clean(self):
        self.attendance(check_in=time(9, 30), check_out=time(15, 0))

        day, summary = self.payload()

        self.assertFalse(
            day["isLate"],
            msg="the endpoint reports records; it does not judge times",
        )
        self.assertFalse(day["isEarly"])
        self.assertEqual(summary["lateCount"], 0)
        self.assertEqual(summary["earlyCount"], 0)

    def test_a_historical_flag_is_left_exactly_as_recorded(self):
        """
        The deliberate consequence of this phase, pinned so it cannot be
        undone by accident.

        07:50 in and 17:04 out would not be flagged by today's rule. That row
        was written under an older one, and it keeps its flag: this endpoint
        does not reconcile, backfill or re-judge anything. Correcting such a
        day is a decision for whoever owns the record, not something the
        reporting path does on its own.
        """
        row = self.attendance(check_in=time(7, 50), check_out=time(17, 4))
        self.flag(row, "early_out")

        day, summary = self.payload()

        self.assertTrue(day["isEarly"])
        self.assertEqual(summary["earlyCount"], 1)

    def test_the_summary_is_counted_from_the_days_it_reports(self):
        first = self.attendance(
            check_in=time(9, 30), check_out=time(17, 0), day=date(2026, 9, 7)
        )
        second = self.attendance(
            check_in=time(9, 45), check_out=time(15, 0), day=date(2026, 9, 8)
        )
        self.flag(first, "late_come")
        self.flag(second, "late_come")
        self.flag(second, "early_out")

        _day, summary = self.payload()
        response = self.client.get(
            "/api/attendance/timesheet/", {"year": 2026, "month": 9}
        )
        days = response.data["days"]

        self.assertEqual(summary["lateCount"], sum(1 for d in days if d["isLate"]))
        self.assertEqual(summary["earlyCount"], sum(1 for d in days if d["isEarly"]))
        self.assertEqual(summary["lateCount"], 2)
        self.assertEqual(summary["earlyCount"], 1)

    def test_both_flags_on_one_day_are_both_reported(self):
        row = self.attendance(check_in=time(9, 30), check_out=time(15, 0))
        self.flag(row, "late_come")
        self.flag(row, "early_out")

        day, _summary = self.payload()

        self.assertTrue(day["isLate"])
        self.assertTrue(
            day["isEarly"],
            msg="the app's calendar marks either one and its day detail says "
            "which — both have to survive the trip",
        )
