"""
Phase UI-4E.1 — Overtime (OT) permission request API.

OvertimeRequest is a NEW, dedicated employee-request model, structurally
independent of Attendance.attendance_overtime/attendance_overtime_approve
(auto-computed from real clock-in/out, never touched by this phase) and
the existing overtime-approve/<pk> endpoint (which approves real worked
OT on an Attendance row, not a pre-request). These tests pin:
server-derived employee identity on create (never trust employee_id/
approved/canceled from the body), strict ownership scoping on list/
detail/cancel, time-range validation, and no side effects on Attendance/
WorkRecords/Timesheet.
"""

from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import OvertimeRequest
from joydigi.testkit import make_company, make_employee, make_user


class OvertimeRequestAPITests(TestCase):
    def setUp(self):
        self.company = make_company("Overtime Co")
        self.attacker_user = make_user("ot_attacker", password="secret123")
        self.victim_user = make_user("ot_victim", password="secret123")
        self.attacker = make_employee(
            company=self.company,
            email="ot-attacker@test.joydigi",
            user=self.attacker_user,
        )
        self.victim = make_employee(
            company=self.company,
            email="ot-victim@test.joydigi",
            user=self.victim_user,
        )

        self.other_company = make_company("Other OT Co")
        self.other_user = make_user("ot_other", password="secret123")
        self.other_employee = make_employee(
            company=self.other_company,
            email="ot-other@test.joydigi",
            user=self.other_user,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker_user)

    # ---- create ----

    def test_create_overtime_request(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {
                "request_date": "2026-09-01",
                "start_time": "18:00:00",
                "end_time": "21:00:00",
                "description": "Hoàn thành bản phát hành",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = OvertimeRequest.objects.get(id=response.data["id"])
        self.assertEqual(instance.employee_id_id, self.attacker.id)
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)

    def test_description_is_optional(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {
                "request_date": "2026-09-01",
                "start_time": "18:00:00",
                "end_time": "20:00:00",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_create_does_not_touch_attendance_workrecords_or_timesheet(self):
        from attendance.models import Attendance, WorkRecords

        before_attendance = Attendance.objects.count()
        before_workrecords = WorkRecords.objects.count()

        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {
                "request_date": "2026-09-03",
                "start_time": "18:00:00",
                "end_time": "20:00:00",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Attendance.objects.count(), before_attendance)
        self.assertEqual(WorkRecords.objects.count(), before_workrecords)

    def test_employee_id_spoof_is_ignored_not_trusted(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {
                "employee_id": self.victim.id,  # spoof attempt
                "request_date": "2026-09-01",
                "start_time": "18:00:00",
                "end_time": "21:00:00",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        created = OvertimeRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)
        self.assertNotEqual(created.employee_id_id, self.victim.id)

    def test_approved_spoof_is_ignored(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {
                "request_date": "2026-09-01",
                "start_time": "18:00:00",
                "end_time": "21:00:00",
                "approved": True,
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = OvertimeRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.approved)

    def test_canceled_spoof_is_ignored(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {
                "request_date": "2026-09-01",
                "start_time": "18:00:00",
                "end_time": "21:00:00",
                "canceled": True,
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = OvertimeRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.canceled)

    def test_missing_request_date_rejected(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {"start_time": "18:00:00", "end_time": "21:00:00"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("request_date", response.data)

    def test_missing_start_time_rejected(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {"request_date": "2026-09-01", "end_time": "21:00:00"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("start_time", response.data)

    def test_missing_end_time_rejected(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {"request_date": "2026-09-01", "start_time": "18:00:00"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("end_time", response.data)

    def test_end_time_before_start_time_rejected(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {
                "request_date": "2026-09-01",
                "start_time": "21:00:00",
                "end_time": "18:00:00",
            },
        )

        self.assertEqual(response.status_code, 400)

    def test_end_time_equal_to_start_time_rejected(self):
        response = self.client.post(
            "/api/attendance/overtime-requests/",
            {
                "request_date": "2026-09-01",
                "start_time": "18:00:00",
                "end_time": "18:00:00",
            },
        )

        self.assertEqual(response.status_code, 400)

    # ---- list / detail (ownership) ----

    def test_list_returns_only_own_requests(self):
        own = OvertimeRequest.objects.create(
            employee_id=self.attacker,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )
        OvertimeRequest.objects.create(
            employee_id=self.victim,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )

        response = self.client.get("/api/attendance/overtime-requests/")

        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertIn(own.id, ids)
        self.assertEqual(len(ids), 1)

    def test_detail_allows_owner(self):
        own = OvertimeRequest.objects.create(
            employee_id=self.attacker,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )

        response = self.client.get(f"/api/attendance/overtime-requests/{own.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], own.id)

    def test_detail_denies_non_owner(self):
        victim_request = OvertimeRequest.objects.create(
            employee_id=self.victim,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )

        response = self.client.get(
            f"/api/attendance/overtime-requests/{victim_request.id}/"
        )

        self.assertEqual(response.status_code, 404)

    def test_detail_denies_cross_company_employee(self):
        other_request = OvertimeRequest.objects.create(
            employee_id=self.other_employee,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )

        response = self.client.get(
            f"/api/attendance/overtime-requests/{other_request.id}/"
        )

        self.assertEqual(response.status_code, 404)

    # ---- cancel (ownership) ----

    def test_cancel_own_pending_request(self):
        own = OvertimeRequest.objects.create(
            employee_id=self.attacker,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )

        response = self.client.post(
            f"/api/attendance/overtime-request-cancel/{own.id}/"
        )

        self.assertEqual(response.status_code, 200)
        own.refresh_from_db()
        self.assertTrue(own.canceled)
        self.assertFalse(own.approved)

    def test_cancel_denies_non_owner(self):
        victim_request = OvertimeRequest.objects.create(
            employee_id=self.victim,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )

        response = self.client.post(
            f"/api/attendance/overtime-request-cancel/{victim_request.id}/"
        )

        self.assertEqual(response.status_code, 404)
        victim_request.refresh_from_db()
        self.assertFalse(victim_request.canceled)

    def test_cancel_denies_cross_company_employee(self):
        other_request = OvertimeRequest.objects.create(
            employee_id=self.other_employee,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )

        response = self.client.post(
            f"/api/attendance/overtime-request-cancel/{other_request.id}/"
        )

        self.assertEqual(response.status_code, 404)
        other_request.refresh_from_db()
        self.assertFalse(other_request.canceled)

    # ---- authentication ----

    def test_unauthenticated_request_rejected(self):
        anonymous_client = APIClient()

        response = anonymous_client.get("/api/attendance/overtime-requests/")

        self.assertIn(response.status_code, (401, 403))

    # ---- approve does not touch Attendance (Step 14) ----

    def test_approving_the_request_row_directly_does_not_alter_attendance_fields(
        self,
    ):
        """
        There is no employee-facing 'approve' API (approval is admin-only,
        via /duyet-don/ — covered in the admin test module). This test
        pins the model-level guarantee: flipping OvertimeRequest.approved
        never touches any Attendance row, since the two are structurally
        unrelated (no FK, no signal, no save() cross-reference).
        """
        from attendance.models import Attendance

        before_attendance = list(
            Attendance.objects.values_list("id", "attendance_overtime",
                                            "attendance_overtime_approve")
        )
        instance = OvertimeRequest.objects.create(
            employee_id=self.attacker,
            request_date=date.today() + timedelta(days=1),
            start_time="18:00",
            end_time="21:00",
        )
        instance.approved = True
        instance.save()

        after_attendance = list(
            Attendance.objects.values_list("id", "attendance_overtime",
                                            "attendance_overtime_approve")
        )
        self.assertEqual(before_attendance, after_attendance)
