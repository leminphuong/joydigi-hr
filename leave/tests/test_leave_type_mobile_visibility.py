"""Phase LEAVE-7A.4: trace why a newly-created, active LeaveType
("Nghỉ chế độ") wasn't appearing in the Flutter create-leave dropdown.

Root cause (proven, not backend): `CreateLeaveScreen` never fetches
its own leave-type list — it only renders whatever `RequestsScreen`
passes in from `RequestsController`'s already-cached
`RequestsLoaded.leaveTypes`, which is fetched once per session (plus
after any request mutation). A type activated by an admin *after*
that first load stayed invisible until a pull-to-refresh, a request
mutation, or an app restart. No backend bug — confirmed here.

This file answers Section 3/4/8's exact questions from the backend
side: `GET /api/leave/leave-type/?is_active=true` already returns
every active, same-company `LeaveType` regardless of whether the
requesting employee has an `AvailableLeave` allocation for it — that
allocation is checked only at *submission* time
(`LeaveRequest.clean()`, `leave/models.py`), never at list time. That
existing rule is preserved here, not bypassed.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from joydigi.testkit import make_company, make_employee, make_user
from leave.models import AvailableLeave, LeaveType


class LeaveTypeMobileVisibilityTests(TestCase):
    """A — the exact reported scenario: an existing type plus a new
    one, same company, both active."""

    def setUp(self):
        self.company = make_company("Visibility Co")
        self.password = "secret123"
        self.user = make_user("visibility_emp", password=self.password)
        make_employee(
            company=self.company,
            email="visibility_emp@test.joydigi",
            user=self.user,
        )
        self.client = APIClient()
        login = self.client.post(
            "/api/auth/login/",
            {"username": "visibility_emp", "password": self.password},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['access']}"
        )

    def test_both_active_same_company_types_are_returned(self):
        existing = LeaveType.objects.create(name="Nghỉ phép năm", total_days=12)
        new_type = LeaveType.objects.create(
            name="Nghỉ chế độ", total_days=30, is_active=True
        )

        response = self.client.get("/api/leave/leave-type/?is_active=true")

        self.assertEqual(response.status_code, 200)
        names = {item["name"] for item in response.data["results"]}
        self.assertIn(existing.name, names)
        self.assertIn(new_type.name, names)

    def test_inactive_type_excluded(self):
        LeaveType.objects.create(
            name="Nghỉ chế độ (tắt)", total_days=30, is_active=False
        )
        response = self.client.get("/api/leave/leave-type/?is_active=true")
        names = {item["name"] for item in response.data["results"]}
        self.assertNotIn("Nghỉ chế độ (tắt)", names)

    def test_other_company_type_excluded(self):
        other_company = make_company("Other Co")
        # LeaveType.objects is a JoydigiCompanyManager — scoped by the
        # request-bound company context, never a client-supplied id;
        # constructing the row directly (bypassing that context) is
        # the correct way to simulate a genuinely foreign-company row.
        foreign = LeaveType(
            name="Loại nghỉ công ty khác", total_days=5, is_active=True
        )
        foreign.company_id = other_company
        foreign.save()

        response = self.client.get("/api/leave/leave-type/?is_active=true")
        names = {item["name"] for item in response.data["results"]}
        self.assertNotIn("Loại nghỉ công ty khác", names)

    def test_new_active_leave_type_returned_when_expected(self):
        new_type = LeaveType.objects.create(
            name="Nghỉ chế độ", total_days=30, is_active=True
        )
        response = self.client.get("/api/leave/leave-type/?is_active=true")
        names = {item["name"] for item in response.data["results"]}
        self.assertIn(new_type.name, names)


class LeaveTypeAllocationBehaviorTests(TestCase):
    """Section 8: allocation affects submission, NOT list visibility —
    the API must show every active company type regardless, and the
    existing submission-time check must stay intact, not be bypassed."""

    def setUp(self):
        self.company = make_company("Allocation Co")
        self.password = "secret123"
        self.user = make_user("allocation_emp", password=self.password)
        self.employee = make_employee(
            company=self.company,
            email="allocation_emp@test.joydigi",
            user=self.user,
        )
        self.client = APIClient()
        login = self.client.post(
            "/api/auth/login/",
            {"username": "allocation_emp", "password": self.password},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['access']}"
        )
        self.leave_type = LeaveType.objects.create(
            name="Nghỉ chế độ", total_days=30, is_active=True
        )

    def test_unallocated_type_still_appears_in_list(self):
        # No AvailableLeave row exists for this employee+type at all.
        self.assertFalse(
            AvailableLeave.objects.filter(
                employee_id=self.employee, leave_type_id=self.leave_type
            ).exists()
        )
        response = self.client.get("/api/leave/leave-type/?is_active=true")
        names = {item["name"] for item in response.data["results"]}
        self.assertIn(self.leave_type.name, names)

    def test_submitting_without_allocation_is_rejected(self):
        from datetime import date, timedelta

        response = self.client.post(
            "/api/leave/user-request/",
            {
                "leave_type_id": self.leave_type.id,
                "start_date": str(date.today() + timedelta(days=1)),
                "start_date_breakdown": "full_day",
                "end_date": str(date.today() + timedelta(days=1)),
                "end_date_breakdown": "full_day",
                "description": "test",
            },
            format="json",
        )
        # Existing, unmodified validation — never bypassed.
        self.assertEqual(response.status_code, 400)

    def test_submitting_with_allocation_succeeds(self):
        from datetime import date, timedelta

        AvailableLeave.objects.create(
            employee_id=self.employee,
            leave_type_id=self.leave_type,
            available_days=30,
            carryforward_days=0,
            total_leave_days=30,
        )
        response = self.client.post(
            "/api/leave/user-request/",
            {
                "leave_type_id": self.leave_type.id,
                "start_date": str(date.today() + timedelta(days=1)),
                "start_date_breakdown": "full_day",
                "end_date": str(date.today() + timedelta(days=1)),
                "end_date_breakdown": "full_day",
                "description": "test",
            },
            format="json",
        )
        self.assertIn(response.status_code, (200, 201))
