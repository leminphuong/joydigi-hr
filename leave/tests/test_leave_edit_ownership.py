"""Who may edit a leave request — 2026-11-14 (P1 found in the review of the
P1 fixes, approved by the owner).

* An employee edits only their OWN leave that still waits for approval. A
  colleague's leave — even with its id — is not found (404), on the web form
  (GET and POST) and in the API; an ``employee_id`` sent by the client is
  never trusted (the leave stays the employee's own).
* The management form (HR web ``request-update`` / ``request-creation``, HR
  API ``PUT /api/leave/request/<pk>/``) is entered by any reporting manager:
  it now acts only on employees the caller may manage (base.access
  ``can_manage_employee``: superuser, check-in admin, holder of the leave
  permission, or the employee's own reporting manager) — for the leave being
  edited and for the employee it would be (re)assigned to. Refused: 403.
* Ownership and status are checked again under the row lock, in the same
  transaction as the save (no TOCTOU).

Fake data only. The race tests need PostgreSQL and are skipped on SQLite.
"""

import threading
from unittest import skipUnless

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_employee, make_user
from leave.models import AvailableLeave, LeaveRequest
from leave.tests.test_leave_balance_concurrency import HTMX, monday_in, slow
from leave.tests.test_leave_cancellation import grant
from leave.tests.test_leave_p1_fixes import P1World


class OwnershipWorld(P1World):
    def build(self):
        super().build()
        # the outsider manages the colleague (not the member)
        EmployeeWorkInformation.objects.filter(employee_id=self.colleague).update(reporting_manager_id=self.outsider)
        self.far_user = make_user("race_far_emp", password="x-Race-Far-1")
        self.far = make_employee(company=self.other_company, email="far@race.test", user=self.far_user)
        AvailableLeave.objects.create(employee_id=self.far, leave_type_id=self.monthly, available_days=12)

    def leave_of(self, employee, weeks=16, status="requested"):
        start = monday_in(weeks)
        return LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=employee, leave_type_id=self.monthly, start_date=start, end_date=start,
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=1,
            approved_available_days=0, approved_carryforward_days=0, description="gốc", status=status,
        )])[0]

    def row(self, request):
        return LeaveRequest._base_manager.get(pk=request.pk)

    def payload(self, request, description, employee_id=None):
        body = self.edit_payload(request, description)
        if employee_id is not None:
            body["employee_id"] = employee_id
        return body

    # employee web form
    def web_edit(self, user, request, description="sửa", employee_id=None):
        return self.web(user).post(
            f"/leave/user-request-update/{request.pk}/", self.payload(request, description, employee_id), **HTMX
        )

    # employee API
    def api_edit(self, user, request, description="sửa API", employee_id=None):
        return self.api(user).put(
            f"/api/leave/user-request/{request.pk}/", self.payload(request, description, employee_id), format="json"
        )

    # management web form / API
    def hr_web_edit(self, user, request, description="quản lý sửa", employee_id=None):
        return self.web(user).post(
            f"/leave/request-update/{request.pk}/", self.payload(request, description, employee_id), **HTMX
        )

    def hr_api_edit(self, user, request, description="quản lý sửa API", employee_id=None):
        return self.api(user).put(
            f"/api/leave/request/{request.pk}/", self.payload(request, description, employee_id), format="json"
        )


class EmployeeWebFormTests(OwnershipWorld, TestCase):
    def setUp(self):
        self.build()

    def test_an_employee_edits_their_own_pending_leave(self):
        mine = self.leave_of(self.member)
        self.web_edit(self.member_user, mine, "sửa đơn của tôi")
        self.assertEqual((self.row(mine).description, self.row(mine).employee_id_id), ("sửa đơn của tôi", self.member.pk))

    def test_a_colleagues_leave_is_not_found_even_with_its_id(self):
        theirs = self.leave_of(self.colleague)
        self.assertEqual(self.web(self.member_user).get(f"/leave/user-request-update/{theirs.pk}/", **HTMX).status_code, 404)
        response = self.web_edit(self.member_user, theirs, "sửa trộm")
        self.assertEqual(response.status_code, 404)
        self.assertEqual((self.row(theirs).description, self.row(theirs).employee_id_id), ("gốc", self.colleague.pk))

    def test_a_spoofed_employee_id_never_moves_the_leave(self):
        mine = self.leave_of(self.member)
        self.web_edit(self.member_user, mine, "đổi chủ", employee_id=self.colleague.pk)
        self.assertEqual(self.row(mine).employee_id_id, self.member.pk, "still the member's")
        theirs = self.leave_of(self.colleague, weeks=17)
        self.assertEqual(self.web_edit(self.member_user, theirs, "lấy đơn", employee_id=self.member.pk).status_code, 404)
        self.assertEqual(self.row(theirs).employee_id_id, self.colleague.pk)

    def test_an_approved_leave_is_not_edited_by_its_employee(self):
        mine = self.approved(2)
        self.web_edit(self.member_user, mine, "sửa sau khi duyệt")
        self.assertNotEqual(self.row(mine).description, "sửa sau khi duyệt")
        self.assertEqual(self.consistent(mine), ("approved", 2, 10))

    def test_another_companys_leave_is_not_found(self):
        far = self.leave_of(self.far)
        self.assertEqual(self.web_edit(self.member_user, far, "khác công ty").status_code, 404)
        self.assertEqual(self.row(far).description, "gốc")

    def test_an_owner_changed_meanwhile_is_refused_under_the_lock(self):
        from leave.balance_ops import save_edited_leave

        mine = self.leave_of(self.member)
        stale = self.row(mine)  # what the form read
        LeaveRequest._base_manager.filter(pk=mine.pk).update(employee_id=self.colleague)  # HR reassigned it
        stale.description = "sửa bản cũ"
        self.assertFalse(save_edited_leave(stale, only_if_status="requested", owner_id=self.member.pk))
        self.assertEqual(self.row(mine).description, "gốc")


class EmployeeApiTests(OwnershipWorld, TestCase):
    def setUp(self):
        self.build()

    def test_own_pending_leave_is_edited(self):
        mine = self.leave_of(self.member)
        response = self.api_edit(self.member_user, mine, "API sửa của tôi")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.row(mine).description, "API sửa của tôi")

    def test_a_colleagues_leave_is_not_found(self):
        theirs = self.leave_of(self.colleague)
        for call in (
            lambda: self.api(self.member_user).get(f"/api/leave/user-request/{theirs.pk}/"),
            lambda: self.api_edit(self.member_user, theirs, "API sửa trộm"),
            lambda: self.api(self.member_user).delete(f"/api/leave/user-request/{theirs.pk}/"),
        ):
            self.assertEqual(call().status_code, 404)
        self.assertEqual((self.row(theirs).description, self.row(theirs).employee_id_id), ("gốc", self.colleague.pk))

    def test_a_spoofed_employee_id_is_ignored(self):
        mine = self.leave_of(self.member)
        self.api_edit(self.member_user, mine, "API đổi chủ", employee_id=self.colleague.pk)
        self.assertEqual(self.row(mine).employee_id_id, self.member.pk)

    def test_an_approved_leave_is_refused(self):
        mine = self.approved(2)
        self.assertEqual(self.api_edit(self.member_user, mine, "API sau duyệt").status_code, 409)
        self.assertEqual(self.consistent(mine), ("approved", 2, 10))

    def test_another_companys_leave_is_not_found(self):
        far = self.leave_of(self.far)
        self.assertEqual(self.api_edit(self.member_user, far, "khác công ty").status_code, 404)


# The project's CBV login_required renders any refusal raised inside a web
# form view as its "went wrong" page with 404 when DEBUG is off (403 with
# DEBUG on): both are a refusal. What matters is that nothing changed.
REFUSED = (403, 404)


class ManagementFormTests(OwnershipWorld, TestCase):
    """The outsider is a reporting manager (of the colleague), so the
    management form lets them in; it must not act outside their team."""

    def setUp(self):
        self.build()

    def test_a_team_lead_can_not_edit_a_leave_outside_their_team(self):
        member_leave = self.leave_of(self.member)
        self.assertIn(self.hr_web_edit(self.outsider_user, member_leave, "ngoài nhóm").status_code, REFUSED)
        self.assertIn(
            self.web(self.outsider_user).get(f"/leave/request-update/{member_leave.pk}/", **HTMX).status_code,
            REFUSED, "the form is not even shown",
        )
        self.assertEqual(self.row(member_leave).description, "gốc")
        self.assertEqual(self.hr_api_edit(self.outsider_user, member_leave, "ngoài nhóm API").status_code, 403)
        self.assertEqual(self.row(member_leave).description, "gốc")

    def test_a_team_lead_still_edits_their_teams_pending_leave(self):
        team_leave = self.leave_of(self.colleague)
        self.assertLess(self.hr_web_edit(self.outsider_user, team_leave, "trong nhóm").status_code, 400)
        self.assertEqual(self.row(team_leave).description, "trong nhóm")
        self.assertEqual(self.hr_api_edit(self.outsider_user, team_leave, "trong nhóm API").status_code, 201)

    def test_a_team_lead_can_not_move_a_leave_to_someone_outside_their_team(self):
        team_leave = self.leave_of(self.colleague)
        self.assertIn(
            self.hr_web_edit(self.outsider_user, team_leave, "chuyển chủ", employee_id=self.member.pk).status_code,
            REFUSED,
        )
        self.assertEqual(
            self.hr_api_edit(self.outsider_user, team_leave, "chuyển chủ API", employee_id=self.member.pk).status_code,
            403,
        )
        self.assertEqual(self.row(team_leave).employee_id_id, self.colleague.pk)

    def test_a_team_lead_can_not_create_a_leave_for_someone_outside_their_team(self):
        before = LeaveRequest._base_manager.filter(employee_id=self.member).count()
        start = monday_in(18)
        response = self.web(self.outsider_user).post("/leave/request-creation/", {
            "employee_id": self.member.pk, "leave_type_id": self.monthly.pk,
            "start_date": str(start), "start_date_breakdown": "full_day",
            "end_date": str(start), "end_date_breakdown": "full_day", "description": "tạo hộ",
        }, **HTMX)
        self.assertIn(response.status_code, REFUSED)
        self.assertEqual(LeaveRequest._base_manager.filter(employee_id=self.member).count(), before)

    def test_hr_with_the_permission_keeps_editing_any_leave_of_the_company(self):
        hr_leave = self.leave_of(self.member)
        grant(self.hr_user, "add_leaverequest")
        self.assertLess(self.hr_web_edit(self.hr_user, hr_leave, "HR sửa").status_code, 400)
        self.assertEqual(self.row(hr_leave).description, "HR sửa")
        self.assertEqual(self.hr_api_edit(self.hr_user, hr_leave, "HR sửa API").status_code, 201)
        self.assertEqual(self.hr_api_edit(self.admin_user, hr_leave, "admin sửa API").status_code, 201)


@skipUnless(connection.vendor == "postgresql", "row locks need PostgreSQL (SQLite has none)")
class ConcurrentEditTests(OwnershipWorld, TransactionTestCase):
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

    def test_employee_api_edit_while_the_manager_approves(self):
        mine = self.ask(2)
        with slow(LeaveRequest):
            self.race(lambda: self.api_edit(self.member_user, mine, "API sửa đúng lúc duyệt"),
                      lambda: self.approve(self.manager_user, mine))
        status, taken, fund = self.consistent(mine)
        self.assertEqual(fund, 12 - taken)
        self.assertEqual(status == "approved", taken == 2, (status, taken, fund))

    def test_employee_web_edit_while_the_manager_approves(self):
        mine = self.ask(2)
        with slow(LeaveRequest):
            self.race(lambda: self.web_edit(self.member_user, mine, "web sửa đúng lúc duyệt"),
                      lambda: self.approve(self.manager_user, mine))
        status, taken, fund = self.consistent(mine)
        self.assertEqual(fund, 12 - taken)
        self.assertEqual(status == "approved", taken == 2, (status, taken, fund))

    def test_a_colleague_edits_while_the_manager_approves(self):
        mine = self.ask(2)
        with slow(LeaveRequest):
            results = self.race(lambda: self.web_edit(self.colleague_user, mine, "đồng nghiệp sửa"),
                                lambda: self.approve(self.manager_user, mine))
        self.assertEqual(getattr(results[0], "status_code", None), 404)
        row = self.row(mine)
        self.assertNotEqual(row.description, "đồng nghiệp sửa")
        self.assertEqual(self.consistent(mine), ("approved", 2, 10))
