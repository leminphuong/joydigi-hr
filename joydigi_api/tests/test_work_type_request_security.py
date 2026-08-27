"""
Phase SECURITY-4G.1S — hardens the pre-existing `WorkTypeRequest` mobile
API against a real, verified mass-assignment vulnerability discovered
during Phase UI-4G.1's audit.

Root cause: `WorkTypeRequestSerializer` used `fields = "__all__"` with
no `read_only_fields`, and `WorkTypeRequestView.post()` only
overwrote `employee_id` in the raw request dict (not via
`serializer.save(employee_id=...)`) — leaving `approved`/`canceled`
fully client-writable on both create (`POST`) and the generic update
endpoint (`PUT /worktype-requests/<pk>/`, reachable by the request's
own owner via `manager_or_owner_permission_required`'s ownership
bypass, which grants access with NO permission check at all when
`obj.employee_id == request.user.employee_get`).

Given `WorkTypeRequest` approval has a REAL side effect — the daily
`base.scheduler.switch_work_type()` job writes the approved
`work_type_id` onto the employee's actual
`EmployeeWorkInformation.work_type_id` — this let an authenticated
employee potentially self-approve a work-type change without any
manager action.

These tests pin: the fix (serializer `read_only_fields` on
`employee_id`/`approved`/`canceled`) closes both the POST and PUT
paths, employee_id/company spoofing is impossible, spoofing cannot
reach the real `EmployeeWorkInformation.work_type_id`, and — critically
— the LEGITIMATE manager-approval workflow (a completely separate view,
`WorkRequestApproveView`, which mutates the model instance directly and
never touches this serializer) and its real scheduler side effect are
completely unaffected, as is the legitimate cancel/revert workflow.
"""

from datetime import date, timedelta

from django.test import TestCase
from rest_framework.test import APIClient

from base.models import WorkType, WorkTypeRequest
from base.scheduler import switch_work_type
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_auth.models import JoydigiUser


class WorkTypeRequestSecurityAPITests(TestCase):
    def setUp(self):
        self.company = make_company("WTR Security Co")
        self.attacker_user = make_user("wtr_attacker", password="secret123")
        self.victim_user = make_user("wtr_victim", password="secret123")
        self.manager_user = make_user("wtr_manager", password="secret123")

        self.office_type = WorkType.objects.create(work_type="Office")
        self.office_type.company_id.add(self.company)
        self.remote_type = WorkType.objects.create(work_type="Remote")
        self.remote_type.company_id.add(self.company)

        self.manager = make_employee(
            company=self.company,
            email="wtr-manager@test.joydigi",
            user=self.manager_user,
        )
        self.attacker = make_employee(
            company=self.company,
            email="wtr-attacker@test.joydigi",
            user=self.attacker_user,
            work_type=self.office_type,
        )
        self.attacker.employee_work_info.reporting_manager_id = self.manager
        self.attacker.employee_work_info.save(update_fields=["reporting_manager_id"])
        self.attacker = type(self.attacker).objects.select_related(
            "employee_work_info"
        ).get(pk=self.attacker.pk)
        # `JoydigiUser.employee_get` is a reverse-OneToOne descriptor
        # that caches the related `Employee` (and transitively its
        # `employee_work_info`) on first access — re-fetching a fresh
        # `JoydigiUser` instance here avoids `force_authenticate` later
        # handing the view a stale cache from before
        # `reporting_manager_id` was set above (same known testkit
        # quirk as Phase UI-4G.1's RemoteWorkRequest tests).
        self.attacker_user = JoydigiUser.objects.get(pk=self.attacker_user.pk)

        self.victim = make_employee(
            company=self.company,
            email="wtr-victim@test.joydigi",
            user=self.victim_user,
            work_type=self.office_type,
        )

        self.other_company = make_company("WTR Other Co")
        self.other_user = make_user("wtr_other", password="secret123")
        self.other_employee = make_employee(
            company=self.other_company,
            email="wtr-other@test.joydigi",
            user=self.other_user,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker_user)

    def _valid_payload(self, **overrides):
        payload = {
            "work_type_id": self.remote_type.id,
            "previous_work_type_id": self.office_type.id,
            "requested_date": date.today().isoformat(),
            "is_permanent_work_type": True,
            "description": "Requesting a work type change.",
        }
        payload.update(overrides)
        return payload

    # ---- item 1: normal create succeeds ----

    def test_normal_employee_create_succeeds(self):
        response = self.client.post(
            "/api/base/worktype-requests/", self._valid_payload()
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = WorkTypeRequest.objects.get(id=response.data["id"])
        self.assertEqual(instance.employee_id_id, self.attacker.id)
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)

    # ---- item 2/3: employee identity server-derived, spoof ignored ----

    def test_employee_identity_is_server_derived(self):
        response = self.client.post(
            "/api/base/worktype-requests/", self._valid_payload()
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["employee_id"], self.attacker.id)

    def test_employee_id_spoof_is_ignored_not_trusted(self):
        response = self.client.post(
            "/api/base/worktype-requests/",
            self._valid_payload(employee_id=self.victim.id),
        )

        self.assertEqual(response.status_code, 201, response.data)
        created = WorkTypeRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)
        self.assertNotEqual(created.employee_id_id, self.victim.id)

    # ---- item 4: cross-company employee_id spoof safe ----

    def test_cross_company_employee_id_spoof_is_ignored(self):
        response = self.client.post(
            "/api/base/worktype-requests/",
            self._valid_payload(employee_id=self.other_employee.id),
        )

        self.assertEqual(response.status_code, 201, response.data)
        created = WorkTypeRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)
        self.assertNotEqual(created.employee_id_id, self.other_employee.id)

    # ---- items 5-7: approved/canceled spoof on CREATE ----

    def test_approved_spoof_cannot_self_approve_on_create(self):
        response = self.client.post(
            "/api/base/worktype-requests/",
            self._valid_payload(approved=True),
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = WorkTypeRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.approved)

    def test_canceled_spoof_cannot_set_canceled_on_create(self):
        response = self.client.post(
            "/api/base/worktype-requests/",
            self._valid_payload(canceled=True),
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = WorkTypeRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.canceled)

    def test_approved_and_canceled_simultaneous_spoof_on_create(self):
        response = self.client.post(
            "/api/base/worktype-requests/",
            self._valid_payload(approved=True, canceled=True),
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = WorkTypeRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)

    # ---- item 8: spoof cannot mutate EmployeeWorkInformation ----

    def test_spoofed_approval_never_touches_employee_work_information(self):
        before = EmployeeWorkInformation.objects.get(
            employee_id=self.attacker
        ).work_type_id_id

        response = self.client.post(
            "/api/base/worktype-requests/",
            self._valid_payload(approved=True),
        )

        self.assertEqual(response.status_code, 201, response.data)
        after = EmployeeWorkInformation.objects.get(
            employee_id=self.attacker
        ).work_type_id_id
        self.assertEqual(before, after)
        self.assertEqual(before, self.office_type.id)

    # ---- item 9: normal pending state preserved ----

    def test_normal_pending_state_preserved(self):
        response = self.client.post(
            "/api/base/worktype-requests/", self._valid_payload()
        )

        self.assertEqual(response.status_code, 201, response.data)
        instance = WorkTypeRequest.objects.get(id=response.data["id"])
        self.assertFalse(instance.approved)
        self.assertFalse(instance.canceled)

    # ---- item 14: PUT spoof protected (the owner-bypass attack path) ----

    def test_owner_put_cannot_self_approve(self):
        pending = WorkTypeRequest.objects.create(
            employee_id=self.attacker,
            work_type_id=self.remote_type,
            previous_work_type_id=self.office_type,
            requested_date=date.today(),
            is_permanent_work_type=True,
        )

        response = self.client.put(
            f"/api/base/worktype-requests/{pending.id}/",
            self._valid_payload(approved=True),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending.refresh_from_db()
        self.assertFalse(pending.approved)

    def test_owner_put_cannot_self_set_canceled(self):
        pending = WorkTypeRequest.objects.create(
            employee_id=self.attacker,
            work_type_id=self.remote_type,
            previous_work_type_id=self.office_type,
            requested_date=date.today(),
            is_permanent_work_type=True,
        )

        response = self.client.put(
            f"/api/base/worktype-requests/{pending.id}/",
            self._valid_payload(canceled=True),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending.refresh_from_db()
        self.assertFalse(pending.canceled)

    def test_owner_put_cannot_reassign_employee_id(self):
        pending = WorkTypeRequest.objects.create(
            employee_id=self.attacker,
            work_type_id=self.remote_type,
            previous_work_type_id=self.office_type,
            requested_date=date.today(),
            is_permanent_work_type=True,
        )

        response = self.client.put(
            f"/api/base/worktype-requests/{pending.id}/",
            self._valid_payload(employee_id=self.victim.id),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending.refresh_from_db()
        self.assertEqual(pending.employee_id_id, self.attacker.id)

    def test_owner_put_still_allows_legitimate_field_edits(self):
        """The security patch must not break ordinary owner edits of a
        still-pending request's real business fields."""
        pending = WorkTypeRequest.objects.create(
            employee_id=self.attacker,
            work_type_id=self.remote_type,
            previous_work_type_id=self.office_type,
            requested_date=date.today(),
            is_permanent_work_type=True,
            description="Original reason",
        )

        response = self.client.put(
            f"/api/base/worktype-requests/{pending.id}/",
            self._valid_payload(description="Updated reason"),
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending.refresh_from_db()
        self.assertEqual(pending.description, "Updated reason")

    # ---- item 13: unauthenticated denied ----

    def test_unauthenticated_create_denied(self):
        anonymous_client = APIClient()

        response = anonymous_client.post(
            "/api/base/worktype-requests/", self._valid_payload()
        )

        self.assertIn(response.status_code, (401, 403))

    # ---- item 10/11: legitimate manager approval + real side effect ----

    def test_legitimate_manager_approval_still_works_and_side_effect_preserved(self):
        pending = WorkTypeRequest.objects.create(
            employee_id=self.attacker,
            work_type_id=self.remote_type,
            previous_work_type_id=self.office_type,
            requested_date=date.today(),
            is_permanent_work_type=True,
        )

        manager_client = APIClient()
        manager_client.force_authenticate(user=self.manager_user)
        response = manager_client.put(
            f"/api/base/worktype-requests-approve/{pending.id}/", {}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending.refresh_from_db()
        self.assertTrue(pending.approved)
        self.assertFalse(pending.canceled)

        # The daily scheduler job is what actually applies the change —
        # invoke it directly (same function the real cron calls) to
        # prove the legitimate approval side effect is fully intact.
        switch_work_type()

        work_info = EmployeeWorkInformation.objects.get(employee_id=self.attacker)
        self.assertEqual(work_info.work_type_id_id, self.remote_type.id)

    # ---- item 12: legitimate cancel/revert still works ----

    def test_legitimate_owner_cancel_still_works_and_reverts_work_type(self):
        pending = WorkTypeRequest.objects.create(
            employee_id=self.attacker,
            work_type_id=self.remote_type,
            previous_work_type_id=self.office_type,
            requested_date=date.today(),
            is_permanent_work_type=True,
        )

        response = self.client.put(
            f"/api/base/worktype-requests-cancel/{pending.id}/", {}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.data)
        pending.refresh_from_db()
        self.assertTrue(pending.canceled)
        self.assertFalse(pending.approved)

        work_info = EmployeeWorkInformation.objects.get(employee_id=self.attacker)
        self.assertEqual(work_info.work_type_id_id, self.office_type.id)
