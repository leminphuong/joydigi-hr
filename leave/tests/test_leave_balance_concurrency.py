"""Leave balance under every decision, one at a time and at the same time.

Phase 1 of the 2026-11-13 upgrade (audit SEC-18). One fund: "NGHỈ PHÉP
THÁNG" holds it, "Nghỉ Phép Năm" draws on it (``shared_balance_with``),
"Nghỉ Chế Độ" keeps its own. Every decision that moves days (approve,
reject, cancel, allocation approve/reject, from the web or the app) locks
the request row and the balance row and checks the status again after the
lock, so:

* a request is deducted once, however many managers approve it at once;
* two requests approved at once both count (no lost update);
* a refund gives back what was taken, once (reject + cancel, two rejects);
* an allocation adds its days once.

``SequentialDecisionTests`` run on any database. ``ConcurrentDecisionTests``
need PostgreSQL (row locks; SQLite has none) and are skipped elsewhere: they
start real threads, each with its own connection, released together by a
barrier, and slow one ``save()`` down to hold the race window open.

Fake data only.
"""

import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase
from rest_framework.test import APIClient

from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from leave.models import AvailableLeave, LeaveAllocationRequest, LeaveRequest, LeaveType

HTMX = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "objectDetailsModalTarget",
        "HTTP_HX_CURRENT_URL": "http://testserver/leave/request-view/"}
SLOW = 0.4  # seconds a slowed save() holds its transaction open


def monday_in(weeks):
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) + 7 * weeks)


class World:
    """Company, employee + reporting manager + admin, the three leave types."""

    def build(self):
        self.company = make_company("Race Co")
        self.manager_user = make_user("race_manager", password="x-Race-Manager-1")
        self.manager = make_employee(company=self.company, email="m@race.test", user=self.manager_user)
        self.admin_user = make_user("race_admin", password="x-Race-Admin-1", is_superuser=True)
        self.admin = make_employee(company=self.company, email="admin@race.test", user=self.admin_user)
        self.member_user = make_user("race_member", password="x-Race-Member-1")
        self.member = make_employee(company=self.company, email="a@race.test", user=self.member_user)
        EmployeeWorkInformation.objects.filter(employee_id=self.member).update(reporting_manager_id=self.manager)
        self.monthly = LeaveType.objects.create(name="NGHI PHEP THANG", total_days=12, payment="paid", company_id=self.company)
        self.annual = LeaveType.objects.create(name="Nghi Phep Nam", total_days=12, payment="paid", company_id=self.company)
        self.regime = LeaveType.objects.create(name="Nghi Che Do", total_days=30, payment="paid", company_id=self.company)
        LeaveType._base_manager.filter(pk=self.annual.pk).update(shared_balance_with=self.monthly)
        self.annual.refresh_from_db()
        # Exact balances (whatever the sync signals opened): one fund + Chế Độ.
        AvailableLeave._base_manager.filter(employee_id=self.member).delete()
        AvailableLeave.objects.create(employee_id=self.member, leave_type_id=self.monthly,
                                      available_days=12, carryforward_days=0)
        AvailableLeave.objects.create(employee_id=self.member, leave_type_id=self.regime,
                                      available_days=30, carryforward_days=0)
        self._week = 0

    # -- data ---------------------------------------------------------------
    def ask(self, days=1, leave_type=None, status="requested"):
        """A leave request of `days` working days, Monday on, a new week each time."""
        self._week += 1
        start = monday_in(self._week)
        return LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.member, leave_type_id=leave_type or self.monthly,
            start_date=start, end_date=start + timedelta(days=days - 1),
            start_date_breakdown="full_day", end_date_breakdown="full_day",
            requested_days=days, approved_available_days=0, approved_carryforward_days=0,
            description="race test", status=status,
        )])[0]

    def allocation(self, days=2, leave_type=None):
        return LeaveAllocationRequest._base_manager.bulk_create([LeaveAllocationRequest(
            employee_id=self.member, leave_type_id=leave_type or self.monthly,
            requested_days=days, description="race test", status="requested",
        )])[0]

    def fund(self, leave_type=None):
        row = AvailableLeave._base_manager.get(employee_id=self.member, leave_type_id=leave_type or self.monthly)
        return row.available_days + row.carryforward_days

    def status(self, obj):
        return type(obj)._base_manager.get(pk=obj.pk).status

    # -- callers ------------------------------------------------------------
    def api(self, user):
        client = APIClient()
        client.force_authenticate(user=type(user).objects.get(pk=user.pk))
        return client

    def web(self, user):
        client = Client()
        client.force_login(user)
        return client

    def approve(self, user, request):
        return self.api(user).put(f"/api/leave/approve/{request.pk}/", format="json")

    def reject(self, user, request):
        return self.api(user).put(f"/api/leave/reject/{request.pk}/", format="json")

    def cancel(self, request, reason="race test"):
        return self.api(self.member_user).put(f"/api/leave/cancel/{request.pk}/", {"reason": reason}, format="json")

    def waiting_cancellation(self, request):
        from leave.models import LeaveCancellationRequest

        return LeaveCancellationRequest._base_manager.filter(leave_request=request, status="requested").first()

    def approve_cancellation(self, user, cancellation, reason=""):
        return self.api(user).put(
            f"/api/leave/cancellation-requests/{cancellation.pk}/approve/", {"reason": reason}, format="json"
        )

    def reject_cancellation(self, user, cancellation, reason=""):
        return self.api(user).put(
            f"/api/leave/cancellation-requests/{cancellation.pk}/reject/", {"reason": reason}, format="json"
        )

    def approve_allocation(self, user, allocation):
        return self.api(user).put(f"/api/leave/allocation-approve/{allocation.pk}/", format="json")

    def reject_allocation(self, user, allocation):
        return self.api(user).put(f"/api/leave/allocation-reject/{allocation.pk}/", format="json")

    def web_approve(self, user, request):
        return self.web(user).get(f"/leave/request-approve/{request.pk}/")

    def web_reject(self, user, request):
        return self.web(user).post(f"/leave/request-cancel/{request.pk}/", {"reason": "race test"}, **HTMX)

    def web_cancel(self, request):
        return self.web(self.member_user).post(
            f"/leave/user-request-cancel/{request.pk}/", {"reason": "race test"}, **HTMX
        )


class SequentialDecisionTests(World, TestCase):
    def setUp(self):
        self.build()

    # approve
    def test_approve_once_takes_the_days_and_records_them(self):
        request = self.ask(2)
        self.assertEqual(self.approve(self.manager_user, request).status_code, 200)
        self.assertEqual(self.fund(), 10)
        row = LeaveRequest._base_manager.get(pk=request.pk)
        self.assertEqual((row.status, row.approved_available_days, row.approved_carryforward_days), ("approved", 2, 0))

    def test_a_second_approval_changes_nothing(self):
        request = self.ask(1)
        self.approve(self.manager_user, request)
        response = self.approve(self.admin_user, request)
        self.assertEqual(response.status_code, 400)
        self.assertIn("non_field_errors", response.json(), "the answer clients already read")
        self.assertEqual(self.fund(), 11)

    def test_not_enough_days_is_refused_and_nothing_moves(self):
        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(available_days=1)
        request = self.ask(2)
        response = self.approve(self.manager_user, request)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Không đủ ngày phép", str(response.json()))
        self.assertEqual((self.fund(), self.status(request)), (1, "requested"))

    def test_carry_forward_is_used_first(self):
        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(
            available_days=5, carryforward_days=2)
        request = self.ask(3)
        self.approve(self.manager_user, request)
        row = AvailableLeave._base_manager.get(employee_id=self.member, leave_type_id=self.monthly)
        self.assertEqual((row.carryforward_days, row.available_days), (0, 4))
        self.reject(self.manager_user, request)
        row.refresh_from_db()
        self.assertEqual((row.carryforward_days, row.available_days), (2, 5), "given back where it came from")

    def test_balance_never_goes_negative(self):
        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(available_days=1)
        first, second = self.ask(1), self.ask(1)
        self.assertEqual(self.approve(self.manager_user, first).status_code, 200)
        self.assertEqual(self.approve(self.manager_user, second).status_code, 400)
        self.assertEqual(self.fund(), 0)

    def test_the_employee_can_not_approve_their_own_request(self):
        request = self.ask(1)
        self.assertEqual(self.approve(self.member_user, request).status_code, 403)
        self.assertEqual(self.fund(), 12)

    # reject / cancel / refund
    def test_reject_of_an_approved_request_gives_the_days_back_once(self):
        request = self.ask(2)
        self.approve(self.manager_user, request)
        self.assertEqual(self.reject(self.manager_user, request).status_code, 200)
        self.assertEqual(self.fund(), 12)
        self.assertEqual(self.reject(self.admin_user, request).status_code, 400, "already rejected")
        self.assertEqual(self.fund(), 12)

    def test_reject_of_a_pending_request_moves_nothing(self):
        request = self.ask(2)
        self.assertEqual(self.reject(self.manager_user, request).status_code, 200)
        self.assertEqual((self.fund(), self.status(request)), (12, "rejected"))

    def test_employee_cancel_of_an_approved_leave_only_asks_then_one_refund(self):
        # Policy of 2026-11-13 (second pass): asking changes nothing; the
        # manager's approval of the request cancels and refunds, once.
        request = self.ask(2)
        self.approve(self.manager_user, request)
        self.assertEqual(self.cancel(request).status_code, 200)
        self.assertEqual((self.fund(), self.status(request)), (10, "approved"))
        waiting = self.waiting_cancellation(request)
        self.assertEqual(self.approve_cancellation(self.manager_user, waiting).status_code, 200)
        self.assertEqual((self.fund(), self.status(request)), (12, "cancelled"))
        self.assertEqual(self.cancel(request).status_code, 409, "nothing approved to cancel any more")
        # The manager then rejecting the cancelled request gives nothing more.
        self.assertEqual(self.reject(self.manager_user, request).status_code, 200)
        self.assertEqual(self.fund(), 12)

    def test_cancel_is_only_for_the_owner(self):
        pending = self.ask(1)
        self.assertEqual(self.cancel(pending).status_code, 200, "a pending leave is withdrawn")
        self.assertEqual((self.status(pending), self.fund()), ("cancelled", 12))
        request = self.ask(1)
        self.approve(self.manager_user, request)
        response = self.api(self.manager_user).put(f"/api/leave/cancel/{request.pk}/", format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.fund(), 11)

    def test_re_approving_a_rejected_request_takes_the_days_once_again(self):
        request = self.ask(1)
        self.approve(self.manager_user, request)
        self.reject(self.manager_user, request)
        LeaveRequest._base_manager.filter(pk=request.pk).update(status="requested")  # resubmitted
        self.approve(self.manager_user, request)
        self.assertEqual(self.fund(), 11)

    # web, same rules
    def test_web_approve_twice_takes_the_days_once(self):
        request = self.ask(2)
        self.web_approve(self.admin_user, request)
        self.web_approve(self.admin_user, request)
        self.assertEqual((self.fund(), self.status(request)), (10, "approved"))

    def test_web_cancel_asks_and_web_reject_refunds_once(self):
        request = self.ask(2)
        self.web_approve(self.admin_user, request)
        self.web_cancel(request)
        self.assertEqual((self.fund(), self.status(request)), (10, "approved"), "only asked")
        self.assertIsNotNone(self.waiting_cancellation(request))
        self.web_reject(self.admin_user, request)
        self.assertEqual((self.fund(), self.status(request)), (12, "rejected"))

    # allocations
    def test_allocation_adds_once_and_a_reject_takes_it_back_once(self):
        allocation = self.allocation(3)
        self.assertEqual(self.approve_allocation(self.manager_user, allocation).status_code, 200)
        self.assertEqual(self.fund(), 15)
        self.assertEqual(self.approve_allocation(self.admin_user, allocation).status_code, 400)
        self.assertEqual(self.fund(), 15)
        self.assertEqual(self.reject_allocation(self.manager_user, allocation).status_code, 200)
        self.assertEqual(self.fund(), 12)
        self.assertEqual(self.reject_allocation(self.manager_user, allocation).status_code, 400)
        self.assertEqual(self.fund(), 12)

    def test_withdrawn_allocation_never_leaves_a_negative_balance(self):
        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(available_days=0)
        allocation = self.allocation(3)
        self.approve_allocation(self.manager_user, allocation)
        self.approve(self.manager_user, self.ask(2))
        self.reject_allocation(self.manager_user, allocation)
        self.assertEqual(self.fund(), 0)

    def test_allocation_of_the_annual_type_lands_in_the_one_fund(self):
        allocation = self.allocation(2, leave_type=self.annual)
        self.assertEqual(self.approve_allocation(self.manager_user, allocation).status_code, 200)
        self.assertEqual(self.fund(), 14)
        self.assertFalse(
            AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.annual).exists(),
            "never a second, separate annual fund",
        )

    def test_bulk_allocation_form_approves_into_the_fund_once(self):
        from leave.forms import LeaveAllocationBulkForm

        allocation = self.allocation(2, leave_type=self.annual)
        LeaveAllocationBulkForm._approve(object(), allocation)
        LeaveAllocationBulkForm._approve(object(), allocation)  # a double click
        self.assertEqual((self.fund(), self.status(allocation)), (14, "approved"))

    # one fund for the monthly and the annual type, Chế Độ apart
    def test_monthly_and_annual_requests_draw_on_one_fund(self):
        self.approve(self.manager_user, self.ask(1, self.monthly))
        self.approve(self.manager_user, self.ask(2, self.annual))
        self.assertEqual(self.fund(), 9)
        self.approve(self.manager_user, self.ask(3, self.regime))
        self.assertEqual((self.fund(), self.fund(self.regime)), (9, 27))

    def test_annual_refund_goes_back_to_the_one_fund(self):
        request = self.ask(2, self.annual)
        self.approve(self.manager_user, request)
        self.cancel(request)
        self.approve_cancellation(self.manager_user, self.waiting_cancellation(request))
        self.assertEqual(self.fund(), 12)

    # bulk approve
    def test_bulk_approve_with_a_repeated_id_takes_the_days_once(self):
        request = self.ask(2)
        response = self.api(self.manager_user).put(
            "/api/leave/request-bulk-action/", {"leave_request_id": [request.pk, request.pk]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual((self.fund(), self.status(request)), (10, "approved"))

    # balance shown to the app after a decision
    def test_the_app_balance_follows_each_decision(self):
        def remaining():
            body = self.api(self.member_user).get("/api/leave/available-leave/?page_size=100").json()
            return next(r for r in body["results"] if r["leave_type_id"]["id"] == self.monthly.pk)["remaining_days"]

        request = self.ask(2)
        self.approve(self.manager_user, request)
        self.assertEqual(remaining(), 10)
        self.approve_allocation(self.manager_user, self.allocation(1))
        self.assertEqual(remaining(), 11)
        self.cancel(request)
        self.assertEqual(remaining(), 11, "asking to cancel changes nothing")
        self.approve_cancellation(self.manager_user, self.waiting_cancellation(request))
        self.assertEqual(remaining(), 13)


class YearRolloverTests(World, TestCase):
    """The yearly cap is counted per year; a new year never starts with a
    second 12 days on its own — HR sets the new year's starting point."""

    def setUp(self):
        self.build()
        EmployeeWorkInformation._base_manager.filter(employee_id=self.member).update(date_joining=date(2020, 1, 1))
        self.row = AvailableLeave._base_manager.get(employee_id=self.member, leave_type_id=self.monthly)

    def mark(self, reason):
        self.row._change_reason = reason
        self.row.save()

    def plan(self, year, month):
        from leave.shared_fund import plan_accrual

        rows = plan_accrual(pool=self.monthly, year=year, month=month, employee_ids=[self.member.pk])
        return rows[0]

    def test_cap_reached_this_year_and_nothing_for_next_year_without_its_starting_point(self):
        from leave.shared_fund import CONVERSION_MARK, accrual_state

        self.mark(CONVERSION_MARK.format(year=2026, days=12, period="2026-12"))
        self.assertEqual(accrual_state(self.row, 2026)[0], 12)
        december = self.plan(2026, 12)
        self.assertEqual(december.action, "skip")
        january = self.plan(2027, 1)
        self.assertEqual(january.action, "skip")
        self.assertIn("Chưa có mốc của năm", january.reason)
        self.assertEqual(accrual_state(self.row, 2027), (0.0, set()), "2026 markers never count for 2027")

    def test_new_year_with_its_starting_point_accrues_one_day_a_month_up_to_the_cap(self):
        from leave.shared_fund import CONVERSION_MARK, apply_accrual

        self.mark(CONVERSION_MARK.format(year=2027, days=11, period="2027-11"))
        december = self.plan(2027, 12)
        self.assertEqual((december.action, december.credit), ("credit", 1))
        apply_accrual([december], year=2027, month=12, expect_count=1)
        self.row.refresh_from_db()
        self.assertEqual(self.row.available_days, 13)
        self.assertEqual(self.plan(2027, 12).action, "skip", "the same month never twice")


@contextmanager
def slow(model):
    """`model.save()` waits SLOW seconds first: whoever saves holds the race open."""
    real_save = model.save

    def slow_save(self, *args, **kwargs):
        time.sleep(SLOW)
        return real_save(self, *args, **kwargs)

    with patch.object(model, "save", slow_save):
        yield


@skipUnless(connection.vendor == "postgresql", "row locks need PostgreSQL (SQLite has none)")
class ConcurrentDecisionTests(World, TransactionTestCase):
    def setUp(self):
        self.build()

    def race(self, *calls):
        """Run `calls` in threads released together; their results in order."""
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def run(index, call):
            try:
                barrier.wait(timeout=30)
                results[index] = call()
            except Exception as error:  # reported by the assertions below
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

    def test_two_managers_approve_the_same_request_at_once(self):
        request = self.ask(1)
        with slow(LeaveRequest):
            results = self.race(lambda: self.approve(self.manager_user, request),
                                lambda: self.approve(self.admin_user, request))
        self.assertEqual(self.codes(results), [200, 400], "exactly one approval goes through")
        self.assertEqual(self.fund(), 11, "the day is taken once")

    def test_web_and_app_approve_the_same_request_at_once(self):
        request = self.ask(2)
        with slow(LeaveRequest):
            self.race(lambda: self.web_approve(self.admin_user, request),
                      lambda: self.approve(self.manager_user, request))
        self.assertEqual((self.fund(), self.status(request)), (10, "approved"))

    def test_two_requests_approved_at_once_both_count(self):
        first, second = self.ask(1), self.ask(2)
        with slow(AvailableLeave):
            results = self.race(lambda: self.approve(self.manager_user, first),
                                lambda: self.approve(self.admin_user, second))
        self.assertEqual(self.codes(results), [200, 200])
        self.assertEqual(self.fund(), 9, "12 - 1 - 2: no update lost")

    def test_monthly_and_annual_requests_at_once_share_one_fund(self):
        monthly, annual = self.ask(1, self.monthly), self.ask(2, self.annual)
        with slow(AvailableLeave):
            self.race(lambda: self.approve(self.manager_user, monthly),
                      lambda: self.approve(self.admin_user, annual))
        self.assertEqual((self.fund(), self.fund(self.regime)), (9, 30))

    def test_last_days_approved_twice_at_once_never_go_negative(self):
        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(available_days=1)
        first, second = self.ask(1), self.ask(1)
        with slow(AvailableLeave):
            results = self.race(lambda: self.approve(self.manager_user, first),
                                lambda: self.approve(self.admin_user, second))
        self.assertEqual(self.codes(results), [200, 400])
        self.assertEqual(self.fund(), 0)

    def test_two_rejects_at_once_refund_once(self):
        request = self.ask(2)
        self.approve(self.manager_user, request)
        with slow(LeaveRequest):
            self.race(lambda: self.reject(self.manager_user, request),
                      lambda: self.web_reject(self.admin_user, request))
        self.assertEqual(self.fund(), 12, "not 14")

    def test_cancel_and_reject_at_once_refund_once(self):
        request = self.ask(2)
        self.approve(self.manager_user, request)
        with slow(LeaveRequest):
            self.race(lambda: self.cancel(request), lambda: self.reject(self.manager_user, request))
        self.assertEqual(self.fund(), 12, "not 14")

    def test_the_same_allocation_approved_twice_at_once_adds_once(self):
        allocation = self.allocation(2)
        with slow(LeaveAllocationRequest):
            results = self.race(lambda: self.approve_allocation(self.manager_user, allocation),
                                lambda: self.approve_allocation(self.admin_user, allocation))
        self.assertEqual(self.codes(results), [200, 400])
        self.assertEqual(self.fund(), 14)

    def test_allocation_and_approval_at_once_both_count(self):
        allocation, request = self.allocation(2), self.ask(1)
        with slow(AvailableLeave):
            self.race(lambda: self.approve_allocation(self.manager_user, allocation),
                      lambda: self.approve(self.admin_user, request))
        self.assertEqual(self.fund(), 13, "12 + 2 - 1")
