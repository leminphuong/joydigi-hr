"""At most 2 days of the shared annual fund used per calendar month
(2026-11-15), and approved leaves that keep their days.

Policy (``leave/monthly_limit.py``):
* NGHỈ PHÉP THÁNG and Nghỉ Phép Năm draw on one fund; together they use at
  most 2 days per calendar month — waiting and approved requests both count;
  rejected / cancelled ones release their days; an approved leave with a
  cancellation still waiting keeps counting until it is approved;
* the balance must still cover the request;
* a request over two months counts its days in each month;
* Nghỉ Chế Độ (own fund) and Nghỉ Bù (compensatory) are not limited;
* an approved leave never changes employee, type, dates or days: HR edits
  only its note, which keeps its recorded days.

Fake data only. The race tests need PostgreSQL and are skipped on SQLite.
"""

import threading
from datetime import date, timedelta
from unittest import skipUnless

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase

from leave.models import AvailableLeave, LeaveRequest, LeaveType
from leave.tests.test_leave_balance_concurrency import HTMX, slow
from leave.tests.test_leave_cancellation import grant
from leave.tests.test_leave_p1_fixes import P1World

LIMIT_WORDS = "Mỗi tháng chỉ được nghỉ phép tối đa 2 ngày"


def month_after(months):
    today = date.today()
    index = today.year * 12 + today.month - 1 + months
    return index // 12, index % 12 + 1


def weekday_of(year, month, nth=0):
    """The (nth+1)-th Monday..Friday date of the first full week of a month."""
    first = date(year, month, 1)
    monday = first + timedelta(days=(7 - first.weekday()) % 7)
    return monday + timedelta(days=nth)


class LimitWorld(P1World):
    def build(self):
        super().build()
        self.year, self.month = month_after(3)
        self.next_year, self.next_month = month_after(4)
        self.comp_type = LeaveType.objects.create(
            name="Nghi Bu", total_days=0, payment="paid", company_id=self.company, is_compensatory_leave=True
        )
        AvailableLeave.objects.create(employee_id=self.member, leave_type_id=self.comp_type, available_days=10)

    def day(self, nth=0, year=None, month=None):
        return weekday_of(year or self.year, month or self.month, nth)

    def ask_api(self, start, end=None, leave_type=None, start_breakdown="full_day", end_breakdown=None, user=None):
        end = end or start
        return self.api(user or self.member_user).post("/api/leave/user-request/", {
            "leave_type_id": (leave_type or self.monthly).pk,
            "start_date": str(start), "end_date": str(end),
            "start_date_breakdown": start_breakdown,
            "end_date_breakdown": end_breakdown or start_breakdown,
            "description": "test giới hạn tháng",
        }, format="json")

    def ask_web(self, start, end=None, leave_type=None, start_breakdown="full_day", end_breakdown=None):
        end = end or start
        return self.web(self.member_user).post("/leave/leave-request-create/", {
            "employee_id": self.member.pk,
            "leave_type_id": (leave_type or self.monthly).pk,
            "start_date": str(start), "end_date": str(end),
            "start_date_breakdown": start_breakdown,
            "end_date_breakdown": end_breakdown or start_breakdown,
            "description": "web giới hạn tháng",
        }, **HTMX)

    def created(self, response):
        return response.status_code in (200, 201) and "id" in response.json()

    def error_text(self, response):
        return str(response.content.decode())

    def requests(self, **filters):
        return LeaveRequest._base_manager.filter(employee_id=self.member, **filters)

    def last(self):
        return self.requests().order_by("-pk").first()

    def approve_fully(self, leave):
        """2 days or more: the reporting manager, then the admin step
        (the project's default two-step rule)."""
        for user in (self.manager_user, self.admin_user):
            if LeaveRequest._base_manager.get(pk=leave.pk).status != "requested":
                break
            self.assertEqual(self.approve(user, leave).status_code, 200)
        self.assertEqual(LeaveRequest._base_manager.get(pk=leave.pk).status, "approved")

    def usage(self, year=None, month=None):
        from leave import monthly_limit

        return monthly_limit.month_usage(self.member, self.monthly, year or self.year, month or self.month)


class MonthlyLimitTests(LimitWorld, TestCase):
    def setUp(self):
        self.build()

    # 1, 2, 3
    def test_half_one_and_exactly_two_days_fit(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), start_breakdown="first_half")))
        self.assertEqual(self.usage()["held_days"], 0.5)
        self.assertTrue(self.created(self.ask_api(self.day(1))))
        self.assertEqual(self.usage()["held_days"], 1.5)
        self.assertTrue(self.created(self.ask_api(self.day(2), start_breakdown="second_half")))
        self.assertEqual((self.usage()["held_days"], self.usage()["remaining_days"]), (2.0, 0.0))

    # 4
    def test_more_than_two_days_in_one_request_is_refused(self):
        response = self.ask_api(self.day(0), self.day(2))  # 3 days
        self.assertEqual(response.status_code, 400)
        self.assertIn(LIMIT_WORDS, self.error_text(response))
        self.assertFalse(self.requests().exists())

    # 5
    def test_enough_balance_but_over_the_month_is_refused(self):
        self.assertEqual(self.fund(), 12)
        self.assertTrue(self.created(self.ask_api(self.day(0), self.day(1))))  # 2 days
        response = self.ask_api(self.day(3), start_breakdown="first_half")
        self.assertEqual(response.status_code, 400)
        self.assertIn("chỉ còn đăng ký thêm được 0 ngày", self.error_text(response))

    # 6
    def test_not_enough_balance_within_the_month_is_refused(self):
        AvailableLeave._base_manager.filter(employee_id=self.member, leave_type_id=self.monthly).update(available_days=1)
        response = self.ask_api(self.day(0), self.day(1))  # 2 days, limit ok, balance 1
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(LIMIT_WORDS, self.error_text(response))
        self.assertFalse(self.requests().exists())

    # 7
    def test_two_requests_in_one_month_add_up(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), start_breakdown="first_half")))
        self.assertTrue(self.created(self.ask_api(self.day(1))))
        # 1.5 used: 0.5 left, 1 day refused
        response = self.ask_api(self.day(2))
        self.assertIn("chỉ còn đăng ký thêm được 0,5 ngày", self.error_text(response))
        self.assertTrue(self.created(self.ask_api(self.day(2), start_breakdown="first_half")))

    # 9
    def test_a_request_over_two_months_counts_in_each(self):
        last = date(self.next_year, self.next_month, 1) - timedelta(days=1)
        start, end = last - timedelta(days=1), last + timedelta(days=2)  # 2 days + 2 days
        self.assertTrue(self.created(self.ask_api(start, end)))
        self.assertEqual(self.usage(last.year, last.month)["held_days"], 2.0)
        self.assertEqual(self.usage(self.next_year, self.next_month)["held_days"], 2.0)
        response = self.ask_api(self.day(7, self.next_year, self.next_month), start_breakdown="first_half")
        self.assertIn(f"Tháng {self.next_month:02d}/{self.next_year}", self.error_text(response))

    def test_one_day_in_one_month_and_two_in_the_next_are_fine(self):
        self.assertTrue(self.created(self.ask_api(self.day(0))))
        nxt = self.day(0, self.next_year, self.next_month)
        self.assertTrue(self.created(self.ask_api(nxt, nxt + timedelta(days=1))))

    # 10
    def test_weekends_and_holidays_excluded_by_the_type_do_not_count(self):
        from base.models import CompanyLeaves, Holidays

        CompanyLeaves.objects.create(based_on_week=None, based_on_week_day="5")
        CompanyLeaves.objects.create(based_on_week=None, based_on_week_day="6")
        LeaveType._base_manager.filter(pk__in=[self.monthly.pk, self.annual.pk]).update(
            exclude_company_leave="yes", exclude_holiday="yes"
        )
        friday, monday = self.day(4), self.day(7)
        Holidays.objects.create(name="Lễ test", start_date=self.day(8), end_date=self.day(8))
        # Fri, Sat, Sun, Mon, Tue(holiday): 2 working days
        self.assertTrue(self.created(self.ask_api(friday, self.day(8))))
        self.assertEqual(self.last().requested_days, 2)
        self.assertEqual(self.usage()["held_days"], 2.0)
        self.assertEqual(self.ask_api(self.day(9), start_breakdown="first_half").status_code, 400)
        self.assertIsNotNone(monday)

    # 11, 12, 13
    def test_waiting_counts_rejected_and_cancelled_release(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), self.day(1))))
        waiting = self.last()
        self.assertEqual(self.usage()["pending_days"], 2.0)
        self.assertEqual(self.ask_api(self.day(2)).status_code, 400)
        self.assertEqual(self.reject(self.manager_user, waiting).status_code, 200)
        self.assertEqual(self.usage()["held_days"], 0.0)
        self.assertTrue(self.created(self.ask_api(self.day(2), self.day(3))))
        again = self.last()
        self.assertEqual(self.cancel(again).status_code, 200)  # withdrawn while waiting
        self.assertEqual(self.usage()["held_days"], 0.0)
        self.assertTrue(self.created(self.ask_api(self.day(3))))

    # 14
    def test_an_approved_leave_with_a_waiting_cancellation_keeps_counting(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), self.day(1))))
        leave = self.last()
        self.approve_fully(leave)
        response = self.cancel(leave)  # cancellation request waits
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(LeaveRequest._base_manager.get(pk=leave.pk).status, "approved", response.content)
        self.assertEqual(self.usage()["used_days"], 2.0, self.usage())
        self.assertEqual(self.ask_api(self.day(2)).status_code, 400)
        waiting = self.waiting_cancellation(leave)
        self.assertEqual(self.approve_cancellation(self.manager_user, waiting).status_code, 200)
        self.assertEqual(self.usage()["held_days"], 0.0)
        self.assertTrue(self.created(self.ask_api(self.day(2))))

    # 15
    def test_monthly_and_annual_types_share_the_limit(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), leave_type=self.monthly)))
        self.assertTrue(self.created(self.ask_api(self.day(1), leave_type=self.annual)))
        response = self.ask_api(self.day(2), leave_type=self.annual, start_breakdown="first_half")
        self.assertEqual(response.status_code, 400)
        self.assertIn(LIMIT_WORDS, self.error_text(response))

    # 16
    def test_regime_and_compensatory_leave_are_not_limited(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), self.day(1))))
        self.assertTrue(self.created(self.ask_api(self.day(2), self.day(4), leave_type=self.regime)))
        nxt = self.day(0, self.next_year, self.next_month)
        self.assertTrue(self.created(self.ask_api(nxt, nxt + timedelta(days=2), leave_type=self.comp_type)))

    # 20 (web)
    def test_the_web_form_applies_the_same_rule(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), self.day(1))))
        before = self.requests().count()
        response = self.ask_web(self.day(2))
        self.assertEqual(self.requests().count(), before, "the web form must refuse too")
        self.assertIn(LIMIT_WORDS, response.content.decode())

    def test_the_app_and_web_show_the_same_numbers(self):
        from leave.services import shared_fund_summaries

        from leave.monthly_limit import months_ahead

        self.assertTrue(self.created(self.ask_api(self.day(0), start_breakdown="first_half")))
        response = self.api(self.member_user).get("/api/leave/available-leave/")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["results"]
        shared = [r["shared_balance"] for r in rows if r.get("shared_balance")]
        self.assertTrue(shared)
        api_limit = shared[0]["monthly_limit"]
        self.assertEqual(api_limit["limit_days"], 2.0)
        web = shared_fund_summaries(self.member)[0]
        self.assertEqual(web["monthly_limit"], 2.0)
        self.assertEqual(api_limit["months"], web["months"])
        # the month asked for, three months ahead, shows up once it is "this month or the next two"
        ahead = months_ahead(self.member, self.monthly, today=date(self.year, self.month, 1))
        self.assertEqual((ahead[0]["held_days"], ahead[0]["remaining_days"]), (0.5, 1.5))

    def test_editing_a_waiting_request_counts_its_new_days_once(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), self.day(1))))
        mine = self.last()
        # moving the same 2 days inside the month is fine (its own days are not counted twice)
        response = self.api(self.member_user).put(f"/api/leave/user-request/{mine.pk}/", {
            "leave_type_id": self.monthly.pk, "start_date": str(self.day(2)), "end_date": str(self.day(3)),
            "start_date_breakdown": "full_day", "end_date_breakdown": "full_day", "description": "dời ngày",
        }, format="json")
        self.assertIn(response.status_code, (200, 201), response.content)
        response = self.api(self.member_user).put(f"/api/leave/user-request/{mine.pk}/", {
            "leave_type_id": self.monthly.pk, "start_date": str(self.day(1)), "end_date": str(self.day(3)),
            "start_date_breakdown": "full_day", "end_date_breakdown": "full_day", "description": "3 ngày",
        }, format="json")
        self.assertEqual(response.status_code, 400)

    def test_history_over_the_limit_is_not_rewritten_and_still_editable_by_note(self):
        # Requests from before the policy (made without it) stay as they are.
        start = self.day(0)
        old = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.member, leave_type_id=self.monthly, start_date=start, end_date=start + timedelta(days=3),
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=4,
            approved_available_days=4, approved_carryforward_days=0, description="cũ", status="approved",
        )])[0]
        grant(self.hr_user, "add_leaverequest")
        body = self.edit_payload(old, "HR ghi chú đơn cũ")
        self.web(self.hr_user).post(f"/leave/request-update/{old.pk}/", body, **HTMX)
        row = LeaveRequest._base_manager.get(pk=old.pk)
        self.assertEqual((row.description, row.requested_days, row.approved_available_days), ("HR ghi chú đơn cũ", 4, 4))


class ReconcileReportTests(LimitWorld, TestCase):
    def setUp(self):
        self.build()

    def test_mismatched_approved_days_and_months_over_the_limit_are_reported_read_only(self):
        from leave.management.commands.leave_reconcile import leave_request_checks

        start = self.day(0)
        bad, over = LeaveRequest._base_manager.bulk_create([
            LeaveRequest(  # what an old edit of an approved leave left: 1 day recorded, 2 taken
                employee_id=self.member, leave_type_id=self.monthly, start_date=start, end_date=start,
                start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=1,
                approved_available_days=2, approved_carryforward_days=0, description="lệch", status="approved",
            ),
            LeaveRequest(  # 3 days in one month, from before the limit
                employee_id=self.member, leave_type_id=self.annual, start_date=self.day(1), end_date=self.day(3),
                start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=3,
                approved_available_days=3, approved_carryforward_days=0, description="cũ", status="approved",
            ),
        ])
        before = list(LeaveRequest._base_manager.order_by("pk").values_list("pk", "requested_days", "status"))
        funds = list(AvailableLeave._base_manager.order_by("pk").values_list("pk", "available_days"))
        report = leave_request_checks()
        self.assertIn(bad.pk, [row["leave_request_id"] for row in report["approved_days_mismatch"]])
        self.assertNotIn(over.pk, [row["leave_request_id"] for row in report["approved_days_mismatch"]])
        months = [(r["employee_id"], r["year"], r["month"], r["held_days"]) for r in report["months_over_limit"]]
        self.assertIn((self.member.pk, self.year, self.month, 4.0), months)
        self.assertEqual(list(LeaveRequest._base_manager.order_by("pk").values_list("pk", "requested_days", "status")), before)
        self.assertEqual(list(AvailableLeave._base_manager.order_by("pk").values_list("pk", "available_days")), funds)


class ApprovedLeaveEditTests(LimitWorld, TestCase):
    def setUp(self):
        self.build()
        grant(self.hr_user, "add_leaverequest")

    def approved_two_days(self):
        self.assertTrue(self.created(self.ask_api(self.day(0), self.day(1))))
        leave = self.last()
        self.approve_fully(leave)
        self.assertEqual(self.consistent(leave), ("approved", 2, 10))
        return leave

    # 17
    def test_hr_can_not_change_the_days_of_an_approved_leave_on_the_web(self):
        leave = self.approved_two_days()
        body = self.edit_payload(leave, "HR rút còn 1 ngày")
        body["end_date"] = body["start_date"]
        self.web(self.hr_user).post(f"/leave/request-update/{leave.pk}/", body, **HTMX)
        row = LeaveRequest._base_manager.get(pk=leave.pk)
        self.assertEqual((row.end_date, row.requested_days), (self.day(1), 2))
        self.assertEqual(self.consistent(leave), ("approved", 2, 10))

    def test_hr_can_not_move_an_approved_leave_to_another_type_or_employee(self):
        leave = self.approved_two_days()
        body = self.edit_payload(leave, "HR đổi loại")
        body["leave_type_id"] = self.regime.pk
        self.web(self.hr_user).post(f"/leave/request-update/{leave.pk}/", body, **HTMX)
        self.assertEqual(LeaveRequest._base_manager.get(pk=leave.pk).leave_type_id_id, self.monthly.pk)
        self.assertEqual(self.consistent(leave), ("approved", 2, 10))

    def test_the_hr_api_refuses_too(self):
        from leave.monthly_limit import APPROVED_EDIT_REFUSED

        leave = self.approved_two_days()
        body = self.edit_payload(leave, "API rút ngày")
        body["end_date"] = body["start_date"]
        response = self.api(self.hr_user).put(f"/api/leave/request/{leave.pk}/", body, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Đơn đã được duyệt nên không thể sửa qua ứng dụng", response.content.decode())
        self.assertTrue(APPROVED_EDIT_REFUSED)  # the web wording, shown by the forms
        self.assertEqual(self.consistent(leave), ("approved", 2, 10))

    def test_the_model_refuses_any_other_path(self):
        from leave.monthly_limit import ApprovedLeaveChangeError

        leave = self.approved_two_days()
        row = LeaveRequest._base_manager.get(pk=leave.pk)
        row.end_date = row.start_date
        with self.assertRaises(ApprovedLeaveChangeError):
            row.save()
        self.assertEqual(self.consistent(leave), ("approved", 2, 10))

    # 18
    def test_hr_edits_the_note_only_and_the_days_stay(self):
        from base.models import Holidays

        leave = self.approved_two_days()
        # a holiday added afterwards on one of its days never changes it
        LeaveType._base_manager.filter(pk=self.monthly.pk).update(exclude_holiday="yes")
        Holidays.objects.create(name="Lễ sau", start_date=self.day(1), end_date=self.day(1))
        body = self.edit_payload(leave, "HR chỉ sửa ghi chú")
        self.web(self.hr_user).post(f"/leave/request-update/{leave.pk}/", body, **HTMX)
        row = LeaveRequest._base_manager.get(pk=leave.pk)
        self.assertEqual((row.description, row.requested_days), ("HR chỉ sửa ghi chú", 2))
        self.assertEqual(self.consistent(leave), ("approved", 2, 10))

    def test_a_legacy_approved_row_without_an_end_date_still_takes_a_note(self):
        start = self.day(5)
        old = LeaveRequest._base_manager.bulk_create([LeaveRequest(
            employee_id=self.member, leave_type_id=self.monthly, start_date=start, end_date=None,
            start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=1,
            approved_available_days=1, approved_carryforward_days=0, description="cũ", status="approved",
        )])[0]
        body = self.edit_payload(old, "ghi chú đơn không có ngày kết thúc")
        body["end_date"] = body["start_date"]
        self.web(self.hr_user).post(f"/leave/request-update/{old.pk}/", body, **HTMX)
        row = LeaveRequest._base_manager.get(pk=old.pk)
        self.assertEqual((row.description, row.requested_days), ("ghi chú đơn không có ngày kết thúc", 1))

    def test_the_adjustment_path_is_a_cancellation_then_a_new_request(self):
        leave = self.approved_two_days()
        self.assertEqual(self.cancel(leave).status_code, 200)
        self.assertEqual(self.approve_cancellation(self.manager_user, self.waiting_cancellation(leave)).status_code, 200)
        self.assertEqual(self.consistent(leave), ("cancelled", 0, 12))
        self.assertTrue(self.created(self.ask_api(self.day(0))))  # the corrected 1 day


@skipUnless(connection.vendor == "postgresql", "row locks need PostgreSQL (SQLite has none)")
class ConcurrentLimitTests(LimitWorld, TransactionTestCase):
    def setUp(self):
        self.build()

    def race(self, *calls):
        barrier = threading.Barrier(len(calls))
        results = [None] * len(calls)

        def run(index, call):
            try:
                barrier.wait(timeout=30)
                results[index] = call()
            except Exception as error:  # noqa: BLE001
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

    # 8
    def test_two_requests_at_once_can_not_both_pass_the_limit(self):
        with slow(LeaveRequest):
            results = self.race(lambda: self.ask_api(self.day(0), self.day(1)),
                                lambda: self.ask_api(self.day(2), self.day(3)))
        codes = sorted(getattr(r, "status_code", 500) for r in results)
        self.assertEqual(codes, [201, 400], results)
        self.assertEqual(self.usage()["held_days"], 2.0)

    def test_web_and_api_at_once_can_not_both_pass_the_limit(self):
        with slow(LeaveRequest):
            self.race(lambda: self.ask_api(self.day(0), self.day(1)), lambda: self.ask_web(self.day(2), self.day(3)))
        self.assertLessEqual(self.usage()["held_days"], 2.0)

    def test_two_hr_api_creates_at_once_can_not_both_pass_and_never_500(self):
        grant(self.hr_user, "add_leaverequest")

        def hr_create(start, end):
            return self.api(self.hr_user).post("/api/leave/request/", {
                "employee_id": self.member.pk, "leave_type_id": self.monthly.pk,
                "start_date": str(start), "end_date": str(end),
                "start_date_breakdown": "full_day", "end_date_breakdown": "full_day", "description": "HR tạo",
            }, format="json")

        with slow(LeaveRequest):
            results = self.race(lambda: hr_create(self.day(0), self.day(1)), lambda: hr_create(self.day(2), self.day(3)))
        codes = sorted(getattr(r, "status_code", 500) for r in results)
        self.assertEqual(codes, [201, 400], results)
        self.assertEqual(self.usage()["held_days"], 2.0)

    # 19
    def test_approve_one_and_refund_another_at_once_stays_consistent(self):
        self.assertTrue(self.created(self.ask_api(self.day(0))))
        first = self.last()
        self.assertEqual(self.approve(self.manager_user, first).status_code, 200)
        self.assertEqual(self.cancel(first).status_code, 200)
        nxt = self.day(0, self.next_year, self.next_month)
        self.assertTrue(self.created(self.ask_api(nxt)))  # 1 day: one approval step
        second = self.last()
        with slow(LeaveRequest):
            self.race(lambda: self.approve_cancellation(self.manager_user, self.waiting_cancellation(first)),
                      lambda: self.approve(self.manager_user, second))
        self.assertEqual(self.fund(), 12 - 1)  # first refunded, second taken, each once
        self.assertEqual(self.consistent(first)[0:2], ("cancelled", 0))
