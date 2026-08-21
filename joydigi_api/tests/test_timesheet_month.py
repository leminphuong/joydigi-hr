"""
Phase 3A backend tests for `GET /api/attendance/timesheet/`.

Covers: auth gating, employee scoping (no cross-employee leak — this is
exactly the class of bug found in `LateComeEarlyOutView` during the
Phase 3A audit), month/year validation, and each real data source the
view merges (Attendance, AttendanceLateComeEarlyOut, approved
LeaveRequest, Holidays).
"""

from datetime import date, time

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceLateComeEarlyOut
from base.models import Holidays
from joydigi.testkit import make_company, make_employee, make_user
from leave.models import LeaveRequest, LeaveType


class TimesheetMonthViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Timesheet Co")
        self.password = "secret123"
        self.user = make_user("timesheet_user", password=self.password)
        self.employee = make_employee(
            company=self.company,
            email="timesheet_user@test.joydigi",
            user=self.user,
        )
        self.client.force_authenticate(user=self.user)

    def _get(self, **params):
        return self.client.get("/api/attendance/timesheet/", params)

    def test_unauthenticated_rejected(self):
        anon_client = APIClient()
        response = anon_client.get(
            "/api/attendance/timesheet/", {"year": 2026, "month": 8}
        )
        self.assertEqual(response.status_code, 401)

    def test_authenticated_access_returns_200(self):
        response = self._get(year=2026, month=8)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["year"], 2026)
        self.assertEqual(response.data["month"], 8)

    def test_missing_year_or_month_is_400(self):
        self.assertEqual(self._get(month=8).status_code, 400)
        self.assertEqual(self._get(year=2026).status_code, 400)

    def test_invalid_month_is_400(self):
        self.assertEqual(self._get(year=2026, month=13).status_code, 400)
        self.assertEqual(self._get(year=2026, month=0).status_code, 400)

    def test_invalid_year_is_400(self):
        self.assertEqual(self._get(year=1900, month=8).status_code, 400)

    def test_empty_month_returns_valid_zeroed_response_not_an_error(self):
        response = self._get(year=2026, month=8)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["presentDays"], 0)
        self.assertEqual(len(response.data["days"]), 31)
        self.assertTrue(all(d["checkIn"] is None for d in response.data["days"]))

    def test_full_attendance_day_reflects_real_clock_in_and_out(self):
        Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 8, 10),
            attendance_clock_in=time(8, 30),
            attendance_clock_out=time(17, 35),
            attendance_worked_hour="09:05",
            at_work_second=32700,
        )
        response = self._get(year=2026, month=8)
        day = next(d for d in response.data["days"] if d["date"] == "2026-08-10")
        self.assertEqual(day["checkIn"], "08:30:00")
        self.assertEqual(day["checkOut"], "17:35:00")
        self.assertEqual(day["workedHour"], "09:05")
        self.assertEqual(response.data["summary"]["presentDays"], 1)
        self.assertEqual(response.data["summary"]["workedSeconds"], 32700)

    def test_check_in_only_leaves_check_out_null_not_fabricated(self):
        Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 8, 11),
            attendance_clock_in=time(8, 30),
        )
        response = self._get(year=2026, month=8)
        day = next(d for d in response.data["days"] if d["date"] == "2026-08-11")
        self.assertEqual(day["checkIn"], "08:30:00")
        self.assertIsNone(day["checkOut"])

    def test_late_and_early_are_scoped_to_the_requesting_employee_only(self):
        other_employee = make_employee(
            company=self.company,
            email="other@test.joydigi",
        )
        my_attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 8, 12),
            attendance_clock_in=time(8, 50),
        )
        other_attendance = Attendance.objects.create(
            employee_id=other_employee,
            attendance_date=date(2026, 8, 12),
            attendance_clock_in=time(8, 55),
        )
        # `AttendanceLateComeEarlyOut.save()` calls `super().save()` twice
        # (once to get a pk, once to set `employee_id`); `.objects.create()`
        # passes `force_insert=True` through both calls, which makes the
        # second one re-attempt the same INSERT and fail a UNIQUE
        # constraint. Pre-existing model bug, unrelated to this endpoint —
        # worked around here by using plain `.save()` instead of `.create()`.
        AttendanceLateComeEarlyOut(attendance_id=my_attendance, type="late_come").save()
        AttendanceLateComeEarlyOut(
            attendance_id=other_attendance, type="late_come"
        ).save()

        response = self._get(year=2026, month=8)

        day = next(d for d in response.data["days"] if d["date"] == "2026-08-12")
        self.assertTrue(day["isLate"])
        # Only this employee's own late record is counted — the other
        # employee's identical-day record must not leak in.
        self.assertEqual(response.data["summary"]["lateCount"], 1)

    def test_approved_leave_marks_the_day_as_leave(self):
        leave_type = LeaveType.objects.create(name="Annual")
        LeaveRequest.objects.create(
            employee_id=self.employee,
            leave_type_id=leave_type,
            start_date=date(2026, 8, 13),
            end_date=date(2026, 8, 13),
            status="approved",
            description="Test leave",
        )
        response = self._get(year=2026, month=8)
        day = next(d for d in response.data["days"] if d["date"] == "2026-08-13")
        self.assertTrue(day["isLeave"])
        self.assertEqual(response.data["summary"]["leaveDays"], 1)

    def test_a_requested_but_not_approved_leave_does_not_mark_the_day(self):
        leave_type = LeaveType.objects.create(name="Annual")
        LeaveRequest.objects.create(
            employee_id=self.employee,
            leave_type_id=leave_type,
            start_date=date(2026, 8, 14),
            end_date=date(2026, 8, 14),
            status="requested",
            description="Pending leave",
        )
        response = self._get(year=2026, month=8)
        day = next(d for d in response.data["days"] if d["date"] == "2026-08-14")
        self.assertFalse(day["isLeave"])

    def test_company_holiday_marks_the_day(self):
        Holidays.objects.create(
            name="Test Holiday",
            start_date=date(2026, 8, 15),
            end_date=date(2026, 8, 15),
            company_id=self.company,
            is_specific=False,
        )
        response = self._get(year=2026, month=8)
        day = next(d for d in response.data["days"] if d["date"] == "2026-08-15")
        self.assertTrue(day["isHoliday"])
