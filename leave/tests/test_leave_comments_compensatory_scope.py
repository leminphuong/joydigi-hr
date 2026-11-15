"""Comments, attachments, compensatory leave and approvals — who may act
(2026-11-14, the P1s left open by the view/delete ownership fix, approved by
the owner).

* Comments of a leave request, an allocation request and a compensatory
  leave request: only someone who may see the request (its employee, their
  reporting manager, HR/Admin of the employee's company, a step approver)
  reads, adds or attaches; a comment is deleted by its author or by someone
  who may manage the employee. An attachment is deleted only through its own
  comment, and only by them — the file ids sent by the browser used to be
  deleted whatever comment they belonged to (by anybody, for compensatory
  leave).
* Compensatory leave: a team lead approves, rejects, deletes and edits only
  requests of their team; HR/Admin of the company keep their rights;
  nobody decides their own request; a decision is taken on the locked row.
* Approvals through the API: a check-in admin approves only inside the
  companies where they hold that role (a JWT request has no selected
  company, so an admin role anywhere used to count everywhere); allocation
  approvals the same for HR.

Fake data only. The race test needs PostgreSQL and is skipped on SQLite.
"""

import threading
from unittest import skipUnless

from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings

from leave.models import (
    AvailableLeave,
    CompensatoryLeaveRequest,
    CompensatoryLeaverequestComment,
    LeaveAllocationRequest,
    LeaveallocationrequestComment,
    LeaveGeneralSetting,
    LeaveRequest,
    LeaverequestComment,
    LeaverequestFile,
    LeaveType,
)
from leave.tests.test_leave_balance_concurrency import HTMX, slow
from leave.tests.test_leave_cancellation import grant
from leave.tests.test_leave_edit_ownership import REFUSED
from leave.tests.test_leave_view_delete_ownership import ViewDeleteWorld

SECRET = "ghi chú riêng tư"


class CommentWorld(ViewDeleteWorld):
    def build(self):
        super().build()
        self.comp_type = LeaveType.objects.create(
            name="Nghi Bu", total_days=0, payment="paid", company_id=self.company, is_compensatory_leave=True
        )
        LeaveGeneralSetting.objects.create(compensatory_leave=True)

    # -- data ---------------------------------------------------------------
    def comment_on(self, model, request_obj, author, text=None):
        comment = model.objects.create(request_id=request_obj, employee_id=author, comment=text or f"{SECRET} {author.pk}")
        attachment = LeaverequestFile.objects.create(file="leave/attachments/race.txt")
        comment.files.add(attachment)
        return comment, attachment

    def allocation_of(self, employee, status="requested"):
        return LeaveAllocationRequest._base_manager.bulk_create([LeaveAllocationRequest(
            employee_id=employee, leave_type_id=self.monthly, requested_days=2, description="race", status=status,
        )])[0]

    def comp_of(self, employee, status="requested", days=1):
        return CompensatoryLeaveRequest._base_manager.bulk_create([CompensatoryLeaveRequest(
            employee_id=employee, leave_type_id=self.comp_type, requested_days=days,
            description=f"{SECRET} bù {employee.pk}", status=status,
        )])[0]

    def comp_fund(self, employee):
        row = AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id=self.comp_type).first()
        return (row.available_days + row.carryforward_days) if row else 0

    def comp_row(self, comp):
        return CompensatoryLeaveRequest._base_manager.filter(pk=comp.pk).first()

    def alive(self, model, obj):
        return model._base_manager.filter(pk=obj.pk).exists()

    def text_of(self, response):
        return response.content.decode(errors="replace")


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class LeaveCommentTests(CommentWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.leave_of(self.member)
        self.comment, self.attachment = self.comment_on(LeaverequestComment, self.leave, self.member)

    def view(self, user, leave=None):
        return self.web(user).get(f"/leave/leave-request-view-comment/{(leave or self.leave).pk}/", **HTMX)

    def add(self, user, text, leave=None):
        return self.web(user).post(f"/leave/leave-request-add-comment/{(leave or self.leave).pk}/", {"comment": text}, **HTMX)

    def test_the_employee_reads_and_adds_comments_on_their_own_leave(self):
        self.assertIn(SECRET, self.text_of(self.view(self.member_user)))
        self.add(self.member_user, "nhân viên bình luận")
        self.assertTrue(LeaverequestComment.objects.filter(request_id=self.leave, comment="nhân viên bình luận").exists())

    def test_a_colleague_neither_reads_nor_adds_comments(self):
        self.assertNotIn(SECRET, self.text_of(self.view(self.colleague_user)))
        response = self.add(self.colleague_user, "đồng nghiệp chen vào")
        self.assertNotIn(SECRET, self.text_of(response))
        self.assertFalse(LeaverequestComment.objects.filter(comment="đồng nghiệp chen vào").exists())

    def test_a_team_lead_outside_the_team_does_not_read_the_comments(self):
        self.assertNotIn(SECRET, self.text_of(self.view(self.outsider_user)))

    def test_the_reporting_manager_and_hr_keep_reading_and_adding(self):
        grant(self.hr_user, "view_leaverequestcomment")  # the comment permission, as before
        for user, text in ((self.manager_user, "quản lý bình luận"), (self.hr_user, "HR bình luận")):
            self.assertIn(SECRET, self.text_of(self.view(user)))
            self.add(user, text)
            self.assertTrue(LeaverequestComment.objects.filter(request_id=self.leave, comment=text).exists())

    def test_nobody_attaches_a_file_to_someone_elses_comment(self):
        own_leave = self.leave_of(self.colleague, 17)
        upload = SimpleUploadedFile("x.txt", b"x", content_type="text/plain")
        self.web(self.colleague_user).post(
            f"/leave/leave-request-view-comment/{own_leave.pk}/?comment_id={self.comment.pk}", {"files": upload}, **HTMX
        )
        self.assertEqual(self.comment.files.count(), 1, "the member's comment got the colleague's file")

    def test_a_team_lead_outside_the_team_does_not_delete_the_comment(self):
        self.web(self.outsider_user).get(f"/leave/leave-request-delete-comment/{self.comment.pk}/", **HTMX)
        self.web(self.outsider_user).get(f"/leave/compensatory-request-delete-comment/{self.comment.pk}/", **HTMX)
        self.assertTrue(self.alive(LeaverequestComment, self.comment))

    def test_an_attachment_is_deleted_only_through_its_own_comment(self):
        own_leave = self.leave_of(self.colleague, 17)
        own_comment, _ = self.comment_on(LeaverequestComment, own_leave, self.colleague)
        response = self.web(self.colleague_user).get(
            "/leave/delete-leave-comment-file/",
            {"ids": self.attachment.pk, "leave_id": own_leave.pk, "comment_id": own_comment.pk}, **HTMX,
        )
        self.assertLess(response.status_code, 500)
        self.assertTrue(self.alive(LeaverequestFile, self.attachment), "the member's file was deleted by a colleague")

    def test_the_author_deletes_their_own_attachment_and_comment(self):
        self.web(self.member_user).get(
            "/leave/delete-leave-comment-file/",
            {"ids": self.attachment.pk, "leave_id": self.leave.pk, "comment_id": self.comment.pk}, **HTMX,
        )
        self.assertFalse(self.alive(LeaverequestFile, self.attachment))
        self.web(self.member_user).get(f"/leave/leave-request-delete-comment/{self.comment.pk}/", **HTMX)
        self.assertFalse(self.alive(LeaverequestComment, self.comment))

    def test_the_reporting_manager_deletes_a_comment_on_their_teams_leave(self):
        self.web(self.manager_user).get(f"/leave/leave-request-delete-comment/{self.comment.pk}/", **HTMX)
        self.assertFalse(self.alive(LeaverequestComment, self.comment))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class AllocationCommentTests(CommentWorld, TestCase):
    def setUp(self):
        self.build()
        self.allocation = self.allocation_of(self.member)
        self.comment, self.attachment = self.comment_on(LeaveallocationrequestComment, self.allocation, self.member)

    def view(self, user):
        return self.web(user).get(f"/leave/allocation-request-view-comment/{self.allocation.pk}/", **HTMX)

    def test_the_employee_and_their_manager_read_and_add(self):
        for user, text in ((self.member_user, "nv cấp phép"), (self.manager_user, "ql cấp phép")):
            self.assertIn(SECRET, self.text_of(self.view(user)))
            self.web(user).post(f"/leave/allocation-request-add-comment/{self.allocation.pk}/", {"comment": text}, **HTMX)
            self.assertTrue(LeaveallocationrequestComment.objects.filter(comment=text).exists())

    def test_a_colleague_and_an_outside_lead_neither_read_nor_add(self):
        for user in (self.colleague_user, self.outsider_user):
            self.assertNotIn(SECRET, self.text_of(self.view(user)))
            self.web(user).post(f"/leave/allocation-request-add-comment/{self.allocation.pk}/", {"comment": "chen"}, **HTMX)
        self.assertFalse(LeaveallocationrequestComment.objects.filter(comment="chen").exists())

    def test_an_outside_lead_does_not_delete_the_comment_or_its_file(self):
        own = self.allocation_of(self.colleague)
        own_comment, _ = self.comment_on(LeaveallocationrequestComment, own, self.outsider)
        self.web(self.outsider_user).get(f"/leave/allocation-request-delete-comment/{self.comment.pk}/", **HTMX)
        self.web(self.outsider_user).get(
            "/leave/delete-allocation-comment-file/",
            {"ids": self.attachment.pk, "leave_id": own.pk, "comment_id": own_comment.pk}, **HTMX,
        )
        self.assertTrue(self.alive(LeaveallocationrequestComment, self.comment))
        self.assertTrue(self.alive(LeaverequestFile, self.attachment))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class CompensatoryCommentTests(CommentWorld, TestCase):
    def setUp(self):
        self.build()
        self.comp = self.comp_of(self.member)
        self.comment, self.attachment = self.comment_on(CompensatoryLeaverequestComment, self.comp, self.member)

    def view(self, user):
        return self.web(user).get(f"/leave/view-compensatory-leave-comment/{self.comp.pk}/", **HTMX)

    def test_the_employee_and_their_manager_read(self):
        for user in (self.member_user, self.manager_user):
            self.assertIn(SECRET, self.text_of(self.view(user)))

    def test_a_colleague_neither_reads_nor_adds(self):
        self.assertNotIn(SECRET, self.text_of(self.view(self.colleague_user)))
        self.web(self.colleague_user).post(
            f"/leave/create-compensatory-leave-comment/{self.comp.pk}/", {"comment": "chen bù"}, **HTMX
        )
        self.assertFalse(CompensatoryLeaverequestComment.objects.filter(comment="chen bù").exists())

    def test_anybody_used_to_delete_any_attachment_by_id(self):
        response = self.web(self.colleague_user).get(
            "/leave/delete-compensatory-comment-file/",
            {"ids": self.attachment.pk, "leave_id": self.comp.pk, "compensatory": "true"}, **HTMX,
        )
        self.assertLess(response.status_code, 500)
        self.assertTrue(self.alive(LeaverequestFile, self.attachment))

    def test_the_author_deletes_their_own_attachment(self):
        self.web(self.member_user).get(
            "/leave/delete-compensatory-comment-file/",
            {"ids": self.attachment.pk, "leave_id": self.comp.pk, "comment_id": self.comment.pk, "compensatory": "true"},
            **HTMX,
        )
        self.assertFalse(self.alive(LeaverequestFile, self.attachment))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class CompensatoryDecisionTests(CommentWorld, TestCase):
    def setUp(self):
        self.build()

    def approve_comp(self, user, comp):
        return self.web(user).get(f"/leave/approve-compensatory-leave/{comp.pk}/", **HTMX)

    def reject_comp(self, user, comp):
        return self.web(user).post(f"/leave/reject-compensatory-leave/{comp.pk}/", {"reason": "không hợp lệ"}, **HTMX)

    def test_a_team_lead_does_not_approve_outside_their_team(self):
        comp = self.comp_of(self.member)
        self.approve_comp(self.outsider_user, comp)
        self.assertEqual((self.comp_row(comp).status, self.comp_fund(self.member)), ("requested", 0))

    def test_the_reporting_manager_and_hr_approve(self):
        first, second = self.comp_of(self.member), self.comp_of(self.member)
        self.approve_comp(self.manager_user, first)
        self.assertEqual((self.comp_row(first).status, self.comp_fund(self.member)), ("approved", 1))
        grant(self.hr_user, "change_compensatoryleaverequest")
        self.approve_comp(self.hr_user, second)
        self.assertEqual((self.comp_row(second).status, self.comp_fund(self.member)), ("approved", 2))

    def test_nobody_approves_their_own_request(self):
        own = self.comp_of(self.manager)
        self.approve_comp(self.manager_user, own)
        self.assertEqual(self.comp_row(own).status, "requested")

    def test_a_team_lead_does_not_reject_outside_their_team(self):
        comp = self.comp_of(self.member)
        self.approve_comp(self.manager_user, comp)
        self.reject_comp(self.outsider_user, comp)
        self.assertEqual((self.comp_row(comp).status, self.comp_fund(self.member)), ("approved", 1))
        self.reject_comp(self.manager_user, comp)
        self.assertEqual((self.comp_row(comp).status, self.comp_fund(self.member)), ("rejected", 0))

    def test_a_team_lead_does_not_delete_outside_their_team(self):
        comp = self.comp_of(self.member)
        self.web(self.outsider_user).get(f"/leave/delete-compensatory-leave/{comp.pk}/", **HTMX)
        self.assertIsNotNone(self.comp_row(comp))
        self.web(self.member_user).get(f"/leave/delete-compensatory-leave/{comp.pk}/", **HTMX)
        self.assertIsNone(self.comp_row(comp))

    def test_a_colleague_does_not_see_the_request(self):
        comp = self.comp_of(self.member)
        for url in (f"/leave/compensatory-leave-individual-view/{comp.pk}/",
                    f"/leave/compensatory-detail-view/{comp.pk}/",
                    f"/leave/my-compensatory-detail-view/{comp.pk}/"):
            self.assertNotIn(SECRET, self.text_of(self.web(self.colleague_user).get(url, **HTMX)), url)
        self.assertIn(SECRET, self.text_of(self.web(self.member_user).get(
            f"/leave/my-compensatory-detail-view/{comp.pk}/", **HTMX)))
        self.assertIn(SECRET, self.text_of(self.web(self.manager_user).get(
            f"/leave/compensatory-detail-view/{comp.pk}/", **HTMX)))
        self.assertIn(SECRET, self.text_of(self.web(self.member_user).get(
            f"/leave/compensatory-leave-individual-view/{comp.pk}/?instances_ids=[{comp.pk}]&my_request=False", **HTMX)))

    def test_a_colleague_does_not_open_or_save_the_edit_form(self):
        comp = self.comp_of(self.member)
        response = self.web(self.colleague_user).get(f"/leave/update-compensatory-leave/{comp.pk}/", **HTMX)
        self.assertNotIn(SECRET, self.text_of(response))
        self.web(self.colleague_user).post(f"/leave/update-compensatory-leave/{comp.pk}/", {
            "employee_id": self.colleague.pk, "description": "lấy đơn bù",
        }, **HTMX)
        row = self.comp_row(comp)
        self.assertEqual((row.employee_id_id, row.description), (self.member.pk, f"{SECRET} bù {self.member.pk}"))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class CrossCompanyApprovalTests(CommentWorld, TestCase):
    """Approvals through the mobile API and the web allocation decisions."""

    def setUp(self):
        self.build()
        from base.models import CompanyGroupAssignment
        from base.roles import ADMIN_ROLE

        self.admin_group, _ = Group.objects.get_or_create(name=ADMIN_ROLE)
        self.admin_group.permissions.add(*Permission.objects.filter(
            content_type__app_label="leave",
            codename__in=["view_leaverequest", "change_leaverequest", "change_leaveallocationrequest"],
        ))
        # Admin of the other company only.
        CompanyGroupAssignment.objects.create(user=self.far_hr_user, company=self.other_company, group=self.admin_group)
        self.CompanyGroupAssignment = CompanyGroupAssignment

    def bulk_approve(self, user, leaves):
        return self.api(user).put(
            "/api/leave/request-bulk-action/", {"leave_request_id": [x.pk for x in leaves]}, format="multipart"
        )

    def test_an_admin_of_another_company_neither_approves_nor_rejects(self):
        leave = self.ask(2)
        self.assertEqual(self.approve(self.far_hr_user, leave).status_code, 403)
        self.assertEqual(self.bulk_approve(self.far_hr_user, [leave]).status_code, 403)
        self.assertEqual(self.reject(self.far_hr_user, leave).status_code, 403)
        self.assertEqual(self.consistent(leave), ("requested", 0, 12))

    def test_an_admin_of_this_company_approves(self):
        self.CompanyGroupAssignment.objects.create(user=self.hr_user, company=self.company, group=self.admin_group)
        leave = self.ask(2)
        self.assertEqual(self.approve(self.hr_user, leave).status_code, 200)
        self.assertEqual(self.consistent(leave), ("approved", 2, 10))

    def test_the_reporting_manager_still_approves_and_rejects(self):
        leave, other = self.ask(2), self.ask(1)
        self.assertEqual(self.approve(self.manager_user, leave).status_code, 200)
        self.assertEqual(self.reject(self.manager_user, other).status_code, 200)

    def test_hr_of_another_company_does_not_decide_an_allocation(self):
        grant(self.far_hr_user, "change_leaveallocationrequest")
        allocation = self.allocation_of(self.member)
        before = self.fund()
        self.assertEqual(self.api(self.far_hr_user).put(f"/api/leave/allocation-approve/{allocation.pk}/").status_code, 403)
        self.assertEqual(self.api(self.far_hr_user).put(f"/api/leave/allocation-reject/{allocation.pk}/").status_code, 403)
        self.assertEqual((LeaveAllocationRequest._base_manager.get(pk=allocation.pk).status, self.fund()), ("requested", before))

    def test_hr_of_this_company_decides_an_allocation(self):
        grant(self.hr_user, "change_leaveallocationrequest")
        allocation = self.allocation_of(self.member)
        before = self.fund()
        self.assertEqual(self.api(self.hr_user).put(f"/api/leave/allocation-approve/{allocation.pk}/").status_code, 200)
        self.assertEqual(self.fund(), before + 2)

    def test_hr_of_another_company_neither_files_an_allocation_nor_reads_the_balances(self):
        grant(self.far_hr_user, "add_leaveallocationrequest")
        grant(self.far_hr_user, "view_availableleave")
        before = LeaveAllocationRequest._base_manager.filter(employee_id=self.member).count()
        response = self.api(self.far_hr_user).post("/api/leave/allocation-request/", {
            "employee_id": self.member.pk, "leave_type_id": self.monthly.pk, "requested_days": 2,
            "description": "cấp hộ khác công ty",
        }, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(LeaveAllocationRequest._base_manager.filter(employee_id=self.member).count(), before)
        self.assertEqual(self.api(self.far_hr_user).get(f"/api/leave/employee-leave-type/{self.member.pk}/").status_code, 403)
        grant(self.hr_user, "view_availableleave")
        self.assertEqual(self.api(self.hr_user).get(f"/api/leave/employee-leave-type/{self.member.pk}/").status_code, 200)

    def test_on_the_web_nobody_approves_their_own_allocation(self):
        grant(self.hr_user, "delete_leaveallocationrequest")
        own = self.allocation_of(self.hr)
        self.web(self.hr_user).get(f"/leave/leave-allocation-request-approve/{own.pk}/", **HTMX)
        self.assertEqual(LeaveAllocationRequest._base_manager.get(pk=own.pk).status, "requested")
        team = self.allocation_of(self.member)
        self.web(self.manager_user).get(f"/leave/leave-allocation-request-approve/{team.pk}/", **HTMX)
        self.assertEqual(LeaveAllocationRequest._base_manager.get(pk=team.pk).status, "approved")

    def test_an_admin_of_this_company_still_approves_on_the_web(self):
        self.CompanyGroupAssignment.objects.create(user=self.hr_user, company=self.company, group=self.admin_group)
        leave = self.ask(2)
        self.web(self.hr_user).get(f"/leave/request-approve/{leave.pk}/", **HTMX)
        self.assertEqual(self.consistent(leave), ("approved", 2, 10))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class ReviewFollowUpTests(CommentWorld, TestCase):
    """Found in the independent review of this round."""

    def setUp(self):
        self.build()

    # -- compensatory: only a waiting request is deleted or edited ------------
    def test_an_approved_compensatory_request_is_neither_deleted_nor_edited(self):
        comp = self.comp_of(self.member, status="approved")
        for user in (self.member_user, self.manager_user):
            self.web(user).get(f"/leave/delete-compensatory-leave/{comp.pk}/", **HTMX)
            self.assertIsNotNone(self.comp_row(comp), user.username)
            response = self.web(user).get(f"/leave/update-compensatory-leave/{comp.pk}/", **HTMX)
            self.assertNotIn(SECRET, self.text_of(response), user.username)
        self.assertEqual(self.comp_row(comp).status, "approved")

    def test_rejecting_a_waiting_request_takes_no_days(self):
        granted, waiting = self.comp_of(self.member), self.comp_of(self.member)
        self.web(self.manager_user).get(f"/leave/approve-compensatory-leave/{granted.pk}/", **HTMX)
        self.assertEqual(self.comp_fund(self.member), 1)
        self.web(self.manager_user).post(f"/leave/reject-compensatory-leave/{waiting.pk}/", {"reason": "x"}, **HTMX)
        self.assertEqual((self.comp_row(waiting).status, self.comp_fund(self.member)), ("rejected", 1))

    # -- allocation requests ----------------------------------------------------
    def allocation_edit(self, user, allocation, employee, description):
        return self.web(user).post(f"/leave/leave-allocation-request-update/{allocation.pk}/", {
            "employee_id": employee.pk, "leave_type_id": self.monthly.pk, "requested_days": 9,
            "description": description,
        }, **HTMX)

    def test_a_colleague_neither_opens_nor_saves_an_allocation_request(self):
        allocation = self.allocation_of(self.member)
        response = self.web(self.colleague_user).get(f"/leave/leave-allocation-request-update/{allocation.pk}/", **HTMX)
        self.assertIn(response.status_code, REFUSED)
        self.allocation_edit(self.colleague_user, allocation, self.colleague, "lấy yêu cầu cấp phép")
        row = LeaveAllocationRequest._base_manager.get(pk=allocation.pk)
        self.assertEqual((row.employee_id_id, row.requested_days, row.description), (self.member.pk, 2, "race"))

    def test_the_employee_edits_their_own_waiting_allocation_request_only(self):
        allocation = self.allocation_of(self.member)
        self.allocation_edit(self.member_user, allocation, self.member, "sửa của tôi")
        self.assertEqual(LeaveAllocationRequest._base_manager.get(pk=allocation.pk).description, "sửa của tôi")
        approved = self.allocation_of(self.member, status="approved")
        self.allocation_edit(self.member_user, approved, self.member, "sửa sau duyệt")
        self.assertEqual(LeaveAllocationRequest._base_manager.get(pk=approved.pk).description, "race")

    def test_a_colleague_does_not_see_an_allocation_request(self):
        allocation = self.allocation_of(self.member)
        LeaveAllocationRequest._base_manager.filter(pk=allocation.pk).update(description=f"{SECRET} cấp phép")
        for url in (f"/leave/leave-allocation-request-single-view/{allocation.pk}/",
                    f"/leave/detail-leave-allocation-request/{allocation.pk}/",
                    f"/leave/leave-allocation-request-detail-view/{allocation.pk}/"):
            self.assertNotIn(SECRET, self.text_of(self.web(self.colleague_user).get(url, **HTMX)), url)
            self.assertIn(SECRET, self.text_of(self.web(self.member_user).get(url, **HTMX)), url)

    # -- penalties ------------------------------------------------------------
    def test_a_team_lead_does_not_fine_outside_their_team(self):
        from base.models import PenaltyAccounts

        leave = self.leave_of(self.member)
        body = {"employee_id": self.member.pk, "leave_type_id": self.monthly.pk, "minus_leaves": 1, "penalty_amount": 0}
        self.web(self.outsider_user).post(f"/leave/cut-penalty/{leave.pk}/", body, **HTMX)
        self.assertEqual(PenaltyAccounts.objects.filter(employee_id=self.member).count(), 0)
        self.web(self.manager_user).post(f"/leave/cut-penalty/{leave.pk}/", body, **HTMX)
        self.assertEqual(PenaltyAccounts.objects.filter(employee_id=self.member).count(), 1)

    # -- attendance dates for a compensatory request -----------------------------
    def test_attendance_dates_only_for_oneself_or_the_team(self):
        url = "/leave/get-leave-attendance-dates/"
        self.assertEqual(self.web(self.colleague_user).get(url, {"employee_id": self.member.pk}).content, b"")
        for junk in ("abc", "²", "99999999"):
            self.assertLess(self.web(self.member_user).get(url, {"employee_id": junk}).status_code, 500, junk)
        self.assertLess(self.web(self.member_user).get(url, {"employee_id": self.member.pk}).status_code, 400)

    def test_a_junk_or_foreign_request_id_in_the_compensatory_file_delete(self):
        for junk in ("abc", "99999999"):
            response = self.web(self.member_user).get(
                "/leave/delete-compensatory-comment-file/", {"ids": 1, "leave_id": junk, "compensatory": "true"}, **HTMX
            )
            self.assertLess(response.status_code, 500, junk)

    # -- comments: legitimate attach, step approver, company-scoped roles -------
    def test_the_author_and_the_manager_attach_files(self):
        leave = self.leave_of(self.member)
        comment, _ = self.comment_on(LeaverequestComment, leave, self.member)
        for user in (self.member_user, self.manager_user):
            upload = SimpleUploadedFile("y.txt", b"y", content_type="text/plain")
            self.web(user).post(
                f"/leave/leave-request-view-comment/{leave.pk}/?comment_id={comment.pk}", {"files": upload}, **HTMX
            )
        self.assertEqual(comment.files.count(), 3)

    def test_a_step_approver_reads_the_comments(self):
        from joydigi.testkit import make_employee, make_user
        from leave.models import LeaveRequestConditionApproval

        stepper_user = make_user("race_stepper2", password="x-Race-Stepper-2")
        stepper = make_employee(company=self.company, email="step2@race.test", user=stepper_user)
        leave = self.leave_of(self.member)
        self.comment_on(LeaverequestComment, leave, self.member)
        LeaveRequestConditionApproval.objects.create(leave_request_id=leave, sequence=1, manager_id=stepper)
        response = self.web(stepper_user).get(f"/leave/leave-request-view-comment/{leave.pk}/", **HTMX)
        self.assertIn(SECRET, self.text_of(response))

    def test_comment_rights_count_in_the_company_where_the_role_is_held(self):
        from base.models import CompanyGroupAssignment
        from joydigi.testkit import make_employee, make_user

        leave = self.leave_of(self.member)
        self.comment_on(LeaverequestComment, leave, self.member)
        reader_group = Group.objects.create(name="race comment readers")
        reader_group.permissions.add(Permission.objects.get(content_type__app_label="leave", codename="view_leaverequestcomment"))
        employee_group = Group.objects.create(name="race plain employees")
        user = make_user("race_multi_reader", password="x-Race-Reader-1")
        make_employee(company=self.company, email="reader@race.test", user=user)
        CompanyGroupAssignment.objects.create(user=user, company=self.other_company, group=reader_group)
        CompanyGroupAssignment.objects.create(user=user, company=self.company, group=employee_group)
        self.assertNotIn(SECRET, self.text_of(self.web(user).get(f"/leave/leave-request-view-comment/{leave.pk}/", **HTMX)))
        CompanyGroupAssignment.objects.create(user=user, company=self.company, group=reader_group)
        self.assertIn(SECRET, self.text_of(self.web(user).get(f"/leave/leave-request-view-comment/{leave.pk}/", **HTMX)))


@skipUnless(connection.vendor == "postgresql", "row locks need PostgreSQL (SQLite has none)")
@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class ConcurrentCompensatoryTests(CommentWorld, TransactionTestCase):
    def setUp(self):
        self.build()

    def test_two_approvals_at_once_grant_the_days_once(self):
        comp = self.comp_of(self.member)
        barrier = threading.Barrier(2)
        results = [None, None]

        def run(index, user):
            try:
                barrier.wait(timeout=30)
                results[index] = self.web(user).get(f"/leave/approve-compensatory-leave/{comp.pk}/", **HTMX)
            except Exception as error:
                results[index] = error
            finally:
                connections.close_all()

        with slow(CompensatoryLeaveRequest):
            threads = [threading.Thread(target=run, args=(0, self.manager_user)),
                       threading.Thread(target=run, args=(1, self.admin_user))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), "a caller hung")
        self.assertEqual((self.comp_row(comp).status, self.comp_fund(self.member)), ("approved", 1), results)
