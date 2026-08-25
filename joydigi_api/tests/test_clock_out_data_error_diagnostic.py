"""
Phase 6.3A.3 — TEMPORARY diagnostic instrumentation for the real
production `DataError` on `POST /api/attendance/clock-out/`
(see the phase report: Django/Postgres traceback never made it to any
existing log, so a narrowly-scoped, client-visible-but-sanitized JSON
diagnostic was authorized for exactly one controlled real-device test).

These tests exist to prove the diagnostic itself is safe before it is
ever deployed: it must never leak secrets, must never turn a real
failure into a false success, and must never weaken the existing
all-or-nothing transaction guarantee. They should be deleted together
with the diagnostic code once the real root cause is found and fixed.
"""

import json
from datetime import date, datetime
from unittest import mock

from django.db import DataError
from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceActivity, AttendanceOverTime
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user


def _raise_data_error_with_secrets(*args, **kwargs):
    """Simulates a raw DB exception whose message/cause could plausibly
    contain sensitive text, so the sanitizer has something real to strip."""
    try:
        raise Exception(
            "value too long for type character varying(10)\n"
            "DETAIL:  password=hunter2 Authorization: Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123signature "
            "postgresql://postgres:admin123@localhost:5432/joydigi_hr"
        )
    except Exception as cause:
        raise DataError("value too long for type character varying(10)") from cause


class ClockOutDataErrorDiagnosticTests(TestCase):
    def setUp(self):
        self.company = make_company("Diagnostic Co")
        self.user = make_user("diaguser", password="secret123")
        self.employee = make_employee(
            company=self.company,
            email="diag@test.joydigi",
            user=self.user,
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Diag Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )
        self.today = date.today()
        day_name = self.today.strftime("%A").lower()
        self.day = EmployeeShiftDay.objects.get(day=day_name)
        EmployeeShiftSchedule.objects.get_or_create(
            shift_id=self.shift,
            day=self.day,
            defaults={
                "is_night_shift": False,
                "minimum_working_hour": "08:00",
                "start_time": "08:00:00",
                "end_time": "17:00:00",
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _clock_in(self):
        clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=self.today,
            attendance_date=self.today,
            day=self.day,
            now="08:00",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=0,
            end_time=1,
            in_datetime=datetime.now(),
        )

    # 1. Normal clock-out is completely unaffected by the diagnostic code.
    def test_normal_clock_out_unaffected(self):
        self._clock_in()

        response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["message"], "Clocked-Out")
        attendance = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNotNone(attendance.attendance_clock_out)

    # 2, 3, 4, 5, 6, 7, 8, 9. The DataError path: correct status, valid
    # JSON, correct code, only the approved diagnostic keys, no
    # traceback/SQL/secrets/tokens/verification-proof content anywhere
    # in the response body.
    def test_dataerror_returns_sanitized_diagnostic_json(self):
        self._clock_in()

        with mock.patch(
            "attendance.models.format_time",
            side_effect=_raise_data_error_with_secrets,
        ):
            response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 500, response.data)
        self.assertIn("application/json", response["Content-Type"])

        data = response.data
        self.assertEqual(data["code"], "CLOCK_OUT_DATA_ERROR")
        self.assertFalse(data["success"])
        self.assertIn("message", data)

        diagnostic = data["diagnostic"]
        self.assertEqual(
            set(diagnostic.keys()),
            {"exception_type", "db_exception_type", "db_message", "stage", "error_id"},
        )
        self.assertEqual(diagnostic["exception_type"], "DataError")
        self.assertEqual(diagnostic["db_exception_type"], "Exception")
        self.assertTrue(diagnostic["error_id"])
        # `format_time` is first called inside `update_attendance_overtime()`
        # (attendance/models.py), which `Attendance.save()` calls before
        # `super().save()` — so the last stage marker actually reached is
        # the one set just before `attendance.save()` in
        # `clock_out_attendance_and_activity()`, not a later one.
        self.assertEqual(diagnostic["stage"], "attendance_save")

        body_text = json.dumps(data)
        for forbidden in (
            "hunter2",
            "Bearer",
            "eyJ",
            "postgresql://",
            "admin123",
            "Authorization",
            "Traceback",
            'File "',
            "DETAIL",
        ):
            self.assertNotIn(
                forbidden, body_text, f"leaked sensitive text: {forbidden!r}"
            )

    # 10. DataError still rolls back the ENTIRE checkout transaction —
    # including writes that had ALREADY executed before the failure
    # point. Failing inside `ot.save(update_fields=[...])` (the very
    # last write in `Attendance.save()`) means, by then, the
    # AttendanceActivity write, the Attendance row's own UPDATE, and the
    # AttendanceOverTime F()-expression `.update()` have all already run
    # — this proves the nested `transaction.atomic()` in
    # `Attendance.save()` and the outer one in `ClockOutAPIView.post()`
    # correctly unwind writes that already happened, not just ones that
    # never got a chance to run.
    def test_dataerror_rolls_back_writes_that_already_executed(self):
        self._clock_in()
        # Backdate the open activity's clock-in so the checkout below
        # computes real elapsed worked time — otherwise clock-in and
        # clock-out happen within the same test second, worked time
        # rounds to "00:00", every diff_* ends up 0, and
        # `Attendance.save()` returns before ever reaching the
        # AttendanceOverTime block this test needs to exercise.
        AttendanceActivity.objects.filter(
            employee_id=self.employee, clock_out__isnull=True
        ).update(clock_in="06:00:00")
        attendance_before = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNone(attendance_before.attendance_clock_out)
        ot_before = AttendanceOverTime.objects.get(employee_id=self.employee)
        ot_snapshot = (
            ot_before.hour_account_second,
            ot_before.hour_pending_second,
            ot_before.overtime_second,
        )

        with mock.patch(
            "attendance.models.AttendanceOverTime.save",
            side_effect=_raise_data_error_with_secrets,
        ):
            response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 500)

        attendance_after = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNone(
            attendance_after.attendance_clock_out,
            "Attendance.save()'s UPDATE (which had already executed) "
            "must have been rolled back",
        )
        self.assertTrue(
            AttendanceActivity.objects.filter(
                employee_id=self.employee, clock_out__isnull=True
            ).exists(),
            "AttendanceActivity's clock-out write (already executed) "
            "must have been rolled back",
        )
        ot_after = AttendanceOverTime.objects.get(employee_id=self.employee)
        self.assertEqual(
            (
                ot_after.hour_account_second,
                ot_after.hour_pending_second,
                ot_after.overtime_second,
            ),
            ot_snapshot,
            "The AttendanceOverTime F()-expression update (which had "
            "already executed) must have been rolled back too",
        )

    # 11. A non-DataError exception is NOT swallowed by this handler —
    # normal 5xx behavior (and the existing Flutter friendly-message
    # handling from Phase 6.3A.1) is untouched.
    def test_non_dataerror_exception_is_not_caught_here(self):
        self._clock_in()

        with mock.patch(
            "attendance.models.AttendanceActivity.save",
            side_effect=ValueError("unrelated failure, not a DataError"),
        ):
            with self.assertRaises(ValueError):
                self.client.post("/api/attendance/clock-out/")

    # 12. Existing 4xx behavior (e.g. already clocked-out) is unchanged.
    def test_existing_4xx_behavior_unchanged(self):
        response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 400)
        self.assertIn("application/json", response["Content-Type"])
        self.assertNotIn("diagnostic", response.data)
