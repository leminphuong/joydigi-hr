"""
Phase UI-4C.1 — Late/Early permission request API.

AttendanceLateEarlyRequest is a NEW, dedicated employee-request model,
entirely distinct from AttendanceLateComeEarlyOut (system-computed from
real clock-in/out times, never touched by this phase). These tests pin:
server-derived employee identity on create (never trust employee_id from
the body), strict ownership scoping on list/detail/cancel, and basic
field validation.
"""

from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import AttendanceLateEarlyRequest
from joydigi.testkit import make_company, make_employee, make_user


class LateEarlyRequestAPITests(TestCase):
    def setUp(self):
        self.company = make_company("Late Early Co")
        self.attacker_user = make_user("late_early_attacker", password="secret123")
        self.victim_user = make_user("late_early_victim", password="secret123")
        self.attacker = make_employee(
            company=self.company,
            email="late-early-attacker@test.joydigi",
            user=self.attacker_user,
        )
        self.victim = make_employee(
            company=self.company,
            email="late-early-victim@test.joydigi",
            user=self.victim_user,
        )

        self.other_company = make_company("Other Co")
        self.other_user = make_user("late_early_other", password="secret123")
        self.other_employee = make_employee(
            company=self.other_company,
            email="late-early-other@test.joydigi",
            user=self.other_user,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker_user)

    # ---- create ----

    def test_create_late_arrival_request(self):
        response = self.client.post(
            "/api/attendance/late-early-requests/",
            {
                "request_type": "late_arrival",
                "request_date": "2026-09-01",
                "requested_time": "09:30",
                "description": "Đưa con đi khám bệnh",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = AttendanceLateEarlyRequest.objects.get(id=response.data["id"])
        self.assertEqual(instance.request_type, "late_arrival")
        self.assertEqual(instance.employee_id_id, self.attacker.id)
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)

    def test_create_early_leave_request(self):
        response = self.client.post(
            "/api/attendance/late-early-requests/",
            {
                "request_type": "early_leave",
                "request_date": "2026-09-02",
                "requested_time": "16:00",
                "description": "Việc gia đình",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = AttendanceLateEarlyRequest.objects.get(id=response.data["id"])
        self.assertEqual(instance.request_type, "early_leave")

    def test_create_does_not_touch_attendance_or_late_come_early_out(self):
        from attendance.models import Attendance, AttendanceLateComeEarlyOut

        before_attendance = Attendance.objects.count()
        before_late_come = AttendanceLateComeEarlyOut.objects.count()

        response = self.client.post(
            "/api/attendance/late-early-requests/",
            {
                "request_type": "late_arrival",
                "request_date": "2026-09-03",
                "requested_time": "09:15",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Attendance.objects.count(), before_attendance)
        self.assertEqual(
            AttendanceLateComeEarlyOut.objects.count(), before_late_come
        )

    def test_employee_id_spoof_is_ignored_not_trusted(self):
        response = self.client.post(
            "/api/attendance/late-early-requests/",
            {
                "employee_id": self.victim.id,  # spoof attempt
                "request_type": "late_arrival",
                "request_date": "2026-09-01",
                "requested_time": "09:30",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        created = AttendanceLateEarlyRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)
        self.assertNotEqual(created.employee_id_id, self.victim.id)

    def test_invalid_request_type_rejected(self):
        response = self.client.post(
            "/api/attendance/late-early-requests/",
            {
                "request_type": "xin_nghi",  # not a canonical value
                "request_date": "2026-09-01",
                "requested_time": "09:30",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("request_type", response.data)

    def test_missing_request_date_rejected(self):
        response = self.client.post(
            "/api/attendance/late-early-requests/",
            {"request_type": "late_arrival", "requested_time": "09:30"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("request_date", response.data)

    def test_missing_requested_time_rejected(self):
        response = self.client.post(
            "/api/attendance/late-early-requests/",
            {"request_type": "late_arrival", "request_date": "2026-09-01"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("requested_time", response.data)

    def test_approved_and_canceled_from_client_are_ignored(self):
        response = self.client.post(
            "/api/attendance/late-early-requests/",
            {
                "request_type": "late_arrival",
                "request_date": "2026-09-01",
                "requested_time": "09:30",
                "approved": True,
                "canceled": True,
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = AttendanceLateEarlyRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)

    # ---- list / detail (ownership) ----

    def test_list_returns_only_own_requests(self):
        own = AttendanceLateEarlyRequest.objects.create(
            employee_id=self.attacker,
            request_type="late_arrival",
            request_date=date.today() + timedelta(days=1),
            requested_time="09:00",
        )
        AttendanceLateEarlyRequest.objects.create(
            employee_id=self.victim,
            request_type="early_leave",
            request_date=date.today() + timedelta(days=1),
            requested_time="16:00",
        )

        response = self.client.get("/api/attendance/late-early-requests/")

        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertIn(own.id, ids)
        self.assertEqual(len(ids), 1)

    def test_detail_allows_owner(self):
        own = AttendanceLateEarlyRequest.objects.create(
            employee_id=self.attacker,
            request_type="late_arrival",
            request_date=date.today() + timedelta(days=1),
            requested_time="09:00",
        )

        response = self.client.get(f"/api/attendance/late-early-requests/{own.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], own.id)

    def test_detail_denies_non_owner(self):
        victim_request = AttendanceLateEarlyRequest.objects.create(
            employee_id=self.victim,
            request_type="early_leave",
            request_date=date.today() + timedelta(days=1),
            requested_time="16:00",
        )

        response = self.client.get(
            f"/api/attendance/late-early-requests/{victim_request.id}/"
        )

        self.assertEqual(response.status_code, 404)

    def test_detail_denies_cross_company_employee(self):
        other_request = AttendanceLateEarlyRequest.objects.create(
            employee_id=self.other_employee,
            request_type="early_leave",
            request_date=date.today() + timedelta(days=1),
            requested_time="16:00",
        )

        response = self.client.get(
            f"/api/attendance/late-early-requests/{other_request.id}/"
        )

        self.assertEqual(response.status_code, 404)

    # ---- cancel (ownership) ----

    def test_cancel_own_pending_request(self):
        own = AttendanceLateEarlyRequest.objects.create(
            employee_id=self.attacker,
            request_type="late_arrival",
            request_date=date.today() + timedelta(days=1),
            requested_time="09:00",
        )

        response = self.client.post(
            f"/api/attendance/late-early-request-cancel/{own.id}/"
        )

        self.assertEqual(response.status_code, 200)
        own.refresh_from_db()
        self.assertTrue(own.canceled)
        self.assertFalse(own.approved)

    def test_cancel_denies_non_owner(self):
        victim_request = AttendanceLateEarlyRequest.objects.create(
            employee_id=self.victim,
            request_type="early_leave",
            request_date=date.today() + timedelta(days=1),
            requested_time="16:00",
        )

        response = self.client.post(
            f"/api/attendance/late-early-request-cancel/{victim_request.id}/"
        )

        self.assertEqual(response.status_code, 404)
        victim_request.refresh_from_db()
        self.assertFalse(victim_request.canceled)

    # ---- authentication ----

    def test_unauthenticated_request_rejected(self):
        anonymous_client = APIClient()

        response = anonymous_client.get("/api/attendance/late-early-requests/")

        self.assertIn(response.status_code, (401, 403))
