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

Phase REMOVE-REMOTE-REQUEST-PREAUTH: filing a request no longer needs a
prior per-employee grant. The endpoint's
`EmployeeWorkInformation.allow_remote` gate is gone, so every create test
below runs as an employee who has been granted nothing; the company-wide
`CheckInPolicy.allow_remote` switch stays and keeps its own tests. What an
approved request is worth is unchanged and is pinned elsewhere, in
`attendance/tests/test_remote_attendance.py` — only an approved, uncanceled,
active request covering the working day lets attendance leave the office
network, and this endpoint cannot produce one of those.
"""

from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import RemoteWorkRequest
from employee.models import EmployeeWorkInformation
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
        # No pre-permission is granted anywhere in this fixture.
        # `EmployeeWorkInformation.allow_remote` keeps its `default=False`
        # for every employee here, and no `CheckInPolicy` row exists unless
        # a test creates one. Since Phase REMOVE-REMOTE-REQUEST-PREAUTH
        # that is simply the state of an employee nobody has granted
        # anything to, and every create test below now runs in it — which
        # is CASE 1 asserted by the whole section rather than by one test.

        self.other_company = make_company("Other Remote Co")
        self.other_user = make_user("remote_other", password="secret123")
        self.other_employee = make_employee(
            company=self.other_company,
            email="remote-other@test.joydigi",
            user=self.other_user,
        )

        # `make_employee(user=...)` links the user to a just-created
        # Employee and leaves that instance cached on the user. It predates
        # its work information, so `request.user.employee_get` would resolve
        # an employee whose company reads as None and every company-scoped
        # check would quietly pass — the same trap
        # `attendance/tests/test_remote_attendance.py::RemoteBase.fresh_user`
        # documents. Re-fetching the users drops the cache so each request
        # resolves the employee the way a real one does. It grants nothing:
        # the block this replaced enabled `allow_remote` here as well, and
        # only the re-fetch is kept.
        self.attacker_user = JoydigiUser.objects.get(pk=self.attacker_user.pk)
        self.victim_user = JoydigiUser.objects.get(pk=self.victim_user.pk)

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

    # ---- CASE 1: filing needs no prior permission ----
    #
    # Phase REMOVE-REMOTE-REQUEST-PREAUTH removed the per-employee
    # eligibility gate this endpoint used to run
    # (`EmployeeWorkInformation.allow_remote`). The test that used to sit
    # here asserted a 400 for an employee whose position had never been
    # marked eligible; that rule no longer exists, so the test is replaced
    # by one asserting the rule that does. This is a requirement change,
    # not a loosened assertion: what replaces it checks the flag really is
    # unset while the request succeeds, so quietly reintroducing the gate
    # fails here.
    #
    # No authority is relaxed by any of it. The accepted request is still
    # unapproved, and `attendance/tests/test_remote_attendance.py` holds
    # the proof that an unapproved request grants no attendance bypass.

    def test_employee_with_no_pre_permission_can_create(self):
        work_info = self.attacker.employee_work_info
        self.assertFalse(work_info.allow_remote, "the fixture must grant nothing")

        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = RemoteWorkRequest.objects.get(id=response.data["id"])
        self.assertEqual(instance.employee_id_id, self.attacker.id)
        # Accepted, and still nothing more than a request.
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)
        # Filing granted the employee nothing, here or anywhere else.
        work_info.refresh_from_db()
        self.assertFalse(work_info.allow_remote)

    def test_an_employee_with_no_company_is_refused_for_isolation(self):
        # The one refusal that survives and is not about permission. A
        # request whose employee has no company is scoped to none, and
        # `JoydigiCompanyManager` admits a company-null row under every
        # selected company, so storing one would place it in every
        # company's approval list. Company isolation is what refuses here,
        # not eligibility — and the refusal is unrelated to whether
        # anything has been granted to this employee.
        EmployeeWorkInformation.objects.filter(employee_id=self.attacker).delete()
        # `make_employee(user=...)` leaves a cached Employee on the user,
        # and that instance still carries its own cached work information.
        # Re-fetching the user drops both caches, so the request resolves
        # the employee freshly and actually sees the missing row.
        self.attacker_user = JoydigiUser.objects.get(pk=self.attacker_user.pk)
        self.client.force_authenticate(user=self.attacker_user)

        response = self.client.post(
            "/api/attendance/remote-requests/",
            {"start_date": "2026-09-01", "end_date": "2026-09-02"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(RemoteWorkRequest.objects.entire().count(), 0)
        # Not the retired permission message, which no longer exists.
        self.assertNotIn("chưa được phép", response.data["error"])

    # ---- company-wide switch (kept: an admin policy, not a per-employee
    # grant — `CheckInPolicyForm`, base/forms.py) ----

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

    def test_status_fields_in_the_body_are_ignored(self):
        # CASE 7. `approved`/`canceled` are read-only on the serializer,
        # `request_status` is a read-only SerializerMethodField, and
        # `status` is not a field at all — so none of the four can write
        # anything. The stored row must still read as a plain request.
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
                "status": "approved",
                "request_status": "Approved",
                "approved": True,
                "canceled": False,
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = RemoteWorkRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)
        # Compared against the model's own labels rather than a literal, so
        # the assertion does not depend on the active translation.
        pending_label = str(instance.request_status())
        approved_label = str(RemoteWorkRequest(approved=True).request_status())
        self.assertNotEqual(pending_label, approved_label)
        self.assertEqual(response.data["request_status"], pending_label)

    def test_employee_id_spoof_across_companies_is_ignored(self):
        # CASE 8. The spoofed employee belongs to another company
        # entirely. The row created must be the authenticated employee's
        # own, and nothing at all may be filed under the other company.
        response = self.client.post(
            "/api/attendance/remote-requests/",
            {
                "employee_id": self.other_employee.id,
                "company_id": self.other_company.id,
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
            },
        )

        self.assertEqual(response.status_code, 201, response.data)
        created = RemoteWorkRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)
        self.assertFalse(
            RemoteWorkRequest.objects.entire()
            .filter(employee_id=self.other_employee)
            .exists()
        )

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
