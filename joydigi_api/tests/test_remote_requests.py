"""
Phase UI-4G.1 — Remote/Work-From-Home request API.

RemoteWorkRequest is a NEW, dedicated employee-request model. Phase
UI-4G.1 audited the pre-existing `WorkTypeRequest` (base/models.py) as
a possible reuse candidate and deliberately did NOT reuse it: (1) its
`work_type_id` FK points at `WorkType`, a per-company free-text field
with no canonical "Remote" identifier, and (2) its approve/cancel
paths have real, mature side effects — a scheduled job
(`base.scheduler.switch_work_type`) writes the approved work type back
onto `EmployeeWorkInformation.work_type_id`, and
`WorkTypeRequestCancelView` unconditionally reverts
`employee_work_info.work_type_id` on cancel. RemoteWorkRequest is a
pure request/response note — creating, approving, or canceling a row
here never touches Attendance, WorkRecords, Timesheet, Employee, or
EmployeeWorkInformation. These tests pin: server-derived employee
identity on create (never trust employee_id/approved/canceled/
company_id from the body), date-range validation, strict ownership
scoping on list/detail/cancel, and no side effects anywhere outside
this model.
"""

from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import RemoteWorkRequest
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_auth.models import JoydigiUser


class RemoteWorkRequestAPITests(TestCase):
    def setUp(self):
        self.company = make_company("Remote Co")
        self.attacker_user = make_user("remote_attacker", password="secret123")
        self.victim_user = make_user("remote_victim", password="secret123")
        self.attacker = make_employee(
            company=self.company,
            email="remote-attacker@test.joydigi",
            user=self.attacker_user,
        )
        self.victim = make_employee(
            company=self.company,
            email="remote-victim@test.joydigi",
            user=self.victim_user,
        )
        # EmployeeWorkInformation.allow_remote defaults to False (opt-in
        # per position) — the eligibility gate this phase reuses from
        # the legacy WorkTypeRequestForm (base/forms.py `clean()`)
        # requires it True before a remote request can be created.
        # Every fixture employee is made eligible by default; the
        # eligibility-gate tests below flip it back off explicitly.
        #
        # `make_employee(user=...)` assigns the OneToOne forward FK on
        # the freshly-created Employee, which makes Django cache that
        # (pre-select_related-refresh) Employee instance onto the
        # JoydigiUser's `employee_get` reverse-relation cache. Since
        # `self.attacker`/`self.victim` below are a SEPARATE, later
        # `select_related` re-fetch, mutating their
        # `.employee_work_info` doesn't reach what
        # `request.user.employee_get` sees during a real request (it
        # resolves the stale cached instance on the user object). Every
        # test authenticates via `self.attacker_user`/`self.victim_user`,
        # so re-fetching those from the DB after the mutation drops that
        # stale cache and forces a fresh query per request.
        for emp in (self.attacker, self.victim):
            emp.employee_work_info.allow_remote = True
            emp.employee_work_info.save(update_fields=["allow_remote"])
        self.attacker_user = JoydigiUser.objects.get(pk=self.attacker_user.pk)
        self.victim_user = JoydigiUser.objects.get(pk=self.victim_user.pk)

        self.other_company = make_company("Other Remote Co")
        self.other_user = make_user("remote_other", password="secret123")
        self.other_employee = make_employee(
            company=self.other_company,
            email="remote-other@test.joydigi",
            user=self.other_user,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker_user)

    # ---- create ----

    def test_create_remote_request(self):
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
                "description": "Làm việc từ xa để chăm sóc gia đình.",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = RemoteWorkRequest.objects.get(id=response.data["id"])
        self.assertEqual(instance.employee_id_id, self.attacker.id)
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)

    def test_single_day_request_start_equals_end(self):
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-01"},
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_description_is_optional(self):
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_end_date_before_start_date_rejected(self):
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-05", "end_date": "2026-09-01"},
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_start_date_rejected(self):
        response = self.client.post(
            "/api/attendance/remote-requests/", {"end_date": "2026-09-02"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("start_date", response.data)

    def test_missing_end_date_rejected(self):
        response = self.client.post(
            "/api/attendance/remote-requests/", {"start_date": "2026-09-01"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("end_date", response.data)

    # ---- eligibility gate (reuses the legacy WorkTypeRequestForm's two
    # existing allow_remote flags — Phase UI-4G.1 decision) ----

    def test_employee_not_individually_eligible_is_rejected(self):
        self.attacker.employee_work_info.allow_remote = False
        self.attacker.employee_work_info.save(update_fields=["allow_remote"])

        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "chưa được phép làm việc từ xa", response.data["error"]
        )
        self.assertEqual(RemoteWorkRequest.objects.count(), 0)

    def test_company_policy_disallowing_remote_is_rejected(self):
        from base.models import CheckInPolicy

        CheckInPolicy.objects.create(company_id=self.company, allow_remote=False)

        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("tắt hình thức làm việc từ xa", response.data["error"])
        self.assertEqual(RemoteWorkRequest.objects.count(), 0)

    def test_no_policy_row_defaults_to_allowed(self):
        # No CheckInPolicy row exists for self.company in this test — the
        # gate must not fail closed just because the company never
        # created one (matches the legacy form's `if policy and not
        # policy.allow_remote` — absence is not the same as "disabled").
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_company_policy_allowing_remote_passes(self):
        from base.models import CheckInPolicy

        CheckInPolicy.objects.create(company_id=self.company, allow_remote=True)

        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 201, response.data)

    # ---- spoofing ----

    def test_employee_id_spoof_is_ignored_not_trusted(self):
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {
                "employee_id": self.victim.id,  # spoof attempt
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        created = RemoteWorkRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)
        self.assertNotEqual(created.employee_id_id, self.victim.id)

    def test_company_id_spoof_is_ignored(self):
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {
                "company_id": self.other_company.id,  # spoof attempt; no such field
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        created = RemoteWorkRequest.objects.get(id=response.data["id"])
        # company is always derived transitively via employee_id, never
        # accepted directly — confirmed by the field simply not existing
        # anywhere on the created instance's writable surface.
        self.assertEqual(
            created.employee_id.employee_work_info.company_id_id, self.company.id
        )

    def test_approved_spoof_is_ignored(self):
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02", "approved": True},
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = RemoteWorkRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.approved)

    def test_canceled_spoof_is_ignored(self):
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02", "canceled": True},
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = RemoteWorkRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.canceled)

    # ---- list / detail (ownership) ----

    def test_list_returns_only_own_requests(self):
        own = RemoteWorkRequest.objects.create(
            employee_id=self.attacker,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )
        RemoteWorkRequest.objects.create(
            employee_id=self.victim,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )

        response = self.client.get("/api/attendance/remote-requests/")

        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["results"]]
        self.assertIn(own.id, ids)
        self.assertEqual(len(ids), 1)

    def test_detail_allows_owner(self):
        own = RemoteWorkRequest.objects.create(
            employee_id=self.attacker,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )

        response = self.client.get(f"/api/attendance/remote-requests/{own.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], own.id)

    def test_detail_denies_non_owner(self):
        victim_request = RemoteWorkRequest.objects.create(
            employee_id=self.victim,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )

        response = self.client.get(
            f"/api/attendance/remote-requests/{victim_request.id}/"
        )

        self.assertEqual(response.status_code, 404)

    def test_detail_denies_cross_company_employee(self):
        other_request = RemoteWorkRequest.objects.create(
            employee_id=self.other_employee,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )

        response = self.client.get(
            f"/api/attendance/remote-requests/{other_request.id}/"
        )

        self.assertEqual(response.status_code, 404)

    # ---- cancel (ownership) ----

    def test_cancel_own_pending_request(self):
        own = RemoteWorkRequest.objects.create(
            employee_id=self.attacker,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )

        response = self.client.post(
            f"/api/attendance/remote-request-cancel/{own.id}/"
        )

        self.assertEqual(response.status_code, 200)
        own.refresh_from_db()
        self.assertTrue(own.canceled)
        self.assertFalse(own.approved)

    def test_cancel_denies_non_owner(self):
        victim_request = RemoteWorkRequest.objects.create(
            employee_id=self.victim,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )

        response = self.client.post(
            f"/api/attendance/remote-request-cancel/{victim_request.id}/"
        )

        self.assertEqual(response.status_code, 404)
        victim_request.refresh_from_db()
        self.assertFalse(victim_request.canceled)

    def test_cancel_denies_cross_company_employee(self):
        other_request = RemoteWorkRequest.objects.create(
            employee_id=self.other_employee,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )

        response = self.client.post(
            f"/api/attendance/remote-request-cancel/{other_request.id}/"
        )

        self.assertEqual(response.status_code, 404)
        other_request.refresh_from_db()
        self.assertFalse(other_request.canceled)

    # ---- authentication ----

    def test_unauthenticated_request_rejected(self):
        anonymous_client = APIClient()

        response = anonymous_client.get("/api/attendance/remote-requests/")

        self.assertIn(response.status_code, (401, 403))

    # ---- create/approve does not touch Attendance/WorkRecords/Timesheet/
    # Employee/EmployeeWorkInformation ----

    def test_create_does_not_touch_attendance(self):
        from attendance.models import Attendance

        before = Attendance.objects.count()

        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Attendance.objects.count(), before)

    def test_create_does_not_touch_workrecords(self):
        from attendance.models import WorkRecords

        before = WorkRecords.objects.count()

        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(WorkRecords.objects.count(), before)

    def test_create_does_not_touch_employee_work_type(self):
        before_work_type_id = self.attacker.employee_work_info.work_type_id_id

        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.attacker.employee_work_info.refresh_from_db()
        self.assertEqual(
            self.attacker.employee_work_info.work_type_id_id, before_work_type_id
        )

    def test_approving_the_request_row_directly_does_not_alter_employee_work_type(
        self,
    ):
        """
        There is no employee-facing 'approve' API (approval is admin-only,
        via /duyet-don/ — covered in the admin test module). This test
        pins the model-level guarantee: flipping
        RemoteWorkRequest.approved never touches the employee's real
        EmployeeWorkInformation.work_type_id, unlike WorkTypeRequest's
        scheduler-driven mutation — since the two are structurally
        unrelated (no FK, no signal, no save() cross-reference, no
        scheduler job reads RemoteWorkRequest).
        """
        before_work_type_id = self.attacker.employee_work_info.work_type_id_id

        instance = RemoteWorkRequest.objects.create(
            employee_id=self.attacker,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )
        instance.approved = True
        instance.save()

        self.attacker.employee_work_info.refresh_from_db()
        self.assertEqual(
            self.attacker.employee_work_info.work_type_id_id, before_work_type_id
        )

    def test_canceling_the_request_row_directly_does_not_alter_employee_work_type(
        self,
    ):
        """Pins that cancel here never mimics WorkTypeRequestCancelView's
        unconditional employee_work_info.work_type_id revert."""
        before_work_type_id = self.attacker.employee_work_info.work_type_id_id

        instance = RemoteWorkRequest.objects.create(
            employee_id=self.attacker,
            start_date=date.today() + timedelta(days=1),
            end_date=date.today() + timedelta(days=2),
        )

        response = self.client.post(
            f"/api/attendance/remote-request-cancel/{instance.id}/"
        )

        self.assertEqual(response.status_code, 200)
        self.attacker.employee_work_info.refresh_from_db()
        self.assertEqual(
            self.attacker.employee_work_info.work_type_id_id, before_work_type_id
        )
