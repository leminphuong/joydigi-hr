"""
Phase 5.2 — fix for a real production bug found during Phase 5.1's
device-verified real check-out: `ClockOutAPIView` used to call the
shared web helper `clock_out()`, passing it the lightweight `Request`
shim built for device/API callers. `clock_out()` performed the real DB
mutation successfully, then unconditionally ended in
`render(request, "attendance/components/in_out_component.html", ...)`
— a call that needs a genuine Django `HttpRequest` and raised against
the shim. `ClockOutAPIView`'s bare `except Exception` swallowed that
and returned a false `400 "Already clocked-out"`, even though the
checkout had already succeeded and was durable (confirmed live via
`GET /attendance/my-attendance/` and a force-stop/relaunch).

The fix splits `clock_out()` into `perform_clock_out()` (pure
mutation, never renders) + the existing `clock_out()` wrapper (calls
`perform_clock_out()`, then renders — used only by the real HTMX web
view). `ClockOutAPIView` now calls `perform_clock_out()` directly.
"""

from datetime import date, datetime
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user


class ClockOutAPITests(TestCase):
    def setUp(self):
        self.company = make_company("Clock-Out Co")
        self.user = make_user("clockoutuser", password="secret123")
        self.employee = make_employee(
            company=self.company,
            email="clockout@test.joydigi",
            user=self.user,
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Test Shift")
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

    # A. valid clock-out
    def test_valid_clock_out_returns_2xx_and_persists(self):
        self._clock_in()

        response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["message"], "Clocked-Out")
        self.assertIsNotNone(response.data.get("clock_out"))

        attendance = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNotNone(attendance.attendance_clock_out)
        self.assertEqual(response.data["attendance_id"], attendance.id)

    # E. response content-type
    def test_response_content_type_is_json(self):
        self._clock_in()

        response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("application/json", response["Content-Type"])

    # F. API path never depends on template render()
    def test_api_clock_out_never_calls_render(self):
        self._clock_in()

        with mock.patch("attendance.views.clock_in_out.render") as mock_render:
            response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 200, response.data)
        mock_render.assert_not_called()

    # B. already clocked-out
    def test_already_clocked_out_returns_400_with_no_extra_mutation(self):
        self._clock_in()
        first = self.client.post("/api/attendance/clock-out/")
        self.assertEqual(first.status_code, 200, first.data)
        attendance_after_first = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        clock_out_after_first = attendance_after_first.attendance_clock_out

        second = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(second.status_code, 400)
        self.assertIn("application/json", second["Content-Type"])
        attendance_after_second = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertEqual(
            attendance_after_second.attendance_clock_out, clock_out_after_first
        )

    # C. checkout without any open attendance
    def test_checkout_without_open_attendance_returns_400(self):
        response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 400)
        self.assertIn("application/json", response["Content-Type"])
        self.assertFalse(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists()
        )

    # D. server-derived employee — a client-supplied employee_id must
    # be ignored; only the authenticated user's own attendance changes.
    def test_employee_is_server_derived_not_client_supplied(self):
        other_user = make_user("otherclockoutuser", password="secret123")
        other_employee = make_employee(
            company=self.company,
            email="other-clockout@test.joydigi",
            user=other_user,
        )
        EmployeeWorkInformation.objects.filter(employee_id=other_employee).update(
            shift_id=self.shift
        )
        clock_in_attendance_and_activity(
            employee=other_employee,
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
        self._clock_in()

        response = self.client.post(
            "/api/attendance/clock-out/", {"employee_id": other_employee.id}
        )

        self.assertEqual(response.status_code, 200, response.data)
        own_attendance = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        other_attendance = Attendance.objects.get(
            employee_id=other_employee, attendance_date=self.today
        )
        self.assertIsNotNone(own_attendance.attendance_clock_out)
        self.assertIsNone(other_attendance.attendance_clock_out)
