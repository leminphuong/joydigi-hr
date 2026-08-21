"""
Phase 4A backend security-audit fixes.

`ShiftRequestView`, `WorkTypeRequestView`, `IndividualShiftRequestView`,
and `IndividualWorkTypeRequestView` previously let any authenticated
employee (1) create a shift/work-type request on behalf of any other
employee by supplying an arbitrary `employee_id` in the POST body, and
(2) read any other employee's shift/work-type request detail by pk,
with no ownership check at all (`object_check` only fetches by id).

These tests pin the fix: `employee_id` is always server-derived on
create, and detail-by-pk requires the caller to own the record (or be
its reporting manager / hold the view permission).
"""

from django.test import TestCase
from rest_framework.test import APIClient

from base.models import EmployeeShift, ShiftRequest, WorkType, WorkTypeRequest
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user


class ShiftWorkTypeRequestIDORTests(TestCase):
    def setUp(self):
        self.company = make_company("IDOR Co")
        self.attacker_user = make_user("attacker", password="secret123")
        self.victim_user = make_user("victim", password="secret123")
        self.attacker = make_employee(
            company=self.company,
            email="attacker@test.joydigi",
            user=self.attacker_user,
        )
        self.victim = make_employee(
            company=self.company,
            email="victim@test.joydigi",
            user=self.victim_user,
        )
        self.manager_user = make_user("manager", password="secret123")
        self.manager = make_employee(
            company=self.company,
            email="manager@test.joydigi",
            user=self.manager_user,
        )
        EmployeeWorkInformation.objects.filter(employee_id=self.attacker).update(
            reporting_manager_id=self.manager
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Morning")
        self.other_shift = EmployeeShift.objects.create(employee_shift="Night")
        self.work_type = WorkType.objects.create(work_type="Office")
        self.other_work_type = WorkType.objects.create(work_type="Remote")

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker_user)

    # ---- ShiftRequest ----

    def test_shift_request_create_forces_server_employee_id(self):
        response = self.client.post(
            "/api/base/shift-requests/",
            {
                "employee_id": self.victim.id,  # spoof attempt
                "shift_id": self.shift.id,
                "requested_date": "2026-09-01",
                "requested_till": "2026-09-01",
                "description": "spoofed",
            },
        )
        self.assertEqual(response.status_code, 201)
        created = ShiftRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)

    def test_shift_request_detail_denies_non_owner(self):
        victim_request = ShiftRequest.objects.create(
            employee_id=self.victim, shift_id=self.shift
        )
        response = self.client.get(
            f"/api/base/shift-requests/{victim_request.id}/"
        )
        self.assertIn(response.status_code, (400, 403))

    def test_shift_request_detail_allows_owner(self):
        own_request = ShiftRequest.objects.create(
            employee_id=self.attacker, shift_id=self.shift
        )
        response = self.client.get(f"/api/base/shift-requests/{own_request.id}/")
        self.assertEqual(response.status_code, 200)

    def test_individual_shift_request_detail_denies_non_owner(self):
        victim_request = ShiftRequest.objects.create(
            employee_id=self.victim, shift_id=self.shift
        )
        response = self.client.get(
            f"/api/base/individual-shift-request/{victim_request.id}"
        )
        self.assertIn(response.status_code, (400, 403))

    # ---- WorkTypeRequest ----

    def test_work_type_request_create_forces_server_employee_id(self):
        response = self.client.post(
            "/api/base/worktype-requests/",
            {
                "employee_id": self.victim.id,  # spoof attempt
                "work_type_id": self.work_type.id,
                "requested_date": "2026-09-01",
                "requested_till": "2026-09-01",
                "description": "spoofed",
            },
        )
        self.assertEqual(response.status_code, 201, response.data)
        created = WorkTypeRequest.objects.get(id=response.data["id"])
        self.assertEqual(created.employee_id_id, self.attacker.id)

    def test_work_type_request_detail_denies_non_owner(self):
        victim_request = WorkTypeRequest.objects.create(
            employee_id=self.victim, work_type_id=self.work_type
        )
        response = self.client.get(
            f"/api/base/worktype-requests/{victim_request.id}/"
        )
        self.assertIn(response.status_code, (400, 403))

    def test_work_type_request_detail_allows_owner(self):
        own_request = WorkTypeRequest.objects.create(
            employee_id=self.attacker, work_type_id=self.work_type
        )
        response = self.client.get(
            f"/api/base/worktype-requests/{own_request.id}/"
        )
        self.assertEqual(response.status_code, 200)

    def test_individual_work_type_request_detail_denies_non_owner(self):
        victim_request = WorkTypeRequest.objects.create(
            employee_id=self.victim, work_type_id=self.work_type
        )
        response = self.client.get(
            f"/api/base/individual-worktype-request/{victim_request.id}"
        )
        self.assertIn(response.status_code, (400, 403))
