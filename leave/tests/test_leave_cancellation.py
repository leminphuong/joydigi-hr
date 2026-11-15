"""Cancelling a leave — JOYDIGI policy fixed on 2026-11-13 — and the other
paths that move leave days, one at a time and at the same time.

* A pending leave is withdrawn; nothing was taken, nothing is given back.
* Asking to cancel an APPROVED leave changes nothing: the leave stays
  approved, its days stay taken, until the reporting manager or HR of the
  same company approves the request — then it is cancelled and gets back
  exactly what it recorded as taken, once. A rejected request changes
  nothing.
* Deleting a leave gives back what it took (all delete paths); the
  creation-time "no approval needed" path, penalties and compensatory leave
  move days under the same row locks.

``SequentialCancellationTests`` run on any database. ``ConcurrentCancellation
Tests`` need PostgreSQL (real threads, each with its own connection, released
together; one save() slowed down to hold the race open) and are skipped on
SQLite, which has no row locks.

Numbered like the request of 2026-11-13 ("Kiểm thử bắt buộc", 1–22). Fake
data only. Set LEAVE_CONTRACT_FIXTURES=<dir> to also write the JSON bodies the
app parses (contract test on the Flutter side).
"""

import json
import os
import threading
from datetime import date, timedelta
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from leave.models import AvailableLeave, LeaveCancellationRequest, LeaveRequest
from leave.tests.test_leave_balance_concurrency import World, monday_in, slow


def grant(user, codename, app_label="leave"):
    user.user_permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))


class CancellationWorld(World):
    """World + HR of the company, HR of another company, a manager of
    somebody else, and a colleague."""

    def build(self):
        super().build()
        self.hr_user = make_user("race_hr", password="x-Race-Hr-1")
        self.hr = make_employee(company=self.company, email="hr@race.test", user=self.hr_user)
        grant(self.hr_user, "change_leaverequest")
        self.other_company = make_company("Other Race Co")
        self.far_hr_user = make_user("race_far_hr", password="x-Race-FarHr-1")
        self.far_hr = make_employee(company=self.other_company, email="farhr@race.test", user=self.far_hr_user)
        grant(self.far_hr_user, "change_leaverequest")
        self.outsider_user = make_user("race_outsider", password="x-Race-Outsider-1")
        self.outsider = make_employee(company=self.company, email="out@race.test", user=self.outsider_user)
        self.colleague_user = make_user("race_colleague", password="x-Race-Colleague-1")
        self.colleague = make_employee(company=self.company, email="col@race.test", user=self.colleague_user)
        # the outsider manages the colleague, not the member
        EmployeeWorkInformation.objects.filter(employee_id=self.colleague).update(reporting_manager_id=self.outsider)

    def approved(self, days=2, leave_type=None):
        """A leave approved by the manager (days taken through the real path)."""
        request = self.ask(days, leave_type)
        response = self.approve(self.manager_user, request)
        assert response.status_code == 200, response.content
        return request

    def asked(self, days=2, leave_type=None):
        """An approved leave with a waiting cancellation request."""
        request = self.approved(days, leave_type)
        response = self.cancel(request)
        assert response.status_code == 200, response.content
        return request, self.waiting_cancellation(request)

    def leave(self, request):
        return LeaveRequest._base_manager.get(pk=request.pk)

    def cancellation(self, cancellation):
        return LeaveCancellationRequest._base_manager.get(pk=cancellation.pk)


class SequentialCancellationTests(CancellationWorld, TestCase):
    def setUp(self):
        self.build()

    # 1
    def test_01_a_pending_leave_is_withdrawn_and_nothing_moves(self):
        pending = self.ask(2)
        response = self.cancel(pending)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual((self.status(pending), self.fund()), ("cancelled", 12))
        deleted = self.ask(1)
        response = self.api(self.member_user).delete(f"/api/leave/user-request/{deleted.pk}/")
        self.assertEqual(response.status_code, 200, "the app's delete of a pending leave still works")
        self.assertFalse(LeaveRequest._base_manager.filter(pk=deleted.pk).exists())
        self.assertEqual(self.fund(), 12)

    # 2, 3
    def test_02_03_asking_keeps_the_leave_approved_and_its_days_taken(self):
        request = self.approved(2)
        response = self.cancel(request, reason="Đổi kế hoạch")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "cancellation_pending")
        self.assertEqual(body["cancellation"]["status"], "requested")
        self.assertEqual(body["cancellation"]["reason"], "Đổi kế hoạch")
        self.assertEqual((self.status(request), self.fund()), ("approved", 10))
        detail = self.api(self.member_user).get(f"/api/leave/user-request/{request.pk}/").json()
        self.assertEqual(detail["status"], "approved")
        self.assertEqual(detail["cancellation"]["status"], "requested")
        listed = self.api(self.member_user).get("/api/leave/user-request/").json()["results"]
        row = next(r for r in listed if r["id"] == request.pk)
        self.assertEqual(row["cancellation"]["status"], "requested")

    # 4, 9
    def test_04_09_the_manager_approves_once_resends_change_nothing(self):
        request, waiting = self.asked(2)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.approve_cancellation(self.manager_user, waiting, reason="Đồng ý")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.status(request), self.fund()), ("cancelled", 12))
        row = self.cancellation(waiting)
        self.assertEqual((row.status, row.decided_by_id, row.decision_reason), ("approved", self.manager.pk, "Đồng ý"))
        self.assertIsNotNone(row.decided_at)
        self.assertEqual((row.refunded_available_days, row.refunded_carryforward_days), (2, 0))
        from leave.models import LeaverequestComment

        self.assertTrue(LeaverequestComment.objects.filter(request_id=request.pk, comment__contains="chấp thuận").exists())
        from notifications.models import Notification

        self.assertTrue(Notification.objects.filter(recipient=self.member_user, verb__contains="chấp thuận").exists())
        for again in (self.approve_cancellation, self.reject_cancellation):
            response = again(self.admin_user, waiting)
            self.assertEqual(response.status_code, 409, "already decided")
            self.assertEqual(response.json()["code"], "already_decided")
        self.assertEqual(self.fund(), 12, "never twice")

    # 5
    def test_05_hr_of_the_same_company_approves(self):
        request, waiting = self.asked(1)
        self.assertEqual(self.approve_cancellation(self.hr_user, waiting).status_code, 200)
        self.assertEqual((self.status(request), self.fund()), ("cancelled", 12))

    # 6
    def test_06_a_rejected_request_changes_nothing_and_may_be_asked_again(self):
        request, waiting = self.asked(2)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.reject_cancellation(self.manager_user, waiting, reason="Đang thiếu người")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.status(request), self.fund()), ("approved", 10))
        row = self.cancellation(waiting)
        self.assertEqual((row.status, row.decision_reason), ("rejected", "Đang thiếu người"))
        from notifications.models import Notification

        self.assertTrue(Notification.objects.filter(recipient=self.member_user, verb__contains="từ chối").exists())
        detail = self.api(self.member_user).get(f"/api/leave/user-request/{request.pk}/").json()
        self.assertEqual(detail["cancellation"]["status"], "rejected")
        self.assertEqual(detail["cancellation"]["decision_reason"], "Đang thiếu người")
        self.assertEqual(self.cancel(request).status_code, 200, "a new request after a rejection")

    # 10 (one at a time)
    def test_10_a_second_request_while_one_waits_is_refused(self):
        request, _waiting = self.asked(1)
        response = self.cancel(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "cancellation_pending")
        self.assertEqual(LeaveCancellationRequest._base_manager.filter(leave_request=request).count(), 1)

    # 11
    def test_11_people_without_the_right_are_refused(self):
        request, waiting = self.asked(2)
        for user in (self.outsider_user, self.colleague_user):
            response = self.approve_cancellation(user, waiting)
            self.assertEqual(response.status_code, 403, user.username)
        self.assertEqual(self.reject_cancellation(self.colleague_user, waiting).status_code, 403)
        self.assertEqual((self.status(request), self.fund(), self.cancellation(waiting).status), ("approved", 10, "requested"))
        self.assertEqual(self.api(self.colleague_user).put(f"/api/leave/cancel/{request.pk}/", format="json").status_code, 403)

    # 12
    def test_12_nobody_decides_their_own_cancellation(self):
        request, waiting = self.asked(1)
        response = self.approve_cancellation(self.member_user, waiting)
        self.assertEqual(response.status_code, 403)
        self.assertIn("chính mình", response.json()["error"])
        # HR asking to cancel their own leave can not approve it either.
        AvailableLeave.objects.create(employee_id=self.hr, leave_type_id=self.monthly, available_days=12)
        own = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.hr, leave_type_id=self.monthly, start_date=monday_in(9), end_date=monday_in(9),
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=1,
            approved_available_days=1, description="own", status="approved")])[0]
        self.assertEqual(self.api(self.hr_user).put(f"/api/leave/cancel/{own.pk}/", format="json").status_code, 200)
        mine = self.waiting_cancellation(own)
        self.assertEqual(self.approve_cancellation(self.hr_user, mine).status_code, 403)
        self.assertEqual(self.status(own), "approved")

    # 13
    def test_13_hr_of_another_company_is_refused(self):
        request, waiting = self.asked(1)
        response = self.approve_cancellation(self.far_hr_user, waiting)
        self.assertEqual(response.status_code, 403)
        self.assertIn("công ty khác", response.json()["error"])
        self.assertEqual((self.status(request), self.fund()), ("approved", 11))

    # 14
    def test_14_a_half_day_gives_back_half_a_day(self):
        start = monday_in(10)
        half = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.member, leave_type_id=self.monthly, start_date=start, end_date=start,
            start_date_breakdown="first_half", end_date_breakdown="first_half", requested_days=0.5,
            approved_available_days=0, approved_carryforward_days=0, description="half", status="requested")])[0]
        self.assertEqual(self.approve(self.manager_user, half).status_code, 200)
        self.assertEqual(self.fund(), 11.5)
        self.cancel(half)
        self.approve_cancellation(self.manager_user, self.waiting_cancellation(half))
        self.assertEqual(self.fund(), 12)
        self.assertEqual(self.cancellation(LeaveCancellationRequest._base_manager.get(leave_request=half)).refunded_available_days, 0.5)

    # 15
    def test_15_several_days_give_back_all_of_them(self):
        request, waiting = self.asked(3)
        self.assertEqual(self.fund(), 9)
        self.approve_cancellation(self.manager_user, waiting)
        self.assertEqual(self.fund(), 12)

    # 16
    def test_16_the_shared_monthly_annual_fund_and_regime_apart(self):
        annual, waiting = self.asked(2, self.annual)
        regime = self.approved(3, self.regime)
        self.assertEqual((self.fund(), self.fund(self.regime)), (10, 27))
        self.approve_cancellation(self.manager_user, waiting)
        self.assertEqual((self.fund(), self.fund(self.regime)), (12, 27), "Nghỉ Chế Độ untouched")
        self.assertFalse(AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.annual).exists())
        self.assertEqual(self.status(regime), "approved")

    # 17
    def test_17_a_cancelled_leave_is_never_refunded_again(self):
        request, waiting = self.asked(2)
        self.approve_cancellation(self.manager_user, waiting)
        self.assertEqual(self.cancel(request).status_code, 409)
        self.assertEqual(self.reject(self.manager_user, request).status_code, 200)
        self.assertEqual(self.fund(), 12)
        LeaveRequest._base_manager.filter(pk=request.pk).delete()
        self.assertEqual(self.fund(), 12, "deleting it gives nothing more")

    # 21
    def test_21_a_failure_inside_the_decision_rolls_everything_back(self):
        request, waiting = self.asked(2)
        with patch("leave.models.LeaverequestComment.objects.create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.approve_cancellation(self.manager_user, waiting)
        self.assertEqual((self.status(request), self.fund(), self.cancellation(waiting).status), ("approved", 10, "requested"))
        self.assertEqual(self.approve_cancellation(self.manager_user, waiting).status_code, 200, "and works afterwards")
        self.assertEqual(self.fund(), 12)

    # 22
    def test_22_an_old_approved_leave_that_never_took_days_gets_nothing_back(self):
        start = monday_in(11)
        legacy = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.member, leave_type_id=self.monthly, start_date=start, end_date=start + timedelta(days=1),
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=2,
            approved_available_days=0, approved_carryforward_days=0, description="legacy", status="approved")])[0]
        self.assertEqual(self.cancel(legacy).status_code, 200)
        self.approve_cancellation(self.manager_user, self.waiting_cancellation(legacy))
        self.assertEqual((self.status(legacy), self.fund()), ("cancelled", 12), "12, never 14")

    def test_a_started_leave_can_not_be_cancelled(self):
        past = date.today() - timedelta(days=3)
        old = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.member, leave_type_id=self.monthly, start_date=past, end_date=past + timedelta(days=5),
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=4,
            approved_available_days=4, description="started", status="approved")])[0]
        response = self.cancel(old)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "already_started")

    def test_an_approved_leave_is_never_deleted_by_the_employee(self):
        request = self.approved(2)
        response = self.api(self.member_user).delete(f"/api/leave/user-request/{request.pk}/")
        self.assertEqual(response.status_code, 409)
        self.web(self.member_user).post(f"/leave/user-request-delete/{request.pk}/", HTTP_HX_REQUEST="true")
        self.assertTrue(LeaveRequest._base_manager.filter(pk=request.pk).exists())
        self.assertEqual(self.fund(), 10)

    def test_deleting_an_approved_leave_gives_its_days_back_once(self):
        request = self.approved(2)
        self.leave(request).delete()  # one by one: the model refuses (as before)
        self.assertTrue(LeaveRequest._base_manager.filter(pk=request.pk).exists())
        self.assertEqual(self.fund(), 10)
        # A bulk delete (queryset) skips the model's refusal: it used to drop
        # the approved leave and keep its days taken for good.
        LeaveRequest._base_manager.filter(pk=request.pk).delete()
        self.assertFalse(LeaveRequest._base_manager.filter(pk=request.pk).exists())
        self.assertEqual(self.fund(), 12)

    def test_the_lists_show_only_what_one_may_decide(self):
        self.asked(1)
        rows = self.api(self.manager_user).get("/api/leave/cancellation-requests/").json()["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["employee"]["id"], self.member.pk)
        self.assertEqual(self.api(self.outsider_user).get("/api/leave/cancellation-requests/").json()["results"], [])
        self.assertEqual(self.api(self.far_hr_user).get("/api/leave/cancellation-requests/").json()["results"], [])

    def test_web_hr_lists_and_decides(self):
        request, waiting = self.asked(2)
        page = self.web(self.manager_user).get("/leave/cancellation-requests/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Chấp thuận hủy")
        self.assertContains(page, self.member.get_full_name())
        other = self.web(self.outsider_user).get("/leave/cancellation-requests/")
        self.assertContains(other, "Không có yêu cầu hủy nào cần bạn xử lý")
        self.assertNotContains(other, f"cancellation-{waiting.pk}")
        self.web(self.outsider_user).post(f"/leave/cancellation-requests/{waiting.pk}/approve/", {"reason": "x"})
        self.assertEqual((self.status(request), self.fund()), ("approved", 10), "refused for the outsider")
        response = self.web(self.manager_user).post(f"/leave/cancellation-requests/{waiting.pk}/approve/", {"reason": "ok"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual((self.status(request), self.fund()), ("cancelled", 12))

    def test_web_detail_pages_show_the_cancellation(self):
        request, waiting = self.asked(1)
        htmx = {"HTTP_HX_REQUEST": "true"}
        hr_page = self.web(self.admin_user).get(f"/leave/leave-requests-detail-view/{request.pk}/", **htmx)
        self.assertEqual(hr_page.status_code, 200)
        self.assertContains(hr_page, "Chờ duyệt hủy")
        self.assertContains(hr_page, "Xử lý yêu cầu hủy")
        own_page = self.web(self.member_user).get(f"/leave/my-leave-request-detail-view/{request.pk}/", **htmx)
        self.assertEqual(own_page.status_code, 200)
        self.assertContains(own_page, "Chờ duyệt hủy")
        self.assertNotContains(own_page, "Xử lý yêu cầu hủy", msg_prefix="no decision link for the employee")

    def test_web_employee_cancel_asks_and_shows_waiting(self):
        request = self.approved(1)
        self.web_cancel(request)
        self.assertEqual(self.status(request), "approved")
        self.assertIsNotNone(self.waiting_cancellation(request))
        self.assertEqual(self.leave(request).cancellation_status_display(), "Chờ duyệt hủy")

    # 18 (one at a time): the creation-time "no approval needed" path
    def test_18_approval_without_review_is_locked_and_never_negative(self):
        from leave.balance_ops import approve_without_review

        first = self.ask(1)
        self.assertTrue(approve_without_review(first))
        self.assertEqual((first.status, self.fund()), ("approved", 11))
        self.assertFalse(approve_without_review(first), "already approved: nothing taken again")
        self.assertEqual(self.fund(), 11)
        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(available_days=0)
        short = self.ask(1)
        self.assertFalse(approve_without_review(short))
        self.assertEqual((short.status, self.fund()), ("requested", 0), "left for HR, never below zero")

    def test_penalties_take_what_is_there_and_give_back_exactly_that(self):
        from leave.balance_ops import deduct_penalty, restore_penalty

        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(available_days=0.5)
        penalty = SimpleNamespace(pk=901, employee_id=self.member, leave_type_id=self.annual,
                                  minus_leaves=1, deduct_from_carry_forward=False)
        self.assertEqual(deduct_penalty(penalty), 0.5)
        self.assertEqual(self.fund(), 0)
        self.assertEqual(restore_penalty(penalty), 0.5, "only what was taken")
        self.assertEqual(self.fund(), 0.5)
        self.assertEqual(restore_penalty(penalty), 0, "once")
        self.assertEqual(self.fund(), 0.5)

    def test_compensatory_take_never_goes_negative(self):
        from leave.balance_ops import take_days

        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(
            available_days=1, carryforward_days=1)
        self.assertEqual(take_days(self.member, self.monthly, 5), 2)
        self.assertEqual(self.fund(), 0)

    def test_contract_bodies_for_the_app(self):
        request, waiting = self.asked(2)
        detail = self.api(self.member_user).get(f"/api/leave/user-request/{request.pk}/").json()
        listed = self.api(self.member_user).get("/api/leave/user-request/").json()
        for key in ("id", "status", "start_date", "end_date", "leave_type_id", "cancellation"):
            self.assertIn(key, detail)
        self.assertEqual(set(detail["cancellation"]), {"id", "status", "reason", "decision_reason", "requested_at", "decided_at"})
        cancel_answer = self.api(self.member_user).put(f"/api/leave/cancel/{self.approved(1).pk}/", format="json").json()
        conflict = self.cancel(request).json()
        self.assertEqual(set(conflict), {"error", "code"})
        target = os.environ.get("LEAVE_CONTRACT_FIXTURES")
        if target:
            os.makedirs(target, exist_ok=True)
            for name, body in {
                "leave_request_cancellation_pending": detail,
                "leave_requests": listed,
                "leave_cancel_requested": cancel_answer,
                "leave_cancel_conflict": conflict,
            }.items():
                with open(os.path.join(target, f"{name}.json"), "w", encoding="utf-8") as out:
                    json.dump(body, out, ensure_ascii=False, indent=2)


@skipUnless(connection.vendor == "postgresql", "row locks need PostgreSQL (SQLite has none)")
class ConcurrentCancellationTests(CancellationWorld, TransactionTestCase):
    def setUp(self):
        self.build()

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
        self.assertFalse(any(t.is_alive() for t in threads), "a caller hung (lock never released?)")
        return results

    def codes(self, results):
        return sorted(getattr(r, "status_code", repr(r)) for r in results)

    # 7
    def test_07_two_managers_approve_the_same_cancellation_at_once(self):
        request, waiting = self.asked(2)
        with slow(LeaveRequest):
            results = self.race(lambda: self.approve_cancellation(self.manager_user, waiting),
                                lambda: self.approve_cancellation(self.hr_user, waiting))
        self.assertEqual(self.codes(results), [200, 409])
        self.assertEqual((self.status(request), self.fund()), ("cancelled", 12), "refunded once")

    # 8
    def test_08_one_approves_and_one_rejects_at_once(self):
        request, waiting = self.asked(2)
        with slow(LeaveCancellationRequest):
            results = self.race(lambda: self.approve_cancellation(self.manager_user, waiting),
                                lambda: self.reject_cancellation(self.hr_user, waiting))
        self.assertEqual(self.codes(results), [200, 409])
        outcome = self.cancellation(waiting).status
        expected = ("cancelled", 12) if outcome == "approved" else ("approved", 10)
        self.assertEqual((self.status(request), self.fund()), expected, f"consistent with {outcome}")

    # 10
    def test_10_two_cancellation_requests_at_once(self):
        request = self.approved(2)
        with slow(LeaveCancellationRequest):
            results = self.race(lambda: self.cancel(request), lambda: self.cancel(request))
        self.assertEqual(self.codes(results), [200, 409])
        self.assertEqual(LeaveCancellationRequest._base_manager.filter(leave_request=request).count(), 1)
        self.assertEqual(self.fund(), 10)

    # 18
    def test_18_two_requests_approved_without_review_for_the_last_day(self):
        from leave.balance_ops import approve_without_review

        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(available_days=1)
        first, second = self.ask(1), self.ask(1)
        with slow(AvailableLeave):
            results = self.race(lambda: approve_without_review(first), lambda: approve_without_review(second))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.fund(), 0, "never below zero")
        self.assertEqual(sorted([self.status(first), self.status(second)]), ["approved", "requested"])

    # 19
    def test_19_two_different_requests_on_one_fund_at_once(self):
        from leave.balance_ops import approve_without_review

        first, second = self.ask(1), self.ask(2)
        with slow(AvailableLeave):
            self.race(lambda: approve_without_review(first), lambda: self.approve(self.manager_user, second))
        self.assertEqual(self.fund(), 9, "12 - 1 - 2: no update lost")

    # 20
    def test_20_an_allocation_and_a_cancellation_approval_at_once(self):
        request, waiting = self.asked(2)
        allocation = self.allocation(2)
        with slow(AvailableLeave):
            results = self.race(lambda: self.approve_cancellation(self.manager_user, waiting),
                                lambda: self.approve_allocation(self.admin_user, allocation))
        self.assertEqual(self.codes(results), [200, 200])
        self.assertEqual(self.fund(), 14, "10 + 2 back + 2 granted")

    def test_20b_an_allocation_and_an_approval_at_once(self):
        allocation, request = self.allocation(2), self.ask(1)
        with slow(AvailableLeave):
            results = self.race(lambda: self.approve_allocation(self.admin_user, allocation),
                                lambda: self.approve(self.manager_user, request))
        self.assertEqual(self.codes(results), [200, 200])
        self.assertEqual(self.fund(), 13)

    def test_deleting_while_the_cancellation_is_approved_refunds_once(self):
        request, waiting = self.asked(2)

        def delete():
            LeaveRequest._base_manager.filter(pk=request.pk).delete()
            return "deleted"

        with slow(LeaveRequest):
            self.race(lambda: self.approve_cancellation(self.manager_user, waiting), delete)
        self.assertEqual(self.fund(), 12, "never 14")

    def test_asking_while_the_manager_rejects_the_leave(self):
        request = self.approved(2)
        with slow(LeaveRequest):
            self.race(lambda: self.cancel(request), lambda: self.reject(self.manager_user, request))
        self.assertEqual((self.status(request), self.fund()), ("rejected", 12))
