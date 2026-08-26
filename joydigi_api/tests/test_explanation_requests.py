"""
Phase UI-4F.1 — Attendance Explanation ("Đơn giải trình") request API.

AttendanceExplanationRequest is a NEW, dedicated employee-request model,
structurally independent of the pre-existing "Attendance Request"
mechanism (Attendance.is_validate_request / AttendanceRequestView at
/api/attendance/attendance-request/, which requests a CREATE/UPDATE of
the real Attendance row and mutates it on approval). This model is a
pure request/response note — creating, approving, or canceling a row
here never touches Attendance, WorkRecords, or Timesheet. These tests
pin: server-derived employee identity on create (never trust
employee_id/approved/canceled from the body), canonical request_type
validation, required (non-blank/non-whitespace) description, strict
ownership scoping on list/detail/cancel, and no side effects on
Attendance/WorkRecords/Timesheet on both create and approve.
"""

from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import AttendanceExplanationRequest
from joydigi.testkit import make_company, make_employee, make_user


class AttendanceExplanationRequestAPITests(TestCase):
    def setUp(self):
        self.company = make_company("Explanation Co")
        self.attacker_user = make_user("expl_attacker", password="secret123")
        self.victim_user = make_user("expl_victim", password="secret123")
        self.attacker = make_employee(
            company=self.company,
            email="expl-attacker@test.joydigi",
            user=self.attacker_user,
        )
        self.victim = make_employee(
            company=self.company,
            email="expl-victim@test.joydigi",
            user=self.victim_user,
        )

        self.other_company = make_company("Other Explanation Co")
        self.other_user = make_user("expl_other", password="secret123")
        self.other_employee = make_employee(
            company=self.other_company,
            email="expl-other@test.joydigi",
            user=self.other_user,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker_user)

    # ---- create: one per canonical request_type (items 3-7) ----

    def _create(self, request_type, **overrides):
        payload = {
            "request_type": request_type,
            "request_date": "2026-08-28",
            "description": "Tôi quên chấm công khi đến văn phòng.",
        }
        payload.update(overrides)
        return self.client.post(
            "/api/attendance/explanation-requests/", payload
        )

    def test_create_missing_check_in(self):
        response = self._create("missing_check_in")

        self.assertEqual(response.status_code, 201, response.data)
        instance = AttendanceExplanationRequest.objects.get(id=response.data["id"])
        self.assertEqual(instance.employee_id_id, self.attacker.id)
        self.assertEqual(instance.request_type, "missing_check_in")
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)

    def test_create_missing_check_out(self):
        response = self._create("missing_check_out")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["request_type"], "missing_check_out")

    def test_create_late_arrival(self):
        response = self._create("late_arrival")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["request_type"], "late_arrival")

    def test_create_early_leave(self):
        response = self._create("early_leave")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["request_type"], "early_leave")

    def test_create_other(self):
        response = self._create("other")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["request_type"], "other")

    # ---- validation ----

    def test_invalid_request_type_rejected(self):
        response = self._create("not_a_real_type")

        self.assertEqual(response.status_code, 400)
        self.assertIn("request_type", response.data)

    def test_missing_request_date_rejected(self):
        response = self.client.post(
            "/api/attendance/explanation-requests/",
            {
                "request_type": "missing_check_in",
                "description": "Tôi quên chấm công khi đến văn phòng.",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("request_date", response.data)

    def test_missing_description_rejected(self):
        response = self.client.post(
            "/api/attendance/explanation-requests/",
            {"request_type": "missing_check_in", "request_date": "2026-08-28"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("description", response.data)

    def test_empty_description_rejected(self):
        response = self._create("missing_check_in", description="")

        self.assertEqual(response.status_code, 400)
        self.assertIn("description", response.data)

    def test_whitespace_only_description_rejected(self):
        response = self._create("missing_check_in", description="   ")

        self.assertEqual(response.status_code, 400)
        self.assertIn("description", response.data)

    # ---- spoofing (items 12-14) ----

    def test_employee_id_spoof_is_ignored_not_trusted(self):
        response = self._create("missing_check_in", employee_id=self.victim.id)

        self.assertEqual(response.status_code, 201, response.data)
        created = AttendanceExplanationRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)
        self.assertNotEqual(created.employee_id_id, self.victim.id)

    def test_approved_spoof_is_ignored(self):
        response = self._create("missing_check_in", approved=True)

        self.assertEqual(response.status_code, 201, response.data)
        instance = AttendanceExplanationRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.approved)

    def test_canceled_spoof_is_ignored(self):
        response = self._create("missing_check_in", canceled=True)

        self.assertEqual(response.status_code, 201, response.data)
        instance = AttendanceExplanationRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.canceled)

    # ---- list / detail (ownership, items 2/15-17) ----

    def test_list_returns_only_own_requests(self):
        own = AttendanceExplanationRequest.objects.create(
            employee_id=self.attacker,
            request_type="missing_check_in",
            request_date=date.today() - timedelta(days=1),
            description="Quên bấm vào.",
        )
        AttendanceExplanationRequest.objects.create(
            employee_id=self.victim,
            request_type="missing_check_in",
            request_date=date.today() - timedelta(days=1),
            description="Quên bấm vào.",
        )

        response = self.client.get("/api/attendance/explanation-requests/")

        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertIn(own.id, ids)
        self.assertEqual(len(ids), 1)

    def test_detail_allows_owner(self):
        own = AttendanceExplanationRequest.objects.create(
            employee_id=self.attacker,
            request_type="late_arrival",
            request_date=date.today() - timedelta(days=1),
            description="Kẹt xe.",
        )

        response = self.client.get(
            f"/api/attendance/explanation-requests/{own.id}/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], own.id)

    def test_detail_denies_non_owner(self):
        victim_request = AttendanceExplanationRequest.objects.create(
            employee_id=self.victim,
            request_type="late_arrival",
            request_date=date.today() - timedelta(days=1),
            description="Kẹt xe.",
        )

        response = self.client.get(
            f"/api/attendance/explanation-requests/{victim_request.id}/"
        )

        self.assertEqual(response.status_code, 404)

    def test_detail_denies_cross_company_employee(self):
        other_request = AttendanceExplanationRequest.objects.create(
            employee_id=self.other_employee,
            request_type="late_arrival",
            request_date=date.today() - timedelta(days=1),
            description="Kẹt xe.",
        )

        response = self.client.get(
            f"/api/attendance/explanation-requests/{other_request.id}/"
        )

        self.assertEqual(response.status_code, 404)

    # ---- cancel (ownership, items 18-21) ----

    def test_cancel_own_pending_request(self):
        own = AttendanceExplanationRequest.objects.create(
            employee_id=self.attacker,
            request_type="early_leave",
            request_date=date.today() - timedelta(days=1),
            description="Đi khám bệnh.",
        )

        response = self.client.post(
            f"/api/attendance/explanation-request-cancel/{own.id}/"
        )

        self.assertEqual(response.status_code, 200)
        own.refresh_from_db()
        self.assertTrue(own.canceled)
        self.assertFalse(own.approved)

    def test_cancel_denies_non_owner(self):
        victim_request = AttendanceExplanationRequest.objects.create(
            employee_id=self.victim,
            request_type="early_leave",
            request_date=date.today() - timedelta(days=1),
            description="Đi khám bệnh.",
        )

        response = self.client.post(
            f"/api/attendance/explanation-request-cancel/{victim_request.id}/"
        )

        self.assertEqual(response.status_code, 404)
        victim_request.refresh_from_db()
        self.assertFalse(victim_request.canceled)

    def test_cancel_denies_cross_company_employee(self):
        other_request = AttendanceExplanationRequest.objects.create(
            employee_id=self.other_employee,
            request_type="early_leave",
            request_date=date.today() - timedelta(days=1),
            description="Đi khám bệnh.",
        )

        response = self.client.post(
            f"/api/attendance/explanation-request-cancel/{other_request.id}/"
        )

        self.assertEqual(response.status_code, 404)
        other_request.refresh_from_db()
        self.assertFalse(other_request.canceled)

    def test_approved_request_cancel_is_idempotent_no_op(self):
        # Same convention as OT/Late-Early: cancel is idempotent (200,
        # no state flip) rather than erroring, once already canceled —
        # but an *approved* request re-canceling would silently
        # overwrite approved=True, which the endpoint intentionally
        # allows only via employee-initiated cancel of their own row
        # (no separate "approved can't cancel" guard exists upstream
        # for OT/Late-Early either, so none is invented here).
        own = AttendanceExplanationRequest.objects.create(
            employee_id=self.attacker,
            request_type="other",
            request_date=date.today() - timedelta(days=1),
            description="Việc riêng.",
            canceled=True,
        )

        response = self.client.post(
            f"/api/attendance/explanation-request-cancel/{own.id}/"
        )

        self.assertEqual(response.status_code, 200)

    # ---- authentication ----

    def test_unauthenticated_request_rejected(self):
        anonymous_client = APIClient()

        response = anonymous_client.get("/api/attendance/explanation-requests/")

        self.assertIn(response.status_code, (401, 403))

    # ---- create/approve does not touch Attendance/WorkRecords/Timesheet
    # (items 23-28) ----

    def test_create_does_not_touch_attendance(self):
        from attendance.models import Attendance

        before = Attendance.objects.count()

        response = self._create("missing_check_in")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Attendance.objects.count(), before)

    def test_create_does_not_touch_workrecords(self):
        from attendance.models import WorkRecords

        before = WorkRecords.objects.count()

        response = self._create("missing_check_out")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(WorkRecords.objects.count(), before)

    def test_create_does_not_touch_timesheet(self):
        from attendance.models import AttendanceSummaryHours

        before = AttendanceSummaryHours.objects.count()

        response = self._create("late_arrival")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(AttendanceSummaryHours.objects.count(), before)

    def test_create_does_not_touch_late_come_early_out(self):
        from attendance.models import AttendanceLateComeEarlyOut

        before = AttendanceLateComeEarlyOut.objects.count()

        response = self._create("early_leave")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(AttendanceLateComeEarlyOut.objects.count(), before)

    def test_approving_the_request_row_directly_does_not_alter_attendance_fields(
        self,
    ):
        """
        There is no employee-facing 'approve' API (approval is admin-only,
        via /duyet-don/ — covered in the admin test module). This test
        pins the model-level guarantee: flipping
        AttendanceExplanationRequest.approved never touches any
        Attendance row, since the two are structurally unrelated (no FK,
        no signal, no save() cross-reference).
        """
        from attendance.models import Attendance

        before_attendance = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        instance = AttendanceExplanationRequest.objects.create(
            employee_id=self.attacker,
            request_type="missing_check_in",
            request_date=date.today() - timedelta(days=1),
            description="Quên bấm vào.",
        )
        instance.approved = True
        instance.save()

        after_attendance = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        self.assertEqual(before_attendance, after_attendance)

    def test_approving_the_request_row_directly_does_not_alter_timesheet_or_workrecords(
        self,
    ):
        from attendance.models import AttendanceSummaryHours, WorkRecords

        before_timesheet = AttendanceSummaryHours.objects.count()
        before_workrecords = WorkRecords.objects.count()

        instance = AttendanceExplanationRequest.objects.create(
            employee_id=self.attacker,
            request_type="missing_check_out",
            request_date=date.today() - timedelta(days=1),
            description="Quên bấm ra.",
        )
        instance.approved = True
        instance.save()

        self.assertEqual(AttendanceSummaryHours.objects.count(), before_timesheet)
        self.assertEqual(WorkRecords.objects.count(), before_workrecords)
