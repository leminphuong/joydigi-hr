"""The four P1 findings of the pre-commit review of 2026-11-13
(audit/reports/final_precommit_review_2026-11-13.md), pinned.

* P1-1 The web approval accepted a leave that was cancelled (withdrawn, or
  whose cancellation was approved): it came back "approved" and its days
  were taken again. Only a leave waiting for approval may be approved.
* P1-2 The web edit forms (employee and HR) saved the whole row from the
  copy read when the request started: an approval committed meanwhile was
  overwritten — the leave back to "requested", nothing recorded as taken,
  while the balance kept the deduction (lost days). An edit now re-reads the
  row under its lock, is refused if the status changed, and never writes the
  decision's fields (status, approved_*) from its stale copy.
* P1-3 Saving a leave updated the clash count of OTHER employees' overlapping
  leaves inside the decision's transaction, while it held its own row lock:
  two managers approving two overlapping leaves at once deadlocked on
  PostgreSQL (500). The clash count is derived data and is now updated after
  the commit.
* P1-4 A superuser could approve the cancellation of their own leave.

Concurrency tests need PostgreSQL (real threads, separate connections) and
are skipped on SQLite. Fake data only.
"""

import threading
from datetime import timedelta
from unittest import skipUnless

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from employee.models import EmployeeWorkInformation
from leave.models import AvailableLeave, LeaveRequest
from leave.tests.test_leave_balance_concurrency import HTMX, monday_in, slow
from leave.tests.test_leave_cancellation import CancellationWorld


class P1World(CancellationWorld):
    def build(self):
        super().build()
        # The colleague (managed by the outsider) gets the same fund.
        AvailableLeave._base_manager.filter(employee_id=self.colleague).delete()
        AvailableLeave.objects.create(employee_id=self.colleague, leave_type_id=self.monthly, available_days=12)

    def colleague_fund(self):
        row = AvailableLeave._base_manager.get(employee_id=self.colleague, leave_type_id=self.monthly)
        return row.available_days + row.carryforward_days

    def overlapping_pair(self, weeks=14):
        """One leave of the member and one of the colleague, on the same day."""
        start = monday_in(weeks)
        rows = LeaveRequest._base_manager.bulk_create([
            LeaveRequest(
                employee_id=who, leave_type_id=self.monthly, start_date=start, end_date=start,
                start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=1,
                approved_available_days=0, approved_carryforward_days=0, description="overlap",
                status="requested",
            )
            for who in (self.member, self.colleague)
        ])
        return rows[0], rows[1]

    def edit_payload(self, request, description="đã sửa"):
        row = LeaveRequest._base_manager.get(pk=request.pk)
        return {
            "employee_id": row.employee_id_id,
            "leave_type_id": row.leave_type_id_id,
            "start_date": str(row.start_date),
            "start_date_breakdown": row.start_date_breakdown,
            "end_date": str(row.end_date),
            "end_date_breakdown": row.end_date_breakdown,
            "description": description,
        }

    def employee_edit(self, request, description="đã sửa"):
        return self.web(self.member_user).post(
            f"/leave/user-request-update/{request.pk}/", self.edit_payload(request, description), **HTMX
        )

    def hr_edit(self, request, description="HR sửa"):
        return self.web(self.admin_user).post(
            f"/leave/request-update/{request.pk}/", self.edit_payload(request, description), **HTMX
        )

    def consistent(self, request):
        """The leave's record and the fund tell the same story."""
        row = LeaveRequest._base_manager.get(pk=request.pk)
        taken = (row.approved_available_days or 0) + (row.approved_carryforward_days or 0)
        return row.status, taken, self.fund()


class SequentialP1Tests(P1World, TestCase):
    def setUp(self):
        self.build()

    # -- P1-1 -------------------------------------------------------------------
    def test_p1_1_a_leave_whose_cancellation_was_approved_is_never_approved_again(self):
        request, waiting = self.asked(2)
        self.approve_cancellation(self.manager_user, waiting)
        self.web_approve(self.admin_user, request)  # e.g. from a list opened earlier
        self.assertEqual((self.status(request), self.fund()), ("cancelled", 12))

    def test_p1_1_a_withdrawn_leave_is_never_approved(self):
        pending = self.ask(2)
        self.cancel(pending)  # withdrawn by the employee
        self.web_approve(self.admin_user, pending)
        self.assertEqual((self.status(pending), self.fund()), ("cancelled", 12))

    def test_p1_1_a_rejected_leave_is_not_approved_from_the_web(self):
        request = self.ask(1)
        self.reject(self.manager_user, request)
        self.web_approve(self.admin_user, request)
        self.assertEqual((self.status(request), self.fund()), ("rejected", 12), "the API refuses it too")

    def test_p1_1_bulk_approve_skips_a_cancelled_leave(self):
        cancelled, pending = self.ask(1), self.ask(2)
        self.cancel(cancelled)
        self.web(self.admin_user).post(
            "/leave/leave-requests-bulk-approve/", {"ids": [cancelled.pk, pending.pk]}, **HTMX
        )
        self.assertEqual(self.status(cancelled), "cancelled")

    def test_p1_1_a_pending_leave_is_still_approved_from_the_web(self):
        request = self.ask(2)
        self.web_approve(self.admin_user, request)
        self.assertEqual((self.status(request), self.fund()), ("approved", 10))

    # -- P1-2 (one at a time: the edit arrives after the approval) ----------------
    def test_p1_2_the_employee_can_not_edit_an_approved_leave(self):
        request = self.approved(2)
        self.employee_edit(request, "đổi lý do sau khi duyệt")
        row = LeaveRequest._base_manager.get(pk=request.pk)
        self.assertNotEqual(row.description, "đổi lý do sau khi duyệt")
        self.assertEqual(self.consistent(request), ("approved", 2, 10))

    def test_p1_2_the_employee_still_edits_a_pending_leave(self):
        request = self.ask(2)
        self.employee_edit(request, "sửa khi còn chờ")
        row = LeaveRequest._base_manager.get(pk=request.pk)
        self.assertEqual((row.description, row.status), ("sửa khi còn chờ", "requested"))

    def test_p1_2_a_stale_edit_copy_never_overwrites_a_decision(self):
        from leave.balance_ops import save_edited_leave

        request = self.ask(2)
        stale = LeaveRequest._base_manager.get(pk=request.pk)  # what the form read
        self.approve(self.manager_user, request)
        stale.description = "sửa từ bản cũ"
        self.assertFalse(save_edited_leave(stale), "the status changed meanwhile: refused")
        self.assertEqual(self.consistent(request), ("approved", 2, 10))

    def test_p1_2_hr_edit_keeps_the_decision_fields_of_the_row(self):
        request = self.approved(2)
        response = self.hr_edit(request, "HR sửa lý do")
        self.assertLess(response.status_code, 500)
        row = LeaveRequest._base_manager.get(pk=request.pk)
        self.assertEqual(row.description, "HR sửa lý do")
        self.assertEqual(self.consistent(request), ("approved", 2, 10))

    # -- P1-4 -------------------------------------------------------------------
    def test_p1_4_a_superuser_never_decides_their_own_cancellation(self):
        AvailableLeave.objects.create(employee_id=self.admin, leave_type_id=self.monthly, available_days=12)
        own = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.admin, leave_type_id=self.monthly, start_date=monday_in(12), end_date=monday_in(12),
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=1,
            approved_available_days=1, description="own", status="approved")])[0]
        self.assertEqual(self.api(self.admin_user).put(f"/api/leave/cancel/{own.pk}/", format="json").status_code, 200)
        waiting = self.waiting_cancellation(own)
        for decide in (self.approve_cancellation, self.reject_cancellation):
            response = decide(self.admin_user, waiting)
            self.assertEqual(response.status_code, 403)
            self.assertIn("chính mình", response.json()["error"])
        self.assertEqual(self.status(own), "approved")
        self.assertEqual(self.api(self.admin_user).get("/api/leave/cancellation-requests/").json()["results"], [],
                         "not listed for the superuser either")
        page = self.web(self.admin_user).get("/leave/cancellation-requests/")
        self.assertNotContains(page, f"cancellation-{waiting.pk}")

    def test_p1_4_a_superuser_still_decides_other_peoples_cancellations(self):
        request, waiting = self.asked(2)
        self.assertEqual(self.approve_cancellation(self.admin_user, waiting).status_code, 200)
        self.assertEqual((self.status(request), self.fund()), ("cancelled", 12))

    # -- follow-up from the independent review (2026-11-13) -------------------
    def own_approved_leave(self):
        AvailableLeave.objects.create(employee_id=self.admin, leave_type_id=self.monthly, available_days=12)
        own = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.admin, leave_type_id=self.monthly, start_date=monday_in(15), end_date=monday_in(15),
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=1,
            approved_available_days=1, description="own", status="approved")])[0]
        AvailableLeave._base_manager.filter(employee_id=self.admin, leave_type_id=self.monthly).update(available_days=11)
        return own

    def admin_fund(self):
        return AvailableLeave._base_manager.get(employee_id=self.admin, leave_type_id=self.monthly).available_days

    def test_p1_4_nobody_rejects_their_own_approved_leave_to_get_its_days_back(self):
        own = self.own_approved_leave()
        response = self.api(self.admin_user).put(f"/api/leave/reject/{own.pk}/", format="json")
        self.assertEqual(response.status_code, 403)
        self.web_reject(self.admin_user, own)
        self.assertEqual((self.status(own), self.admin_fund()), ("approved", 11), "no refund to oneself")

    def test_p1_4_rejecting_someone_elses_approved_leave_still_refunds(self):
        request = self.approved(2)
        self.web_reject(self.admin_user, request)
        self.assertEqual((self.status(request), self.fund()), ("rejected", 12))

    def test_p1_2_hr_api_edit_of_a_decided_leave_is_refused(self):
        request = self.approved(2)
        payload = self.edit_payload(request, "HR API sửa")
        response = self.api(self.admin_user).put(f"/api/leave/request/{request.pk}/", payload, format="json")
        self.assertEqual(response.status_code, 400, "unchanged answer for a leave not waiting")
        self.assertEqual(self.consistent(request), ("approved", 2, 10))

    def test_p1_2_hr_api_still_edits_a_pending_leave(self):
        request = self.ask(2)
        payload = self.edit_payload(request, "HR API sửa khi chờ")
        response = self.api(self.admin_user).put(f"/api/leave/request/{request.pk}/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(LeaveRequest._base_manager.get(pk=request.pk).description, "HR API sửa khi chờ")


@skipUnless(connection.vendor == "postgresql", "row locks need PostgreSQL (SQLite has none)")
class ConcurrentP1Tests(P1World, TransactionTestCase):
    def setUp(self):
        self.build()
        EmployeeWorkInformation.objects.filter(employee_id=self.colleague).update(reporting_manager_id=self.outsider)

    def race(self, *calls):
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def run(index, call):
            try:
                barrier.wait(timeout=30)
                results[index] = call()
            except Exception as error:  # reported by the assertions
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

    def outcome(self, results):
        return [getattr(r, "status_code", f"{type(r).__name__}: {str(r)[:80]}") for r in results]

    # -- P1-3 -------------------------------------------------------------------
    def test_p1_3_two_managers_approve_overlapping_leaves_at_once(self):
        for attempt in range(3):
            mine, theirs = self.overlapping_pair(weeks=14 + attempt)
            with slow(LeaveRequest):
                results = self.race(lambda: self.approve(self.manager_user, mine),
                                    lambda: self.approve(self.outsider_user, theirs))
            self.assertEqual(self.outcome(results), [200, 200], f"attempt {attempt}: no deadlock")
        self.assertEqual((self.fund(), self.colleague_fund()), (9, 9))

    def test_p1_3_the_deferred_clash_count_still_reaches_other_leaves(self):
        mine, theirs = self.overlapping_pair(weeks=20)
        third = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.outsider, leave_type_id=self.monthly, start_date=mine.start_date, end_date=mine.end_date,
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=1,
            approved_available_days=0, approved_carryforward_days=0, description="third", status="requested",
            leave_clashes_count=0)])[0]
        with slow(LeaveRequest):
            results = self.race(lambda: self.approve(self.manager_user, mine),
                                lambda: self.approve(self.outsider_user, theirs))
        self.assertEqual(self.outcome(results), [200, 200])
        # Nobody saved the third leave: only the after-commit update counts it.
        self.assertEqual(LeaveRequest._base_manager.get(pk=third.pk).leave_clashes_count, 2)

    def test_p1_3_web_and_api_approve_overlapping_leaves_at_once(self):
        mine, theirs = self.overlapping_pair()
        with slow(LeaveRequest):
            results = self.race(lambda: self.web_approve(self.admin_user, mine),
                                lambda: self.approve(self.outsider_user, theirs))
        self.assertTrue(all(isinstance(r, object) and not isinstance(r, Exception) for r in results),
                        self.outcome(results))
        self.assertEqual((self.status(mine), self.status(theirs)), ("approved", "approved"))
        self.assertEqual((self.fund(), self.colleague_fund()), (11, 11))

    def test_p1_3_overlapping_cancellation_approvals_at_once(self):
        mine, theirs = self.overlapping_pair()
        self.approve(self.manager_user, mine)
        self.approve(self.outsider_user, theirs)
        self.cancel(mine)
        self.api(self.colleague_user).put(f"/api/leave/cancel/{theirs.pk}/", format="json")
        mine_waiting, theirs_waiting = self.waiting_cancellation(mine), self.waiting_cancellation(theirs)
        with slow(LeaveRequest):
            results = self.race(lambda: self.approve_cancellation(self.manager_user, mine_waiting),
                                lambda: self.approve_cancellation(self.outsider_user, theirs_waiting))
        self.assertEqual(self.outcome(results), [200, 200])
        self.assertEqual((self.fund(), self.colleague_fund()), (12, 12))

    # -- P1-2 (the real race) ----------------------------------------------------
    def test_p1_2_employee_edit_while_the_manager_approves(self):
        request = self.ask(2)
        with slow(LeaveRequest):
            self.race(lambda: self.employee_edit(request, "sửa đúng lúc duyệt"),
                      lambda: self.approve(self.manager_user, request))
        status, taken, fund = self.consistent(request)
        self.assertEqual(fund, 12 - taken, "what the fund lost is what the leave records")
        self.assertEqual(status == "approved", taken == 2, (status, taken, fund))

    def test_p1_2_hr_edit_while_the_manager_approves(self):
        request = self.ask(2)
        with slow(LeaveRequest):
            self.race(lambda: self.hr_edit(request, "HR sửa đúng lúc duyệt"),
                      lambda: self.approve(self.manager_user, request))
        status, taken, fund = self.consistent(request)
        self.assertEqual(fund, 12 - taken)
        self.assertEqual(status == "approved", taken == 2, (status, taken, fund))

    def test_p1_2_hr_api_edit_while_the_manager_approves(self):
        request = self.ask(2)
        payload = self.edit_payload(request, "HR API sửa đúng lúc duyệt")
        with slow(LeaveRequest):
            self.race(lambda: self.api(self.admin_user).put(f"/api/leave/request/{request.pk}/", payload, format="json"),
                      lambda: self.approve(self.manager_user, request))
        status, taken, fund = self.consistent(request)
        self.assertEqual(fund, 12 - taken)
        self.assertEqual(status == "approved", taken == 2, (status, taken, fund))
