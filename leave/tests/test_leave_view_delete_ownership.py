"""Who may see or delete a leave request — 2026-11-14 (P1 + P2 found in the
review of the edit-ownership fix, approved by the owner).

* ``user-request-bulk-delete/`` (the employee's "My leave requests" bulk
  delete) deleted every *pending* leave whose id the browser sent, whoever's
  it was. It now deletes only the caller's OWN leaves, only while they wait
  for approval, checked on the locked row in the same transaction as the
  delete. The ids from the browser are never trusted.
* ``user-request-one/<id>/`` showed any leave by id. It now shows the
  caller's own leave, or a leave of someone the caller may manage
  (HR/Admin/the reporting manager — the employee profile's leave tab uses
  this view too). Anything else is not found (404).
* The management views of the same kind — ``one-request-view/<id>/``,
  ``leave-requests-detail-view/<pk>/``, ``request-delete/<id>/``,
  ``leave-request-bulk-delete/`` — let in anybody who manages *somebody*
  and then acted on any leave. They now act only on employees the caller may
  manage (``base.access.can_manage_employee``) or, for viewing, a leave the
  caller approves as a step manager.
* HR/Admin keep their rights inside their company; nobody acts on a leave of
  another company.

Fake data only. The race tests need PostgreSQL and are skipped on SQLite.
"""

import json
import threading
from unittest import skipUnless

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings

from leave.models import LeaveRequest
from leave.tests.test_leave_balance_concurrency import HTMX, slow
from leave.tests.test_leave_cancellation import grant
from leave.tests.test_leave_edit_ownership import REFUSED, OwnershipWorld

SECRET = "lý do riêng tư"


class ViewDeleteWorld(OwnershipWorld):
    def private_leave_of(self, employee, weeks=16, status="requested"):
        leave = self.leave_of(employee, weeks=weeks, status=status)
        LeaveRequest._base_manager.filter(pk=leave.pk).update(description=f"{SECRET} {leave.pk}")
        return leave

    def exists(self, leave):
        return LeaveRequest._base_manager.filter(pk=leave.pk).exists()

    # employee web: "My leave requests"
    def user_bulk_delete(self, user, ids):
        body = ids if isinstance(ids, str) else json.dumps(ids)
        return self.web(user).post("/leave/user-request-bulk-delete/", {"ids": body}, **HTMX)

    def user_one(self, user, leave, instances_ids=None):
        query = f"?instances_ids={json.dumps(instances_ids)}" if instances_ids else ""
        return self.web(user).get(f"/leave/user-request-one/{leave.pk}/{query}", **HTMX)

    # management web
    def one_view(self, user, leave):
        return self.web(user).get(f"/leave/one-request-view/{leave.pk}/", **HTMX)

    def detail_view(self, user, leave):
        return self.web(user).get(f"/leave/leave-requests-detail-view/{leave.pk}/", **HTMX)

    def hr_delete(self, user, leave):
        return self.web(user).post(f"/leave/request-delete/{leave.pk}/", **HTMX)

    def hr_bulk_delete(self, user, ids):
        return self.web(user).post("/leave/leave-request-bulk-delete/", {"ids": json.dumps(ids)}, **HTMX)

    # management API
    def api_get(self, user, leave):
        return self.api(user).get(f"/api/leave/request/{leave.pk}/")

    def api_delete(self, user, leave):
        return self.api(user).delete(f"/api/leave/request/{leave.pk}/")

    def api_bulk_delete(self, user, leaves):
        return self.api(user).delete(
            "/api/leave/request-bulk-action/", {"leave_request_id": [x.pk for x in leaves]}, format="multipart"
        )

    def shows(self, response, leave):
        return response.status_code == 200 and f"{SECRET} {leave.pk}" in response.content.decode()


class EmployeeBulkDeleteTests(ViewDeleteWorld, TestCase):
    def setUp(self):
        self.build()

    def test_an_employee_deletes_their_own_pending_leaves(self):
        first, second = self.leave_of(self.member, 16), self.leave_of(self.member, 17)
        response = self.user_bulk_delete(self.member_user, [first.pk, second.pk])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.exists(first) or self.exists(second))
        self.assertEqual(self.fund(), 12, "a pending leave took no days, none come back")

    def test_a_colleagues_pending_leave_is_never_deleted(self):
        theirs = self.leave_of(self.colleague)
        self.user_bulk_delete(self.member_user, [theirs.pk])
        self.assertTrue(self.exists(theirs))

    def test_a_mixed_selection_deletes_only_the_callers_own(self):
        mine, theirs = self.leave_of(self.member, 16), self.leave_of(self.colleague, 17)
        self.user_bulk_delete(self.member_user, [mine.pk, theirs.pk])
        self.assertFalse(self.exists(mine))
        self.assertTrue(self.exists(theirs))

    def test_another_companys_leave_is_never_deleted(self):
        far = self.leave_of(self.far)
        self.user_bulk_delete(self.member_user, [far.pk])
        self.assertTrue(self.exists(far))

    def test_a_manager_can_not_use_the_employee_bulk_delete_on_their_team(self):
        team = self.leave_of(self.member)
        self.user_bulk_delete(self.manager_user, [team.pk])
        self.assertTrue(self.exists(team), "the employee page deletes one's own leaves only")

    def test_an_approved_leave_is_not_deleted_and_keeps_its_days(self):
        mine = self.approved(2)
        self.user_bulk_delete(self.member_user, [mine.pk])
        self.assertEqual(self.consistent(mine), ("approved", 2, 10))

    def test_rejected_and_cancelled_leaves_are_not_deleted(self):
        rejected = self.leave_of(self.member, 16, status="rejected")
        cancelled = self.leave_of(self.member, 17, status="cancelled")
        self.user_bulk_delete(self.member_user, [rejected.pk, cancelled.pk])
        self.assertTrue(self.exists(rejected) and self.exists(cancelled))

    def test_junk_ids_change_nothing_and_do_not_crash(self):
        mine = self.leave_of(self.member)
        for body in (
            '["abc", null, 99999999, {"id": 1}, [1]]', "not json", '{"ids": 1}', "12",
            '["\\u00b2", "\\u0661\\u0662", "1e3", 1.5, -3, 0]',  # "²".isdigit() is True, int("²") fails
            "[" * 20000 + "]" * 20000,  # nesting deep enough to exhaust the JSON parser
        ):
            response = self.user_bulk_delete(self.member_user, body)
            self.assertLess(response.status_code, 500, body[:40])
        self.assertTrue(self.exists(mine))

    def test_an_owner_changed_meanwhile_is_refused_under_the_lock(self):
        from leave.request_access import delete_waiting_leave

        mine = self.leave_of(self.member)
        stale = self.row(mine)  # what the view read
        LeaveRequest._base_manager.filter(pk=mine.pk).update(employee_id=self.colleague)  # HR reassigned it
        outcome = delete_waiting_leave(stale, lambda row: row.employee_id_id == self.member.pk)
        self.assertEqual(outcome, "refused")
        self.assertTrue(self.exists(mine))

    def test_a_leave_approved_meanwhile_is_refused_under_the_lock(self):
        from leave.request_access import delete_waiting_leave

        mine = self.ask(2)
        stale = self.row(mine)  # read while it still waited
        self.assertEqual(self.approve(self.manager_user, mine).status_code, 200)
        outcome = delete_waiting_leave(stale, lambda row: row.employee_id_id == self.member.pk)
        self.assertEqual(outcome, "decided")
        self.assertEqual(self.consistent(mine), ("approved", 2, 10))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class UserRequestOneTests(ViewDeleteWorld, TestCase):
    """``user-request-one/<id>/`` — the employee's leave modal, also opened
    from the leave tab of an employee profile."""

    def setUp(self):
        self.build()

    def test_an_employee_sees_their_own_leave(self):
        mine = self.private_leave_of(self.member)
        other_mine = self.private_leave_of(self.member, 17)
        self.assertTrue(self.shows(self.user_one(self.member_user, mine, [mine.pk, other_mine.pk]), mine),
                        "from the list, with previous / next")
        self.assertTrue(self.shows(self.user_one(self.member_user, mine), mine),
                        "from the employee profile's leave tab (no list): used to send the user to '/'")

    def test_a_colleagues_leave_is_not_found(self):
        theirs = self.private_leave_of(self.colleague)
        response = self.user_one(self.member_user, theirs)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(SECRET, response.content.decode())

    def test_another_companys_leave_is_not_found(self):
        far = self.private_leave_of(self.far)
        response = self.user_one(self.member_user, far)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(SECRET, response.content.decode())

    def test_a_missing_id_is_not_found_not_a_crash(self):
        self.assertEqual(self.web(self.member_user).get("/leave/user-request-one/99999999/", **HTMX).status_code, 404)

    def test_a_team_lead_sees_their_team_but_not_others(self):
        team, other = self.private_leave_of(self.colleague, 16), self.private_leave_of(self.member, 17)
        self.assertTrue(self.shows(self.user_one(self.outsider_user, team), team))
        response = self.user_one(self.outsider_user, other)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(SECRET, response.content.decode())

    def test_hr_and_admin_see_leaves_of_their_company(self):
        leave = self.private_leave_of(self.member)
        grant(self.hr_user, "view_leaverequest")
        self.assertTrue(self.shows(self.user_one(self.hr_user, leave), leave))
        self.assertTrue(self.shows(self.user_one(self.admin_user, leave), leave))

    def test_hr_of_another_company_does_not_see_the_leave(self):
        leave = self.private_leave_of(self.member)
        grant(self.far_hr_user, "view_leaverequest")
        response = self.user_one(self.far_hr_user, leave)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(SECRET, response.content.decode())

    def test_a_step_approver_who_manages_nobody_still_sees_the_leave(self):
        from joydigi.testkit import make_employee, make_user
        from leave.models import LeaveRequestConditionApproval

        stepper_user = make_user("race_stepper", password="x-Race-Stepper-1")
        stepper = make_employee(company=self.company, email="step@race.test", user=stepper_user)
        leave = self.private_leave_of(self.member)
        LeaveRequestConditionApproval.objects.create(leave_request_id=leave, sequence=1, manager_id=stepper)
        self.assertTrue(self.shows(self.user_one(stepper_user, leave), leave))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class ManagementViewTests(ViewDeleteWorld, TestCase):
    """``one-request-view`` / ``leave-requests-detail-view`` (web) and
    ``GET /api/leave/request/<pk>/``: let in by "manages somebody"."""

    def setUp(self):
        self.build()

    def test_a_team_lead_does_not_see_a_leave_outside_their_team(self):
        other = self.private_leave_of(self.member)
        for response in (self.one_view(self.outsider_user, other), self.detail_view(self.outsider_user, other)):
            self.assertNotIn(SECRET, response.content.decode())
            self.assertGreaterEqual(response.status_code, 300, "refused (403/404) or sent away")
        self.assertEqual(self.api_get(self.outsider_user, other).status_code, 403)

    def test_a_team_lead_sees_their_teams_leave(self):
        team = self.private_leave_of(self.colleague)
        self.assertTrue(self.shows(self.one_view(self.outsider_user, team), team))
        self.assertTrue(self.shows(self.detail_view(self.outsider_user, team), team))
        self.assertEqual(self.api_get(self.outsider_user, team).status_code, 200)
        team_of_manager = self.private_leave_of(self.member, 17)
        self.assertTrue(self.shows(self.one_view(self.manager_user, team_of_manager), team_of_manager))

    def test_hr_and_admin_see_any_leave_of_their_company(self):
        leave = self.private_leave_of(self.member)
        grant(self.hr_user, "view_leaverequest")
        for user in (self.hr_user, self.admin_user):
            self.assertTrue(self.shows(self.one_view(user, leave), leave))
            self.assertTrue(self.shows(self.detail_view(user, leave), leave))
            self.assertEqual(self.api_get(user, leave).status_code, 200)

    def test_hr_of_another_company_does_not_see_the_leave(self):
        leave = self.private_leave_of(self.member)
        grant(self.far_hr_user, "view_leaverequest")
        for response in (self.one_view(self.far_hr_user, leave), self.detail_view(self.far_hr_user, leave)):
            self.assertNotIn(SECRET, response.content.decode())
        self.assertIn(self.api_get(self.far_hr_user, leave).status_code, (400, 403, 404))

    def test_a_step_manager_of_a_multi_step_leave_still_sees_it(self):
        from leave.models import LeaveRequestConditionApproval

        leave = self.private_leave_of(self.member)
        LeaveRequestConditionApproval.objects.create(
            leave_request_id=leave, sequence=1, is_approved=False, manager_id=self.outsider
        )
        self.assertTrue(self.shows(self.one_view(self.outsider_user, leave), leave))
        self.assertTrue(self.shows(self.detail_view(self.outsider_user, leave), leave))
        # The API list already shows a step approver the leave; its detail
        # used to answer 403.
        self.assertEqual(self.api_get(self.outsider_user, leave).status_code, 200)


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class ClashAndTabTests(ViewDeleteWorld, TestCase):
    """Two more routes that list leave requests: the "clashes" tab of a
    leave's detail (``view-clashes/<pk>/``) and the leave tab of an employee
    profile (``individual-leave-tab-list/<pk>/``, ``leave-tab/<pk>/``)."""

    def setUp(self):
        self.build()
        from employee.models import Employee

        for employee, token in ((self.member, "ThanhVienBiMat"), (self.colleague, "DongNghiepNhom")):
            Employee._base_manager.filter(pk=employee.pk).update(employee_first_name=token)
        start = self.leave_of(self.member, 16)
        self.member_leave = start
        self.colleague_leave = self.leave_of(self.colleague, 16)  # same day: they clash

    def clashes(self, user, leave):
        return self.web(user).get(f"/leave/view-clashes/{leave.pk}/", **HTMX)

    def test_the_clash_list_shows_a_team_lead_only_their_reach(self):
        response = self.clashes(self.outsider_user, self.colleague_leave)
        self.assertLess(response.status_code, 400)
        self.assertNotIn("ThanhVienBiMat", response.content.decode(), "the member is not in the lead's team")
        outside = self.clashes(self.outsider_user, self.member_leave)
        self.assertNotIn("DongNghiepNhom", outside.content.decode(), "a leave outside the team shows no clashes")

    def test_the_clash_list_still_shows_admin_every_clash(self):
        response = self.clashes(self.admin_user, self.member_leave)
        self.assertIn("DongNghiepNhom", response.content.decode())

    def test_an_employee_can_not_open_a_colleagues_leave_tab(self):
        for url in (f"/leave/individual-leave-tab-list/{self.colleague.pk}/", f"/leave/leave-tab/{self.colleague.pk}/"):
            response = self.web(self.member_user).get(url, **HTMX)
            self.assertIn(response.status_code, REFUSED, url)

    def test_the_leave_tab_still_opens_for_its_employee_their_lead_and_hr(self):
        grant(self.hr_user, "view_leaverequest")
        for user, employee in ((self.member_user, self.member), (self.outsider_user, self.colleague),
                               (self.hr_user, self.member), (self.admin_user, self.colleague)):
            for url in (f"/leave/individual-leave-tab-list/{employee.pk}/", f"/leave/leave-tab/{employee.pk}/"):
                self.assertLess(self.web(user).get(url, **HTMX).status_code, 400, (user.username, url))


class BadInputTests(ViewDeleteWorld, TestCase):
    """Junk from the client is refused, never a 500."""

    def setUp(self):
        self.build()
        grant(self.hr_user, "delete_leaverequest")

    def test_junk_ids_in_the_management_bulk_delete(self):
        leave = self.leave_of(self.member)
        response = self.hr_bulk_delete(self.hr_user, ["²", "abc", None])
        self.assertLess(response.status_code, 500)
        self.assertTrue(self.exists(leave))
        response = self.api(self.hr_user).delete(
            "/api/leave/request-bulk-action/", {"leave_request_id": ["abc", "²"]}, format="multipart"
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.exists(leave))

    def test_a_junk_employee_id_in_the_api_edit(self):
        leave = self.leave_of(self.member)
        for junk in ("abc", "²", "1; drop"):
            response = self.hr_api_edit(self.hr_user, leave, "sửa rác", employee_id=junk)
            self.assertEqual(response.status_code, 400, junk)
        self.assertEqual(self.row(leave).employee_id_id, self.member.pk)

    def test_a_protected_leave_is_refused_with_409_not_500(self):
        from unittest import mock

        from django.db.models import ProtectedError

        leave = self.leave_of(self.member)
        with mock.patch.object(LeaveRequest, "delete", side_effect=ProtectedError("protected", set())):
            self.assertEqual(self.api_delete(self.hr_user, leave).status_code, 409)
            self.assertEqual(self.api_bulk_delete(self.hr_user, [leave]).status_code, 409)
            self.assertLess(self.hr_delete(self.hr_user, leave).status_code, 500)
            self.assertLess(self.hr_bulk_delete(self.hr_user, [leave.pk]).status_code, 500)
            self.assertLess(self.user_bulk_delete(self.member_user, [leave.pk]).status_code, 500)
        self.assertTrue(self.exists(leave))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class ManagementDeleteTests(ViewDeleteWorld, TestCase):
    """``request-delete/<id>/`` and ``leave-request-bulk-delete/`` (web),
    ``DELETE /api/leave/request/<pk>/`` and ``request-bulk-action/`` (API)."""

    def setUp(self):
        self.build()

    def test_a_team_lead_can_not_delete_a_leave_outside_their_team(self):
        other = self.leave_of(self.member, 16)
        self.hr_delete(self.outsider_user, other)
        self.assertTrue(self.exists(other), "request-delete")
        self.hr_bulk_delete(self.outsider_user, [other.pk])
        self.assertTrue(self.exists(other), "leave-request-bulk-delete")
        self.assertEqual(self.api_delete(self.outsider_user, other).status_code, 403)
        self.assertEqual(self.api_bulk_delete(self.outsider_user, [other]).status_code, 403)
        self.assertTrue(self.exists(other))

    def test_a_team_lead_deletes_their_teams_pending_leave(self):
        one, two = self.leave_of(self.colleague, 16), self.leave_of(self.colleague, 17)
        self.hr_delete(self.outsider_user, one)
        self.assertFalse(self.exists(one))
        self.hr_bulk_delete(self.outsider_user, [two.pk])
        self.assertFalse(self.exists(two))
        three = self.leave_of(self.colleague, 18)
        self.assertEqual(self.api_delete(self.outsider_user, three).status_code, 200)
        self.assertFalse(self.exists(three))

    def test_a_mixed_bulk_selection_deletes_only_inside_the_team(self):
        team, other = self.leave_of(self.colleague, 16), self.leave_of(self.member, 17)
        self.hr_bulk_delete(self.outsider_user, [team.pk, other.pk])
        self.assertFalse(self.exists(team))
        self.assertTrue(self.exists(other))

    def test_hr_with_the_permission_and_admin_keep_deleting_pending_leaves(self):
        grant(self.hr_user, "delete_leaverequest")
        one, two, three = (self.leave_of(self.member, w) for w in (16, 17, 18))
        self.hr_delete(self.hr_user, one)
        self.hr_bulk_delete(self.hr_user, [two.pk])
        self.assertEqual(self.api_bulk_delete(self.admin_user, [three]).status_code, 200)
        self.assertFalse(self.exists(one) or self.exists(two) or self.exists(three))

    def test_hr_of_another_company_deletes_nothing(self):
        grant(self.far_hr_user, "delete_leaverequest")
        leave = self.leave_of(self.member)
        self.hr_delete(self.far_hr_user, leave)
        self.hr_bulk_delete(self.far_hr_user, [leave.pk])
        self.assertIn(self.api_delete(self.far_hr_user, leave).status_code, (400, 403, 404))
        self.assertIn(self.api_bulk_delete(self.far_hr_user, [leave]).status_code, (400, 403, 404))
        self.assertTrue(self.exists(leave))

    def test_an_approved_leave_is_never_deleted_by_management(self):
        approved = self.approved(2)
        self.hr_delete(self.manager_user, approved)
        self.hr_bulk_delete(self.admin_user, [approved.pk])
        self.api_delete(self.admin_user, approved)
        self.assertEqual(self.consistent(approved), ("approved", 2, 10))


@override_settings(COMPANY_SCOPED_PERMISSIONS=True)
class CrossCompanyApiTests(ViewDeleteWorld, TestCase):
    """The company-scoped managers do not filter JWT requests (the company is
    chosen before DRF authenticates), so the API's own check must keep HR of
    another company out: a leave permission is for one's own companies."""

    def setUp(self):
        self.build()
        for codename in ("view_leaverequest", "change_leaverequest", "delete_leaverequest",
                         "view_leaveallocationrequest"):
            grant(self.far_hr_user, codename)

    def test_hr_of_another_company_can_not_read_edit_or_delete_a_leave(self):
        leave = self.private_leave_of(self.member)
        self.assertIn(self.api_get(self.far_hr_user, leave).status_code, (400, 403, 404))
        self.assertIn(self.hr_api_edit(self.far_hr_user, leave, "HR khác công ty sửa").status_code, (400, 403, 404))
        self.assertIn(self.api_delete(self.far_hr_user, leave).status_code, (400, 403, 404))
        self.assertTrue(self.exists(leave))
        self.assertTrue(self.row(leave).description.startswith(SECRET))

    def test_hr_of_another_company_can_not_create_a_leave_for_the_employee(self):
        from leave.tests.test_leave_balance_concurrency import monday_in

        grant(self.far_hr_user, "add_leaverequest")
        before = LeaveRequest._base_manager.filter(employee_id=self.member).count()
        start = monday_in(19)
        response = self.api(self.far_hr_user).post("/api/leave/request/", {
            "employee_id": self.member.pk, "leave_type_id": self.monthly.pk,
            "start_date": str(start), "start_date_breakdown": "full_day",
            "end_date": str(start), "end_date_breakdown": "full_day", "description": "tạo hộ khác công ty",
        }, format="json")
        self.assertIn(response.status_code, (400, 403, 404))
        self.assertEqual(LeaveRequest._base_manager.filter(employee_id=self.member).count(), before)

    def test_hr_of_another_company_can_not_read_an_allocation_request(self):
        allocation = self.allocation(2)
        response = self.api(self.far_hr_user).get(f"/api/leave/allocation-request/{allocation.pk}/")
        self.assertIn(response.status_code, (400, 403, 404))

    def test_hr_of_their_own_company_keeps_every_right(self):
        grant(self.hr_user, "view_leaverequest")
        grant(self.hr_user, "delete_leaverequest")
        leave = self.private_leave_of(self.member)
        self.assertEqual(self.api_get(self.hr_user, leave).status_code, 200)
        self.assertEqual(self.hr_api_edit(self.hr_user, leave, "HR cùng công ty sửa").status_code, 201)
        self.assertEqual(self.api_delete(self.hr_user, leave).status_code, 200)

    def multi_company_hr(self, role_here_has_the_right):
        """HR of the other company (their work company), with a company role
        assignment in this company too — an HR role, or only an employee
        role. Rights come from company-scoped groups, as in production."""
        from django.contrib.auth.models import Group, Permission

        from base.models import CompanyGroupAssignment
        from joydigi.testkit import make_employee, make_user

        user = make_user("race_multi_hr", password="x-Race-MultiHr-1")
        make_employee(company=self.other_company, email="multi@race.test", user=user)
        hr_group = Group.objects.create(name="race HR role")
        hr_group.permissions.add(*Permission.objects.filter(
            content_type__app_label="leave", codename__in=["view_leaverequest", "change_leaverequest", "delete_leaverequest"]
        ))
        employee_group = Group.objects.create(name="race employee role")
        CompanyGroupAssignment.objects.create(user=user, company=self.other_company, group=hr_group)
        CompanyGroupAssignment.objects.create(
            user=user, company=self.company, group=hr_group if role_here_has_the_right else employee_group
        )
        return user

    def test_an_hr_role_assigned_in_this_company_gives_hr_rights_here(self):
        from django.test import override_settings

        with override_settings(COMPANY_SCOPED_PERMISSIONS=True):
            user = self.multi_company_hr(role_here_has_the_right=True)
            leave = self.private_leave_of(self.member)
            self.assertEqual(self.api_get(user, leave).status_code, 200)
            self.assertEqual(self.api_delete(user, leave).status_code, 200)

    def test_only_an_employee_role_in_this_company_gives_no_hr_rights_here(self):
        from django.test import override_settings

        with override_settings(COMPANY_SCOPED_PERMISSIONS=True):
            user = self.multi_company_hr(role_here_has_the_right=False)
            leave = self.private_leave_of(self.member)
            self.assertEqual(self.api_get(user, leave).status_code, 403)
            self.assertEqual(self.hr_api_edit(user, leave, "vai trò nhân viên sửa").status_code, 403)
            self.assertEqual(self.api_delete(user, leave).status_code, 403)
            self.assertTrue(self.exists(leave))
            listed = self.api(user).get("/api/leave/request/").json()
            self.assertNotIn(leave.pk, [row["id"] for row in listed.get("results", [])])

    def test_the_api_lists_show_no_leave_of_another_company(self):
        mine_here = self.private_leave_of(self.member)
        allocation = self.allocation(2)
        leaves = self.api(self.far_hr_user).get("/api/leave/request/")
        self.assertEqual(leaves.status_code, 200)
        self.assertNotIn(mine_here.pk, [row["id"] for row in leaves.json()["results"]])
        grouped = self.api(self.far_hr_user).get("/api/leave/request/?groupby_field=status")
        self.assertNotIn(f"{SECRET} {mine_here.pk}", grouped.content.decode())
        allocations = self.api(self.far_hr_user).get("/api/leave/allocation-request/")
        self.assertEqual(allocations.status_code, 200)
        self.assertNotIn(allocation.pk, [row["id"] for row in allocations.json()["results"]])

    def test_the_api_lists_keep_what_hr_and_team_leads_may_see(self):
        grant(self.hr_user, "view_leaverequest")
        member_leave, colleague_leave = self.private_leave_of(self.member, 16), self.private_leave_of(self.colleague, 17)
        hr_ids = [row["id"] for row in self.api(self.hr_user).get("/api/leave/request/").json()["results"]]
        self.assertTrue({member_leave.pk, colleague_leave.pk} <= set(hr_ids))
        lead_ids = [row["id"] for row in self.api(self.outsider_user).get("/api/leave/request/").json()["results"]]
        self.assertIn(colleague_leave.pk, lead_ids)
        self.assertNotIn(member_leave.pk, lead_ids)

    def test_the_web_leave_list_keeps_what_hr_and_team_leads_may_see(self):
        from employee.models import Employee

        for employee, token in ((self.member, "ThanhVienBiMat"), (self.colleague, "DongNghiepNhom")):
            Employee._base_manager.filter(pk=employee.pk).update(employee_first_name=token)
        grant(self.hr_user, "view_leaverequest")
        self.leave_of(self.member, 16), self.leave_of(self.colleague, 17)
        hr = self.web(self.hr_user).get("/leave/request-filter/", **HTMX)
        self.assertEqual(hr.status_code, 200)
        self.assertIn("ThanhVienBiMat", hr.content.decode())
        self.assertIn("DongNghiepNhom", hr.content.decode())
        lead = self.web(self.outsider_user).get("/leave/request-filter/", **HTMX)
        self.assertEqual(lead.status_code, 200)
        self.assertIn("DongNghiepNhom", lead.content.decode())
        self.assertNotIn("ThanhVienBiMat", lead.content.decode())

    def test_with_company_scoping_switched_off_the_old_global_rule_applies(self):
        """The settings rollback switch makes permissions global again; the
        leave checks follow (no lock-out of HQ HR managing a subsidiary)."""
        leave = self.private_leave_of(self.member)
        with override_settings(COMPANY_SCOPED_PERMISSIONS=False):
            self.assertEqual(self.api_get(self.far_hr_user, leave).status_code, 200)
            ids = [row["id"] for row in self.api(self.far_hr_user).get("/api/leave/request/").json()["results"]]
            self.assertIn(leave.pk, ids)
            self.assertEqual(self.api_get(self.outsider_user, leave).status_code, 403, "a team lead stays in their team")


@skipUnless(connection.vendor == "postgresql", "row locks need PostgreSQL (SQLite has none)")
class ConcurrentDeleteTests(ViewDeleteWorld, TransactionTestCase):
    """A delete racing an approval: the leave ends either deleted with no
    days taken, or approved with its days taken — never an approved leave
    deleted by a path that may only delete pending ones."""

    def setUp(self):
        self.build()

    def race(self, *calls):
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def run(index, call):
            try:
                barrier.wait(timeout=30)
                results[index] = call()
            except Exception as error:
                results[index] = error
            finally:
                connections.close_all()

        threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), "a caller hung")
        return results

    def outcome(self, leave, approve_response):
        row = LeaveRequest._base_manager.filter(pk=leave.pk).first()
        if getattr(approve_response, "status_code", None) == 200:
            self.assertIsNotNone(row, "an approved leave was deleted by a pending-only delete")
            self.assertEqual(self.consistent(leave), ("approved", 2, 10))
        elif row is None:
            self.assertEqual(self.fund(), 12)
        else:
            self.assertEqual(self.consistent(leave)[2], 12 - self.consistent(leave)[1])

    def test_employee_bulk_delete_while_the_manager_approves(self):
        mine = self.ask(2)
        with slow(LeaveRequest):
            results = self.race(lambda: self.approve(self.manager_user, mine),
                                lambda: self.user_bulk_delete(self.member_user, [mine.pk]))
        self.outcome(mine, results[0])

    def test_management_delete_while_the_manager_approves(self):
        mine = self.ask(2)
        with slow(LeaveRequest):
            results = self.race(lambda: self.approve(self.manager_user, mine),
                                lambda: self.hr_delete(self.admin_user, mine))
        self.outcome(mine, results[0])

    def test_api_delete_while_the_manager_approves(self):
        mine = self.ask(2)
        with slow(LeaveRequest):
            results = self.race(lambda: self.approve(self.manager_user, mine),
                                lambda: self.api_delete(self.admin_user, mine))
        self.outcome(mine, results[0])
