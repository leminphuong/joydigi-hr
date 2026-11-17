"""Multi-step leave approval: who sees, who decides, what is recorded
(2026-11-17).

The incident: a 5-day "Nghỉ Chế Độ" leave of Nguyễn Văn Nam waits at step 1
(Đàm Văn Vũ, his reporting manager, a Director whose only role is the
default "Nhân viên") and step 2 (the company admin). Vũ saw no "Duyệt đơn"
menu and got a 403 on the approval hub: the hub and the menu let in roles
only, while the approval chain is chosen from the reporting manager and the
"Điều kiện duyệt nhiều cấp", whatever their role.

Now one rule (``leave.request_access.review_problem``) decides for the hub,
the menu, the pending count, the web views and the API: only the approver
of the current step; one step per action; who and when on the step; a
reason for every rejection; a decided step is never deleted or rebuilt.

Fake data only. Dates are weeks ahead so nothing depends on today.
"""

from datetime import date, timedelta

from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from base.models import CompanyGroupAssignment, Department, MultipleApprovalCondition, MultipleApprovalManagers
from base.roles import ADMIN_ROLE, can_open_approval_hub, ensure_standard_roles
from base.templatetags.basefilters import pending_checkin_approvals
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_audit.models import UserActivityLog
from leave import monthly_limit
from leave.balance_ops import save_edited_leave
from leave.models import AvailableLeave, LeaveRequest, LeaveRequestConditionApproval, LeaveType
from leave.request_access import actionable_leave_ids, current_step, review_problem


def monday_in(weeks):
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) + 7 * weeks)


def give_role(user, company, name):
    group = Group.objects.get(name=name)
    CompanyGroupAssignment.objects.get_or_create(user=user, company=company, group=group)
    CompanyGroupAssignment.sync_user_group_membership(user, group)


class MultiStepWorld:
    """Company A: Nam (member), Vũ (his reporting manager, role Nhân viên
    only), the company admin (role Quản trị viên, no superuser), a
    colleague with no relation to Nam. Company B: its own admin."""

    def build(self):
        ensure_standard_roles()
        self.company = make_company("Approval Co A")
        self.other_company = make_company("Approval Co B")
        self.admin_user = make_user("ms_admin", password="x-Ms-Admin-1")
        self.admin = make_employee(company=self.company, email="admin@ms.test", user=self.admin_user,
                                   first_name="ADMIN", last_name="JOYDIGI")
        give_role(self.admin_user, self.company, ADMIN_ROLE)
        self.vu_user = make_user("ms_vu", password="x-Ms-Vu-1")
        self.vu = make_employee(company=self.company, email="vu@ms.test", user=self.vu_user,
                                first_name="Vũ", last_name="Đàm Văn")
        self.nam_user = make_user("ms_nam", password="x-Ms-Nam-1")
        self.nam = make_employee(company=self.company, email="nam@ms.test", user=self.nam_user,
                                 first_name="Nam", last_name="Nguyễn Văn")
        EmployeeWorkInformation.objects.filter(employee_id=self.nam).update(reporting_manager_id=self.vu)
        self.nam.refresh_from_db()
        self.outsider_user = make_user("ms_outsider", password="x-Ms-Out-1")
        self.outsider = make_employee(company=self.company, email="out@ms.test", user=self.outsider_user)
        self.far_admin_user = make_user("ms_far_admin", password="x-Ms-Far-1")
        self.far_admin = make_employee(company=self.other_company, email="far@ms.test", user=self.far_admin_user)
        give_role(self.far_admin_user, self.other_company, ADMIN_ROLE)
        self.root_user = make_user("ms_root", password="x-Ms-Root-1", is_superuser=True)
        self.root = make_employee(company=self.other_company, email="root@ms.test", user=self.root_user)
        self.regime = LeaveType.objects.create(name="Nghỉ Chế Độ", total_days=30, payment="paid",
                                               company_id=self.company)
        AvailableLeave._base_manager.filter(employee_id=self.nam).delete()
        AvailableLeave.objects.create(employee_id=self.nam, leave_type_id=self.regime,
                                      available_days=30, carryforward_days=0)
        self._week = 0

    # -- data ---------------------------------------------------------------
    def file_leave(self, days=5, employee=None):
        """Filed the normal way (``save``), so the approval chain is built."""
        self._week += 1
        start = monday_in(self._week)
        return LeaveRequest.objects.create(
            employee_id=employee or self.nam, leave_type_id=self.regime,
            start_date=start, end_date=start + timedelta(days=days - 1),
            start_date_breakdown="full_day", end_date_breakdown="full_day",
            description="Nghỉ chế độ", status="requested",
        )

    def steps(self, leave):
        return list(LeaveRequestConditionApproval._base_manager.filter(leave_request_id=leave)
                    .order_by("sequence"))

    def fresh(self, leave):
        return LeaveRequest._base_manager.get(pk=leave.pk)

    def fund(self):
        row = AvailableLeave._base_manager.get(employee_id=self.nam, leave_type_id=self.regime)
        return row.available_days + row.carryforward_days

    # -- callers ------------------------------------------------------------
    def web(self, user):
        client = Client()
        client.force_login(user)
        return client

    def api(self, user):
        client = APIClient()
        client.force_authenticate(user=type(user).objects.get(pk=user.pk))
        return client

    def web_approve(self, user, leave):
        return self.web(user).post(reverse("request-approve", args=[leave.pk]))

    def hub_reject(self, user, leave, reason="Thiếu người thay ca"):
        return self.web(user).post(reverse("approval-hub-leave-reject", args=[leave.pk]), {"reason": reason})

    def api_approve(self, user, leave):
        return self.api(user).put(f"/api/leave/approve/{leave.pk}/", format="json")

    def api_reject(self, user, leave, reason="Thiếu người thay ca"):
        data = {"reason": reason} if reason is not None else {}
        return self.api(user).put(f"/api/leave/reject/{leave.pk}/", data, format="json")


class ChainTests(MultiStepWorld, TestCase):
    def setUp(self):
        self.build()

    def test_the_incident_chain_is_reporting_manager_then_admin(self):
        leave = self.file_leave()
        self.assertEqual([s.manager_id_id for s in self.steps(leave)], [self.vu.pk, self.admin.pk])
        self.assertEqual(current_step(leave).manager_id_id, self.vu.pk)


class ApprovalHubTests(MultiStepWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.file_leave()

    def test_the_step_one_approver_without_any_role_opens_the_hub_and_sees_the_leave(self):
        self.assertTrue(can_open_approval_hub(self.vu_user))
        response = self.web(self.vu_user).get(reverse("approval-hub"))
        self.assertEqual(response.status_code, 200)
        pending = response.context["my_pending_leaves"]
        self.assertEqual([item.pk for item in pending], [self.leave.pk])
        self.assertTrue(pending[0].can_act)
        self.assertTrue(pending[0].can_reject)
        self.assertFalse(response.context["show_other_sections"])
        self.assertContains(response, reverse("approval-hub-leave-reject", args=[self.leave.pk]))

    def test_the_menu_and_the_count_follow_the_same_rule(self):
        self.assertEqual(pending_checkin_approvals(self.vu_user), 1)
        response = self.web(self.vu_user).get(reverse("approval-hub"))
        self.assertContains(response, 'data-menu="Duyệt đơn"')

    def test_the_admin_count_does_not_include_a_leave_waiting_for_step_one(self):
        self.assertEqual(pending_checkin_approvals(self.admin_user), 0)
        response = self.web(self.admin_user).get(reverse("approval-hub"))
        self.assertEqual(response.context["my_pending_leaves"], [])
        history = {item.pk: item for item in response.context["leave_requests"]}
        self.assertFalse(history[self.leave.pk].can_act)
        self.assertEqual(history[self.leave.pk].current_approval.manager_id_id, self.vu.pk)

    def test_someone_with_no_role_and_no_step_is_still_refused(self):
        self.assertFalse(can_open_approval_hub(self.outsider_user))
        self.assertEqual(self.web(self.outsider_user).get(reverse("approval-hub")).status_code, 403)
        self.assertEqual(pending_checkin_approvals(self.outsider_user), 0)

    def test_an_approver_assigned_by_condition_sees_the_leave_too(self):
        department = Department.objects.create(department="Phòng Kế toán")
        department.company_id.add(self.company)
        EmployeeWorkInformation.objects.filter(employee_id=self.nam).update(department_id=department)
        self.nam = type(self.nam).objects.select_related("employee_work_info").get(pk=self.nam.pk)
        condition = MultipleApprovalCondition.objects.create(
            department=department, condition_field="requested_days", condition_operator="ge",
            condition_value="2", company_id=self.company,
        )
        MultipleApprovalManagers.objects.create(condition_id=condition, sequence=1, employee_id=self.outsider.pk)
        leave = self.file_leave()
        self.assertEqual(current_step(leave).manager_id_id, self.outsider.pk)
        response = self.web(self.outsider_user).get(reverse("approval-hub"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(leave.pk, [item.pk for item in response.context["my_pending_leaves"]])
        self.web_approve(self.outsider_user, leave)
        self.assertTrue(self.steps(leave)[0].is_approved)

    def test_another_selected_company_does_not_hide_an_assigned_leave(self):
        client = self.web(self.vu_user)
        session = client.session
        session["selected_company"] = self.other_company.pk
        session.save()
        response = client.get(reverse("approval-hub"))
        self.assertIn(self.leave.pk, [item.pk for item in response.context["my_pending_leaves"]])
        client.post(reverse("request-approve", args=[self.leave.pk]))
        self.assertTrue(self.steps(self.leave)[0].is_approved)

    def test_viewing_the_hub_changes_nothing(self):
        before = [(s.pk, s.is_approved, s.is_rejected) for s in self.steps(self.leave)]
        self.web(self.admin_user).get(reverse("approval-hub"))
        self.web(self.vu_user).get(reverse("approval-hub"))
        self.assertEqual([(s.pk, s.is_approved, s.is_rejected) for s in self.steps(self.leave)], before)
        self.assertEqual(self.fresh(self.leave).status, "requested")


class WebDecisionTests(MultiStepWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.file_leave()

    def test_step_one_then_step_two(self):
        self.web_approve(self.vu_user, self.leave)
        first, second = self.steps(self.leave)
        self.assertTrue(first.is_approved)
        self.assertEqual(first.acted_by_id, self.vu.pk)
        self.assertIsNotNone(first.acted_at)
        self.assertFalse(second.is_approved)
        self.assertEqual(self.fresh(self.leave).status, "requested")
        self.assertEqual(self.fund(), 30, "no days are taken before the last step")
        self.assertEqual(actionable_leave_ids(self.admin_user), [self.leave.pk])
        self.assertTrue(UserActivityLog.objects.filter(
            details__leave_id=self.leave.pk, details__step=1, details__result="ok").exists())

        self.web_approve(self.admin_user, self.leave)
        self.assertEqual(self.fresh(self.leave).status, "approved")
        self.assertEqual(self.fund(), 25)
        self.assertEqual(self.steps(self.leave)[1].acted_by_id, self.admin.pk)

    def test_approving_twice_changes_nothing_the_second_time(self):
        self.web_approve(self.vu_user, self.leave)
        acted_at = self.steps(self.leave)[0].acted_at
        self.web_approve(self.vu_user, self.leave)
        self.assertEqual(self.steps(self.leave)[0].acted_at, acted_at)
        self.assertFalse(self.steps(self.leave)[1].is_approved)

    def test_step_two_cannot_go_first(self):
        self.web_approve(self.admin_user, self.leave)
        self.assertFalse(any(s.is_approved for s in self.steps(self.leave)))
        self.assertEqual(review_problem(self.admin_user, self.leave), "Đơn này chưa được duyệt ở bước trước.")
        self.assertTrue(UserActivityLog.objects.filter(
            details__leave_id=self.leave.pk, details__result="refused").exists())

    def test_nobody_else_decides(self):
        for user in (self.outsider_user, self.far_admin_user, self.nam_user):
            self.web_approve(user, self.leave)
            self.hub_reject(user, self.leave)
        self.assertFalse(any(s.is_approved or s.is_rejected for s in self.steps(self.leave)))
        self.assertEqual(self.fresh(self.leave).status, "requested")

    def test_a_superuser_who_is_not_the_approver_does_not_skip_the_step(self):
        self.web_approve(self.root_user, self.leave)
        self.assertFalse(any(s.is_approved for s in self.steps(self.leave)))

    def test_reject_from_the_hub_needs_a_reason_and_records_the_step(self):
        self.hub_reject(self.vu_user, self.leave, reason="   ")
        self.assertEqual(self.fresh(self.leave).status, "requested")
        self.hub_reject(self.vu_user, self.leave, reason="Trùng lịch kiểm toán")
        leave = self.fresh(self.leave)
        self.assertEqual(leave.status, "rejected")
        self.assertEqual(leave.reject_reason, "Trùng lịch kiểm toán")
        first, second = self.steps(self.leave)
        self.assertTrue(first.is_rejected)
        self.assertEqual(first.acted_by_id, self.vu.pk)
        self.assertFalse(second.is_approved or second.is_rejected)
        self.assertEqual(self.fund(), 30)


class ApiDecisionTests(MultiStepWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.file_leave()

    def test_api_step_records_who_and_when_and_keeps_order(self):
        self.assertEqual(self.api_approve(self.admin_user, self.leave).status_code, 403)
        self.assertEqual(self.api_approve(self.vu_user, self.leave).status_code, 200)
        first = self.steps(self.leave)[0]
        self.assertTrue(first.is_approved)
        self.assertEqual(first.acted_by_id, self.vu.pk)
        self.assertIsNotNone(first.acted_at)
        self.assertEqual(self.fresh(self.leave).status, "requested")
        self.assertEqual(self.api_approve(self.vu_user, self.leave).status_code, 403, "no second step for Vũ")
        self.assertEqual(self.api_approve(self.admin_user, self.leave).status_code, 200)
        self.assertEqual(self.fresh(self.leave).status, "approved")
        self.assertEqual(self.fund(), 25)

    def test_a_superuser_no_longer_approves_every_step_at_once(self):
        self.assertEqual(self.api_approve(self.root_user, self.leave).status_code, 403)
        self.assertFalse(any(s.is_approved for s in self.steps(self.leave)))
        self.assertEqual(self.fresh(self.leave).status, "requested")

    def test_bulk_approval_never_skips_steps(self):
        # Vũ may decide step 1, but not in bulk (400); the superuser is not
        # the approver of the current step at all (403).
        for user, refused in ((self.vu_user, 400), (self.root_user, 403)):
            response = self.api(user).put(
                "/api/leave/request-bulk-action/", {"leave_request_id": [self.leave.pk]}, format="multipart"
            )
            self.assertEqual(response.status_code, refused)
        self.assertFalse(any(s.is_approved for s in self.steps(self.leave)))

    def test_api_reject_needs_a_reason_and_records_the_step(self):
        self.assertEqual(self.api_reject(self.vu_user, self.leave, reason=None).status_code, 400)
        self.assertEqual(self.fresh(self.leave).status, "requested")
        self.assertEqual(self.api_reject(self.vu_user, self.leave).status_code, 200)
        first = self.steps(self.leave)[0]
        self.assertTrue(first.is_rejected)
        self.assertEqual(first.acted_by_id, self.vu.pk)
        self.assertEqual(self.fresh(self.leave).reject_reason, "Thiếu người thay ca")

    def test_cross_company_admin_is_refused_by_the_api(self):
        self.assertEqual(self.api_approve(self.far_admin_user, self.leave).status_code, 403)
        self.assertEqual(self.api_reject(self.far_admin_user, self.leave).status_code, 403)

    def test_a_user_without_an_employee_record_gets_403_not_500(self):
        bare = make_user("ms_bare", password="x-Ms-Bare-1")
        self.assertEqual(self.api_approve(bare, self.leave).status_code, 403)


class StepHistoryTests(MultiStepWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.file_leave()

    def test_editing_the_note_after_step_one_keeps_the_steps(self):
        self.web_approve(self.vu_user, self.leave)
        before = [(s.pk, s.is_approved, s.acted_by_id, s.acted_at) for s in self.steps(self.leave)]
        leave = self.fresh(self.leave)
        leave.description = "Bổ sung giấy tờ"
        leave.save()
        self.assertEqual([(s.pk, s.is_approved, s.acted_by_id, s.acted_at) for s in self.steps(self.leave)], before)

    def test_dates_cannot_change_once_a_step_is_approved(self):
        self.web_approve(self.vu_user, self.leave)
        leave = self.fresh(self.leave)
        leave.end_date = leave.end_date - timedelta(days=1)
        with self.assertRaises(monthly_limit.StepDecidedEditError):
            leave.save()
        leave = self.fresh(self.leave)
        leave.end_date = leave.end_date - timedelta(days=1)
        self.assertFalse(save_edited_leave(leave))
        self.assertIn("ít nhất một bước", leave.edit_refusal)
        self.assertTrue(self.steps(self.leave)[0].is_approved)

    def test_leave_type_cannot_change_once_a_step_is_approved(self):
        other = LeaveType.objects.create(name="Nghỉ Phép Năm", total_days=12, payment="paid", company_id=self.company)
        AvailableLeave.objects.create(employee_id=self.nam, leave_type_id=other, available_days=12)
        self.web_approve(self.vu_user, self.leave)
        leave = self.fresh(self.leave)
        leave.leave_type_id = other
        with self.assertRaises(monthly_limit.StepDecidedEditError):
            leave.save()

    def test_before_any_decision_a_date_change_still_rebuilds_the_chain(self):
        leave = self.fresh(self.leave)
        leave.end_date = leave.start_date  # one day: below the default two-step rule
        leave.save()
        self.assertEqual(self.fresh(self.leave).status, "requested")

    def test_a_note_edit_before_any_decision_keeps_the_same_step_rows(self):
        before = [s.pk for s in self.steps(self.leave)]
        leave = self.fresh(self.leave)
        leave.description = "Sửa ghi chú"
        leave.save()
        self.assertEqual([s.pk for s in self.steps(self.leave)], before)


class EdgeCaseTests(MultiStepWorld, TestCase):
    """Found by the independent review (2026-11-17)."""

    def setUp(self):
        self.build()

    def test_a_waiting_leave_whose_every_step_is_approved_can_still_be_finished(self):
        # The admin has no reporting manager: the default rule makes the admin
        # the only approver of their own leave, and that step approves itself.
        AvailableLeave.objects.create(employee_id=self.admin, leave_type_id=self.regime, available_days=30)
        own = self.file_leave(employee=self.admin)
        self.assertTrue(self.steps(own) and all(s.is_approved for s in self.steps(own)))
        self.assertEqual(self.fresh(own).status, "requested")
        self.assertIsNotNone(review_problem(self.admin_user, own), "never one's own leave")
        # another admin of the company finishes it on the web ...
        second_user = make_user("ms_admin2", password="x-Ms-Admin-2")
        make_employee(company=self.company, email="admin2@ms.test", user=second_user)
        give_role(second_user, self.company, ADMIN_ROLE)
        self.web_approve(second_user, own)
        self.assertEqual(self.fresh(own).status, "approved")
        self.assertEqual(AvailableLeave._base_manager.get(employee_id=self.admin, leave_type_id=self.regime)
                         .available_days, 25)

    def test_a_superuser_finishes_such_a_leave_through_the_api(self):
        AvailableLeave.objects.create(employee_id=self.admin, leave_type_id=self.regime, available_days=30)
        own = self.file_leave(employee=self.admin)
        self.assertEqual(self.api_approve(self.root_user, own).status_code, 200)
        self.assertEqual(self.fresh(own).status, "approved")

    def test_the_hub_stays_open_after_the_last_step_of_the_approver(self):
        leave = self.file_leave()
        response = self.web_approve(self.vu_user, leave)
        self.assertTrue(can_open_approval_hub(self.vu_user))
        response = self.web(self.vu_user).get(reverse("approval-hub"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(leave.pk, [item.pk for item in response.context["leave_requests"]])
        self.assertEqual(response.context["my_pending_leaves"], [])

    def test_a_note_edit_keeps_the_day_count_the_approver_saw(self):
        leave = self.file_leave()
        self.web_approve(self.vu_user, leave)
        # as if the days had been counted differently when it was filed
        LeaveRequest._base_manager.filter(pk=leave.pk).update(requested_days=4)
        row = self.fresh(leave)
        row.description = "Bổ sung giấy tờ"
        row.save()
        self.assertEqual(self.fresh(leave).requested_days, 4)
        self.assertTrue(self.steps(leave)[0].is_approved)
