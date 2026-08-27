"""Phase LEAVE-7A.2: admin "Loại nghỉ phép" page — reuses the existing
LeaveType CRUD (leave-type-list/, type-creation/, type-update/<id>/);
this only covers the phase's actual changes: the sidebar menu's
permission gate (`leave_type_accessibility`), `is_active` now being a
real, settable field on both `LeaveTypeForm`/`UpdateLeaveTypeForm`,
and Phase LEAVE-7A.1's `?is_active=true` mobile contract staying
correct against an admin-managed toggle (not just a directly-ORM-
flipped flag).

Deliberately unit-level (form classes + the sidebar accessibility
function directly) rather than posting through the full legacy HTMX
CBV chain — `leave/tests/test_*` elsewhere in this app takes the same
approach (see `test_allocation_approve.py`) precisely because these
views are designed to be loaded as HTMX fragments, not hit directly
with a plain test-client GET/POST.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import TestCase
from rest_framework.test import APIClient

from joydigi.testkit import make_company, make_employee, make_user
from leave.forms import LeaveTypeForm, UpdateLeaveTypeForm
from leave.models import LeaveType
from leave.sidebar import leave_type_accessibility


class LeaveTypeSidebarAccessibilityTests(TestCase):
    """The new "Loại nghỉ phép" menu entry must use the exact same
    permission the existing LeaveType CRUD views already require —
    never a newly-invented or broader one."""

    def _request_for(self, user):
        return SimpleNamespace(user=user)

    def test_user_with_view_leavetype_perm_sees_menu(self):
        user = MagicMock()
        user.has_perm.side_effect = lambda perm: perm == "leave.view_leavetype"
        self.assertTrue(
            leave_type_accessibility(self._request_for(user), {}, None)
        )
        user.has_perm.assert_called_with("leave.view_leavetype")

    def test_ordinary_employee_without_the_permission_does_not_see_menu(self):
        user = MagicMock()
        user.has_perm.return_value = False
        self.assertFalse(
            leave_type_accessibility(self._request_for(user), {}, None)
        )


class LeaveTypeFormActiveFieldTests(TestCase):
    """`is_active` used to be excluded from both forms (Section 5's
    exact concern) — now it's a real, settable field."""

    def _valid_minimal_data(self, **overrides):
        data = {
            "name": "Nghỉ chế độ",
            "payment": "unpaid",
            "payment_type": "unpaid",
            "period_in": "day",
            "count": 5,
            "total_days": 5,
            "limit_leave": "on",
            "reset": "",
            "carryforward_type": "no carryforward",
            "require_approval": "no",
            "require_attachment": "no",
            "exclude_company_leave": "no",
            "exclude_holiday": "no",
            "is_encashable": "",
            "is_compensatory_leave": "",
        }
        data.update(overrides)
        return data

    def test_is_active_is_no_longer_excluded_from_create_form(self):
        self.assertNotIn("is_active", LeaveTypeForm.Meta.exclude)
        form = LeaveTypeForm(data=self._valid_minimal_data(is_active="on"))
        self.assertIn("is_active", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertTrue(saved.is_active)

    def test_creating_with_is_active_unchecked_creates_a_disabled_type(self):
        form = LeaveTypeForm(data=self._valid_minimal_data())
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertFalse(saved.is_active)

    def test_is_active_is_no_longer_excluded_from_update_form(self):
        self.assertNotIn("is_active", UpdateLeaveTypeForm.Meta.exclude)
        leave_type = LeaveType.objects.create(name="Sẽ bị tắt", total_days=10)
        self.assertTrue(leave_type.is_active)

        form = UpdateLeaveTypeForm(
            data=self._valid_minimal_data(name="Sẽ bị tắt"),
            instance=leave_type,
        )
        self.assertIn("is_active", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertFalse(saved.is_active)

    def test_updating_with_is_active_checked_keeps_it_enabled(self):
        leave_type = LeaveType.objects.create(
            name="Vẫn hoạt động", total_days=10
        )
        form = UpdateLeaveTypeForm(
            data=self._valid_minimal_data(name="Vẫn hoạt động", is_active="on"),
            instance=leave_type,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertTrue(saved.is_active)


class LeaveTypeStatusDisplayTests(TestCase):
    def test_status_display_reflects_is_active(self):
        active = LeaveType.objects.create(name="Active Type", total_days=5)
        inactive = LeaveType.objects.create(
            name="Inactive Type", total_days=5, is_active=False
        )
        self.assertIn("Đang hoạt động", active.status_display())
        self.assertIn("Đã tắt", inactive.status_display())


class LeaveTypeMobileApiActiveFilterTests(TestCase):
    """Phase LEAVE-7A.1's `?is_active=true` contract, exercised against
    a type disabled through this phase's now-settable form field."""

    def setUp(self):
        self.company = make_company("Leave Type API Co")
        self.password = "secret123"
        self.user = make_user("leave_api_emp", password=self.password)
        make_employee(
            company=self.company,
            email="leave_api_emp@test.joydigi",
            user=self.user,
        )
        self.client = APIClient()
        login = self.client.post(
            "/api/auth/login/",
            {"username": "leave_api_emp", "password": self.password},
            format="json",
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['access']}"
        )

    def test_active_type_appears_inactive_type_excluded(self):
        active = LeaveType.objects.create(name="Nghỉ phép năm", total_days=12)
        inactive = LeaveType.objects.create(
            name="Nghỉ chế độ (đã tắt)", total_days=5, is_active=False
        )

        response = self.client.get("/api/leave/leave-type/?is_active=true")

        self.assertEqual(response.status_code, 200)
        names = {item["name"] for item in response.data["results"]}
        self.assertIn(active.name, names)
        self.assertNotIn(inactive.name, names)

    def test_disabling_via_update_form_removes_it_from_the_active_api_list(self):
        leave_type = LeaveType.objects.create(
            name="Sẽ bị tắt qua form", total_days=8
        )
        form = UpdateLeaveTypeForm(
            data={
                "name": leave_type.name,
                "payment": "unpaid",
                "payment_type": "unpaid",
                "period_in": "day",
                "count": 8,
                "total_days": 8,
                "limit_leave": "on",
                "reset": "",
                "carryforward_type": "no carryforward",
                "require_approval": "no",
                "require_attachment": "no",
                "exclude_company_leave": "no",
                "exclude_holiday": "no",
                "is_encashable": "",
                "is_compensatory_leave": "",
                # is_active deliberately omitted — an unchecked checkbox
                # is exactly how the real form disables a type.
            },
            instance=leave_type,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        leave_type.refresh_from_db()
        self.assertFalse(leave_type.is_active)

        response = self.client.get("/api/leave/leave-type/?is_active=true")
        names = {item["name"] for item in response.data["results"]}
        self.assertNotIn(leave_type.name, names)

        # History safety: the type row itself is untouched (no FK
        # break, no data loss, no delete) — only `is_active` changed.
        leave_type.refresh_from_db()
        self.assertEqual(leave_type.name, "Sẽ bị tắt qua form")
        self.assertEqual(leave_type.total_days, 8)
