"""Security fix 2026-11-13 — regression tests for the P0 findings of the
2026-11-13 audit (audit/reports/full_app_audit/03_BUG_REPORT.md).

Each attack first ran as a probe against a throwaway database and succeeded
(SEC-01 … SEC-05). These tests pin the fix: an ordinary employee, or a
manager acting outside their own team, is refused; the requester, the
employee's own reporting manager, a permission holder and a superuser keep
working. Fake data only.
"""

import datetime

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance, AttendanceActivity, AttendanceLateComeEarlyOut
from base.models import EmployeeShift, ShiftRequest
from employee.models import Employee, EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from leave.models import AvailableLeave, LeaveAllocationRequest, LeaveType


def grant(user, app_label, codename):
    user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))


class SecurityBase(TestCase):
    def setUp(self):
        self.company = make_company("Sec Co")
        self.manager_user = make_user("sec_manager", password="x-Sec-Manager-1")
        self.manager = make_employee(company=self.company, email="m@sec.test", user=self.manager_user)
        self.member_user = make_user("sec_member", password="x-Sec-Member-1")
        self.member = make_employee(company=self.company, email="a@sec.test", user=self.member_user)
        self.other_user = make_user("sec_other", password="x-Sec-Other-1")
        self.other = make_employee(company=self.company, email="o@sec.test", user=self.other_user)
        # manager manages member only; other has no manager link to them
        EmployeeWorkInformation.objects.filter(employee_id=self.member).update(reporting_manager_id=self.manager)
        self.client = APIClient()

    def as_(self, user):
        # A real request loads the user afresh; so do we (no stale relations).
        self.client.force_authenticate(user=type(user).objects.get(pk=user.pk))
        return self.client


class ShiftRequestDeleteTests(SecurityBase):
    """SEC-01."""

    def setUp(self):
        super().setUp()
        self.shift = EmployeeShift.objects.create(employee_shift="Sec shift")

    def request_for(self, employee, approved=False):
        row = ShiftRequest.objects.create(employee_id=employee, shift_id=self.shift)
        if approved:
            ShiftRequest.objects.filter(pk=row.pk).update(approved=True)
        return row

    def test_an_employee_can_not_bulk_delete_someone_elses_requests(self):
        theirs = self.request_for(self.other)
        response = self.as_(self.member_user).delete(
            "/api/base/shift-request-bulk-delete", {"ids": [theirs.id]}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ShiftRequest.objects.filter(pk=theirs.pk).exists())

    def test_one_foreign_id_refuses_the_whole_batch(self):
        mine = self.request_for(self.member)
        theirs = self.request_for(self.other)
        response = self.as_(self.member_user).delete(
            "/api/base/shift-request-bulk-delete", {"ids": [mine.id, theirs.id]}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ShiftRequest.objects.filter(pk__in=[mine.pk, theirs.pk]).count(), 2)

    def test_the_requester_and_their_manager_still_can(self):
        mine = self.request_for(self.member)
        response = self.as_(self.member_user).delete(
            "/api/base/shift-request-bulk-delete", {"ids": [mine.id]}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        pending = self.request_for(self.member)
        response = self.as_(self.manager_user).delete(f"/api/base/shift-request-delete/{pending.id}")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ShiftRequest.objects.filter(pk=pending.pk).exists())

    def test_an_approved_request_is_not_deleted_one_by_one(self):
        # ShiftRequest.delete() keeps approved entries; the endpoint says so
        # instead of answering 200 for a delete that never happened.
        approved = self.request_for(self.member, approved=True)
        response = self.as_(self.manager_user).delete(f"/api/base/shift-request-delete/{approved.id}")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(ShiftRequest.objects.filter(pk=approved.pk).exists())

    def test_a_manager_can_not_delete_outside_their_team(self):
        theirs = self.request_for(self.other)
        response = self.as_(self.manager_user).delete(f"/api/base/shift-request-delete/{theirs.id}")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ShiftRequest.objects.filter(pk=theirs.pk).exists())

    def test_reallocation_needs_owner_or_manager(self):
        theirs = self.request_for(self.other)
        response = self.as_(self.member_user).post(f"/api/base/shift-request-allocation/{theirs.id}")
        self.assertEqual(response.status_code, 403)

    def test_web_delete_is_scoped_too(self):
        from django.test import Client

        theirs = self.request_for(self.other)
        htmx = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "shift-container",
                "HTTP_HX_CURRENT_URL": "http://testserver/employee/shift-request-view/"}
        web = Client()
        web.force_login(self.member_user)
        web.post(f"/shift-request-delete/{theirs.id}/", **htmx)
        self.assertTrue(ShiftRequest.objects.filter(pk=theirs.pk).exists())
        web.force_login(self.other_user)
        web.post(f"/shift-request-delete/{theirs.id}/", **htmx)
        self.assertFalse(ShiftRequest.objects.filter(pk=theirs.pk).exists())


class LeaveAllocationTests(SecurityBase):
    """SEC-02 — the +365 days self-grant."""

    def setUp(self):
        super().setUp()
        self.leave_type = LeaveType.objects.create(name="Sec annual", total_days=12)
        for employee in (self.manager, self.member, self.other):
            AvailableLeave.objects.create(
                employee_id=employee, leave_type_id=self.leave_type,
                available_days=12, carryforward_days=0, total_leave_days=12,
            )

    def balance(self, employee):
        return AvailableLeave.objects.get(employee_id=employee, leave_type_id=self.leave_type).available_days

    def ask(self, user, days):
        response = self.as_(user).post(
            "/api/leave/user-allocation-request/",
            {"leave_type_id": self.leave_type.id, "requested_days": days, "description": "sec"},
        )
        return response, LeaveAllocationRequest.objects.filter(employee_id=user.employee_get).order_by("-id").first()

    def test_a_manager_can_not_approve_their_own_allocation(self):
        response, allocation = self.ask(self.manager_user, 365)
        self.assertEqual(response.status_code, 200)
        response = self.as_(self.manager_user).put(f"/api/leave/allocation-approve/{allocation.id}/", format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.balance(self.manager), 12)

    def test_a_manager_can_not_approve_outside_their_team(self):
        _response, allocation = self.ask(self.other_user, 5)
        response = self.as_(self.manager_user).put(f"/api/leave/allocation-approve/{allocation.id}/", format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.balance(self.other), 12)

    def test_the_direct_manager_approves_and_a_permission_holder_rejects(self):
        _response, allocation = self.ask(self.member_user, 2)
        response = self.as_(self.manager_user).put(f"/api/leave/allocation-approve/{allocation.id}/", format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.balance(self.member), 14)
        hr_user = make_user("sec_hr", password="x-Sec-Hr-1")
        make_employee(company=self.company, email="hr@sec.test", user=hr_user)
        grant(hr_user, "leave", "change_leaveallocationrequest")
        _response, other_allocation = self.ask(self.other_user, 1)
        response = self.as_(hr_user).put(f"/api/leave/allocation-reject/{other_allocation.id}/", format="json")
        self.assertEqual(response.status_code, 200)

    def test_allocation_requests_are_bounded(self):
        response, _allocation = self.ask(self.member_user, 1000)
        self.assertEqual(response.status_code, 400)

    def test_a_manager_files_allocations_only_for_their_team(self):
        response = self.as_(self.manager_user).post(
            "/api/leave/allocation-request/",
            {"employee_id": self.other.id, "leave_type_id": self.leave_type.id, "requested_days": 3, "description": "x"},
        )
        self.assertEqual(response.status_code, 403)
        response = self.as_(self.member_user).post(
            "/api/leave/allocation-request/",
            {"employee_id": self.other.id, "leave_type_id": self.leave_type.id, "requested_days": 3, "description": "x"},
        )
        self.assertEqual(response.status_code, 403)


class OvertimeApprovalTests(SecurityBase):
    """SEC-03."""

    def attendance_of(self, employee):
        today = datetime.date.today()
        row = Attendance(
            employee_id=employee, attendance_date=today,
            attendance_clock_in_date=today, attendance_clock_in=datetime.time(8, 0),
            attendance_clock_out_date=today, attendance_clock_out=datetime.time(20, 0),
        )
        row.save()
        return row

    def approved(self, row):
        return Attendance.objects.get(pk=row.pk).attendance_overtime_approve

    def test_an_employee_approves_nobodys_overtime(self):
        theirs = self.attendance_of(self.other)
        mine = self.attendance_of(self.member)
        for row in (theirs, mine):
            response = self.as_(self.member_user).put(f"/api/attendance/overtime-approve/{row.id}", format="json")
            self.assertEqual(response.status_code, 403)
            self.assertFalse(self.approved(row))

    def test_the_direct_manager_approves(self):
        row = self.attendance_of(self.member)
        response = self.as_(self.manager_user).put(f"/api/attendance/overtime-approve/{row.id}", format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.approved(row))

    def test_a_manager_does_not_approve_outside_their_team(self):
        row = self.attendance_of(self.other)
        response = self.as_(self.manager_user).put(f"/api/attendance/overtime-approve/{row.id}", format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.approved(row))

    def test_attendance_requests_are_filed_for_oneself_only(self):
        response = self.as_(self.member_user).post(
            "/api/attendance/attendance-request/",
            {"employee_id": self.other.id, "attendance_date": str(datetime.date.today())},
        )
        self.assertEqual(response.status_code, 403)


class ProfileEditTests(SecurityBase):
    """SEC-04."""

    def test_a_subordinate_can_not_edit_their_manager(self):
        response = self.as_(self.member_user).put(
            f"/api/employee/employees/{self.manager.id}/", {"address": "changed"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotEqual(Employee.objects.get(pk=self.manager.pk).address, "changed")

    def test_an_employee_edits_only_safe_fields_of_their_own_profile(self):
        response = self.as_(self.member_user).put(
            f"/api/employee/employees/{self.member.id}/", {"address": "home"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Employee.objects.get(pk=self.member.pk).address, "home")
        for field, value in (("email", "x@evil.test"), ("is_active", False), ("badge_id", "X1")):
            response = self.as_(self.member_user).put(
                f"/api/employee/employees/{self.member.id}/", {field: value}, format="json"
            )
            self.assertEqual(response.status_code, 403, field)
        self.assertEqual(Employee.objects.get(pk=self.member.pk).email, "a@sec.test")
        self.assertTrue(Employee.objects.get(pk=self.member.pk).is_active)

    def test_hr_still_edits_anyone(self):
        hr_user = make_user("sec_hr2", password="x-Sec-Hr-2")
        make_employee(company=self.company, email="hr2@sec.test", user=hr_user)
        grant(hr_user, "employee", "change_employee")
        response = self.as_(hr_user).put(
            f"/api/employee/employees/{self.other.id}/", {"address": "by hr"}, format="json"
        )
        self.assertEqual(response.status_code, 200)


class AttendanceVisibilityTests(SecurityBase):
    """SEC-05."""

    def setUp(self):
        super().setUp()
        today = datetime.date.today()
        for employee in (self.manager, self.member, self.other):
            AttendanceActivity.objects.create(
                employee_id=employee, attendance_date=today, clock_in_date=today, clock_in=datetime.time(8, 0)
            )

    def owners(self, user, path):
        response = self.as_(user).get(path)
        self.assertEqual(response.status_code, 200)
        return {row.get("employee_id") for row in response.data}

    def test_activity_is_scoped(self):
        self.assertEqual(self.owners(self.other_user, "/api/attendance/attendance-activity/"), {self.other.id})
        self.assertEqual(
            self.owners(self.manager_user, "/api/attendance/attendance-activity/"), {self.manager.id, self.member.id}
        )
        superuser = make_user("sec_root", password="x-Sec-Root-1", is_superuser=True)
        make_employee(company=self.company, email="root@sec.test", user=superuser)
        self.assertTrue(
            {self.manager.id, self.member.id, self.other.id}
            <= self.owners(superuser, "/api/attendance/attendance-activity/")
        )

    def test_late_early_rows_are_scoped(self):
        today = datetime.date.today()
        for employee in (self.member, self.other):
            attendance = Attendance(
                employee_id=employee, attendance_date=today,
                attendance_clock_in_date=today, attendance_clock_in=datetime.time(9, 30),
            )
            attendance.save()
            row = AttendanceLateComeEarlyOut(attendance_id=attendance, type="late_come")
            row.employee_id = employee
            row.save()
        self.assertEqual(self.owners(self.other_user, "/api/attendance/late-come-early-out-view/"), {self.other.id})


class GroupByFieldTests(SecurityBase):
    """groupby_field could follow any ORM path (SEC-07)."""

    def setUp(self):
        super().setUp()
        self.leave_type = LeaveType.objects.create(name="Sec group", total_days=10)
        self.as_(self.member_user).post(
            "/api/leave/user-allocation-request/",
            {"leave_type_id": self.leave_type.id, "requested_days": 1, "description": "g"},
        )

    def test_paths_to_secrets_are_refused(self):
        for path in (
            "employee_id__employee_work_info__reporting_manager_id__employee_user_id__password",
            "employee_id__employee_user_id__password",
            "employee_id__employee_bank_details__account_number",
            "description",  # a free-text column is fine…
        )[:3]:
            response = self.as_(self.member_user).get(f"/api/leave/user-allocation-request/?groupby_field={path}")
            self.assertEqual(response.status_code, 400, path)
            self.assertNotIn("pbkdf2", response.content.decode())

    def test_plain_and_listed_fields_still_group(self):
        for path in ("leave_type_id", "status", "employee_id__employee_work_info__department_id"):
            response = self.as_(self.member_user).get(f"/api/leave/user-allocation-request/?groupby_field={path}")
            self.assertEqual(response.status_code, 200, path)


class MailTemplateTests(SecurityBase):
    """Mail templates were rendered against real model instances."""

    def test_the_safe_context_keeps_placeholders_and_drops_traversal(self):
        from django import template

        from base.mail_context import mail_template_context

        body = "{{ instance.get_full_name }}|{{ instance.employee_user_id.password }}|{{ self.employee_user_id.password }}|{{ instance.employee_work_info.reporting_manager_id }}"
        member = Employee.objects.get(pk=self.member.pk)  # fresh: the manager link is set after creation
        rendered = template.Template(body).render(template.Context(mail_template_context(member, self.manager)))
        name, secret, own_secret, manager = rendered.split("|")
        self.assertEqual(name, self.member.get_full_name())
        self.assertEqual((secret, own_secret), ("", ""))
        self.assertEqual(manager, self.manager.get_full_name())

    def test_an_employee_can_not_mail_other_employees(self):
        response = self.as_(self.member_user).post(
            "/api/attendance/offline-employee-mail-send",
            {"employee_id": self.other.id, "subject": "s", "body": "{{ instance.employee_user_id.password }}"},
        )
        self.assertEqual(response.status_code, 403)

    def test_converted_templates_need_the_team_and_carry_no_secrets(self):
        from base.models import JoydigiMailTemplate

        mail = JoydigiMailTemplate.objects.create(title="t", body="{{ instance.get_full_name }}{{ instance.employee_user_id.password }}")
        refused = self.as_(self.manager_user).put(
            "/api/attendance/converted-mail-template", {"template_id": mail.id, "employee_id": self.other.id}, format="json"
        )
        self.assertEqual(refused.status_code, 403)
        allowed = self.as_(self.manager_user).put(
            "/api/attendance/converted-mail-template", {"template_id": mail.id, "employee_id": self.member.id}, format="json"
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.data, self.member.get_full_name())
