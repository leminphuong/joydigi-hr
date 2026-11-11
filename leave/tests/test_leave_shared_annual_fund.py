"""One annual fund for two request types — JOYDIGI policy of 2026-11-11.

"NGHỈ PHÉP THÁNG" (the pool, holding the fund) and "Nghỉ Phép Năm" (drawing
on it) request days from ONE fund: 1 day a month, 12 for the year. Fake data
in the production shape: ``self.me`` plays NV006 (12 granted, 1 day used →
11), ``self.colleague`` the 7 who hold two separate funds (12 monthly + an
annual balance of 10 after 2 days used).

Numbered like the report (LEAVE_SHARED_ANNUAL_BALANCE_FIX_JOYDIGI.md); the
Flutter side (20) is tested in the app repository.
"""

from datetime import date, timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_user
from leave.models import AvailableLeave, LeaveRequest, LeaveType
from leave.services import ensure_available_leave
from leave.tests.test_leave_sync import ApiFixtures

YEAR = date.today().year
NEXT_MONDAY = date.today() + timedelta(days=(7 - date.today().weekday()) or 7)


def writes(queries, *tables):
    return [
        q["sql"][:120]
        for q in queries
        if q["sql"].lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE") and any(t in q["sql"] for t in tables)
    ]


class SharedFundFixtures(ApiFixtures):
    def setUp(self):
        super().setUp()
        self.monthly = self.leave_type("NGHI PHEP THANG", self.company_a, total_days=1, count=1)
        self.annual = self.leave_type("Nghi Phep Nam", self.company_a, total_days=1, count=1)
        self.regime = self.leave_type("Nghi Che Do", self.company_a, total_days=30, count=30)
        self.comp = self.leave_type("Compensatory Leave Type", self.company_a, is_compensatory_leave=True, total_days=1)
        self.b_type = self.leave_type("Phep B", self.company_b, total_days=12, count=12)
        self.save(self.comp, is_active=False)
        self.colleague = self.employee(self.company_a)
        self.other_company = self.employee(self.company_b)
        for person in (self.me, self.colleague, self.other_company):
            EmployeeWorkInformation._base_manager.filter(employee_id=person).update(date_joining=date(YEAR - 1, 6, 1))
        # NV006: 12 granted, 1 day approved → 11 / 11; never had "Nghỉ Phép Năm".
        self.set_balance(self.me, self.monthly, 11)
        self.forget(self.me, self.annual)
        self.approved(self.me, self.monthly, date(YEAR, 11, 6), 1)
        # A colleague with two separate funds: monthly 12 + annual 10 (2 used).
        self.set_balance(self.colleague, self.monthly, 12)
        self.set_balance(self.colleague, self.annual, 10)
        self.approved(self.colleague, self.annual, date(YEAR, 3, 2), 2)
        self.admin = make_user("shared_fund_admin", password="fake-pass-1", is_superuser=True)
        self.employee(self.company_b, user=self.admin)  # outside the converted company
        self.admin_api = APIClient()
        self.admin_api.force_authenticate(self.admin)

    def set_balance(self, employee, leave_type, days):
        AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id=leave_type).update(
            available_days=days, total_leave_days=days
        )

    def forget(self, employee, leave_type):
        AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id=leave_type).delete()
        AvailableLeave.history.filter(employee_id=employee.pk, leave_type_id=leave_type.pk).delete()

    def approved(self, employee, leave_type, day, days):
        return LeaveRequest._base_manager.bulk_create([
            LeaveRequest(
                employee_id=employee, leave_type_id=leave_type, start_date=day, end_date=day,
                start_date_breakdown="full_day", end_date_breakdown="full_day", requested_days=days,
                approved_available_days=days, description="history", status="approved",
            )
        ])[0]

    def convert(self, *extra, entitlement="12", as_of="11", expect=None, dry=False):
        args = [
            "--pool", str(self.monthly.pk), "--alias", str(self.annual.pk), "--company", str(self.company_a.pk),
            "--year", str(YEAR), "--as-of", f"{YEAR}-{as_of}", "--entitlement", entitlement, *extra,
        ]
        if dry:
            args.append("--dry-run")
        elif expect is not None:
            args += ["--expect-count", str(expect)]
        out = StringIO()
        call_command("convert_shared_annual_balance", *args, stdout=out)
        self.monthly.refresh_from_db()
        self.annual.refresh_from_db()
        return out.getvalue()

    def accrue(self, month, expect=None, dry=False, *extra):
        args = ["--pool", str(self.monthly.pk), "--company", str(self.company_a.pk), "--period", f"{YEAR}-{month:02d}", *extra]
        args += ["--dry-run"] if dry else ["--expect-count", str(expect)]
        out = StringIO()
        call_command("accrue_shared_leave", *args, stdout=out)
        return out.getvalue()

    def fund(self, employee):
        return self.balance(employee, self.monthly)

    def ask(self, leave_type, day, days=1):
        end = day + timedelta(days=days - 1)
        return self.api.post(
            "/api/leave/user-request/",
            {"leave_type_id": leave_type.pk, "start_date": str(day), "start_date_breakdown": "full_day",
             "end_date": str(end), "end_date_breakdown": "full_day", "description": "test"},
            format="json",
        )

    def approve(self, request_id):
        return self.admin_api.put(f"/api/leave/approve/{request_id}/", {}, format="json")

    def options(self):
        return {row["leave_type_id"]["id"]: row for row in super().options()["results"]}


class SharedAnnualFundTests(SharedFundFixtures):
    def test_01_each_month_accrues_exactly_one_day(self):
        self.convert(entitlement="10", as_of="10", expect=2)  # accrued Jan–Oct
        before = self.fund(self.me).available_days
        self.accrue(11, expect=2)
        self.assertEqual(self.fund(self.me).available_days, before + 1)
        self.accrue(11, expect=0)  # the same month again: nothing
        self.assertEqual(self.fund(self.me).available_days, before + 1)
        self.accrue(12, expect=2)
        self.assertEqual(self.fund(self.me).available_days, before + 2)

    def test_01b_no_accrual_before_the_fund_has_a_starting_point(self):
        # The colleague was given 12 upfront: crediting December now would
        # make 13. Without a conversion marker for the year, nobody is credited.
        out = self.accrue(12, dry=True)
        self.assertIn(f"employee={self.colleague.pk} accrued=0 credit=0 action=skip (Chưa có mốc của năm", out)
        self.assertIn("credits            : 0", out)

    def test_02_the_year_never_passes_12(self):
        self.convert(entitlement="12", as_of="11", expect=2)
        out = self.accrue(12, dry=True)
        self.assertIn(f"employee={self.me.pk} accrued=12 credit=0 action=skip (Đã đủ 12 ngày của năm)", out)
        with self.assertRaisesMessage(CommandError, "--expect-count 1 but 0 credit"):
            self.accrue(12, expect=1)

    def test_03_both_request_types_share_one_balance(self):
        self.convert(expect=2)
        rows = self.options()
        self.assertEqual(rows[self.monthly.pk]["remaining_days"], rows[self.annual.pk]["remaining_days"])
        self.assertEqual(rows[self.annual.pk]["shared_balance"]["fund_type_id"], self.monthly.pk)
        self.assertTrue(rows[self.annual.pk]["leave_type_id"]["name"].endswith("· chung quỹ phép năm"))
        shared = rows[self.annual.pk]["shared_balance"]
        # What Profile shows as QUỸ PHÉP NĂM: granted 12 = 11 left + 1 used.
        self.assertEqual(
            (shared["year"], shared["granted_days"], shared["used_days_this_year"], shared["remaining_days"]),
            (YEAR, 12, 1, 11),
        )
        self.assertEqual([t["name"] for t in shared["types"]], ["NGHI PHEP THANG", "Nghi Phep Nam"])

    def test_04_never_24_days(self):
        self.convert(expect=2)
        self.assertEqual(self.fund(self.colleague).available_days, 10, "12 − 2 used, not 12 + 10")
        old = AvailableLeave._base_manager.get(employee_id=self.colleague, leave_type_id=self.annual)
        self.assertFalse(old.is_active, "the old annual balance is switched off, kept")
        rows = self.client_for(self.colleague)
        self.assertEqual({r["remaining_days"] for r in rows.values() if r["leave_type_id"]["id"] in (self.monthly.pk, self.annual.pk)}, {10})
        with self.assertRaises(ValidationError):
            AvailableLeave(employee_id=self.colleague, leave_type_id=self.annual, available_days=12).save()
        with self.assertRaisesMessage(CommandError, "shares the fund"):
            call_command("backfill_available_leave", "--dry-run", "--leave-type", str(self.annual.pk),
                         "--grant", f"{self.annual.pk}=12", stdout=StringIO())

    def test_05_a_monthly_request_reduces_the_shared_balance(self):
        self.convert(expect=2)
        response = self.ask(self.monthly, NEXT_MONDAY)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.approve(response.json()["id"]).status_code, 200)
        self.assertEqual(self.fund(self.me).available_days, 10)

    def test_06_an_annual_request_reduces_the_same_balance(self):
        self.convert(expect=2)
        response = self.ask(self.annual, NEXT_MONDAY)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.approve(response.json()["id"]).status_code, 200)
        self.assertEqual(self.fund(self.me).available_days, 10)
        self.assertFalse(AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id=self.annual).exists())

    def test_07_approving_twice_deducts_once(self):
        self.convert(expect=2)
        request_id = self.ask(self.annual, NEXT_MONDAY).json()["id"]
        self.approve(request_id)
        self.assertEqual(self.approve(request_id).status_code, 400)
        self.assertEqual(self.fund(self.me).available_days, 10)

    def test_08_rejecting_twice_refunds_once_and_to_the_right_person(self):
        self.convert(expect=2)
        request_id = self.ask(self.annual, NEXT_MONDAY).json()["id"]
        self.approve(request_id)
        admin_rows_before = sorted(AvailableLeave._base_manager.filter(employee_id__employee_user_id=self.admin).values_list("pk", "available_days"))
        self.assertEqual(self.admin_api.put(f"/api/leave/reject/{request_id}/", {}, format="json").status_code, 200)
        self.assertEqual(self.fund(self.me).available_days, 11, "refunded to the employee's fund")
        self.assertEqual(self.admin_api.put(f"/api/leave/reject/{request_id}/", {}, format="json").status_code, 400)
        self.assertEqual(self.fund(self.me).available_days, 11, "not twice")
        self.assertEqual(
            sorted(AvailableLeave._base_manager.filter(employee_id__employee_user_id=self.admin).values_list("pk", "available_days")),
            admin_rows_before, "the rejecting manager's balance is untouched",
        )

    def test_09_no_request_over_the_shared_balance_pending_included(self):
        self.convert(expect=2)  # 11 left
        self.assertEqual(self.ask(self.monthly, NEXT_MONDAY, days=12).status_code, 400)
        first = self.ask(self.monthly, NEXT_MONDAY, days=5)  # Mon–Fri, pending
        self.assertEqual(first.status_code, 201, first.content)
        second = self.ask(self.annual, NEXT_MONDAY + timedelta(days=7), days=5)
        self.assertEqual(second.status_code, 201, "5 + 5 ≤ 11")
        third = self.ask(self.annual, NEXT_MONDAY + timedelta(days=14), days=5)
        self.assertEqual(third.status_code, 400, "5 + 5 + 5 > 11: the pending days are held")

    def test_10_syncing_never_creates_a_second_fund(self):
        self.convert(expect=2)
        newcomer = self.employee(self.company_a)
        ensure_available_leave(newcomer, self.annual)
        self.assertFalse(AvailableLeave._base_manager.filter(employee_id=newcomer, leave_type_id=self.annual).exists())
        self.assertEqual(AvailableLeave._base_manager.filter(employee_id=newcomer, leave_type_id=self.monthly).count(), 1)

    def test_11_reruns_add_nothing(self):
        self.convert(expect=2)
        before = sorted(AvailableLeave._base_manager.values_list("pk", "available_days", "is_active"))
        self.assertIn("changes            : 0", self.convert(expect=0))
        with self.assertRaisesMessage(CommandError, "--expect-count 2 but 0 change"):
            self.convert(expect=2)
        self.assertEqual(sorted(AvailableLeave._base_manager.values_list("pk", "available_days", "is_active")), before)

    def test_11b_no_conversion_rerun_after_a_monthly_accrual(self):
        self.convert(entitlement="11", as_of="11", expect=2)
        self.accrue(12, expect=2)
        out = self.convert(entitlement="11", as_of="11", dry=True)
        self.assertIn("không chạy lại chuyển đổi", out, "an accrued December is never undone")

    def test_12_nghi_che_do_is_untouched(self):
        regime = sorted(AvailableLeave._base_manager.filter(leave_type_id=self.regime).values_list("pk", "available_days"))
        self.convert(entitlement="10", as_of="10", expect=2)
        self.accrue(11, expect=2)
        self.assertEqual(sorted(AvailableLeave._base_manager.filter(leave_type_id=self.regime).values_list("pk", "available_days")), regime)
        self.assertIsNone(LeaveType._base_manager.get(pk=self.regime.pk).shared_balance_with_id)

    def test_13_compensatory_stays_off(self):
        self.convert(expect=2)
        self.comp.refresh_from_db()
        self.assertFalse(self.comp.is_active)
        self.assertNotIn(self.comp.pk, self.options())

    def test_14_other_companies_are_untouched(self):
        before = sorted(AvailableLeave._base_manager.filter(employee_id=self.other_company).values_list("pk", "available_days", "is_active"))
        self.convert(expect=2)
        self.accrue(12, expect=0)
        self.assertEqual(sorted(AvailableLeave._base_manager.filter(employee_id=self.other_company).values_list("pk", "available_days", "is_active")), before)
        with self.assertRaisesMessage(CommandError, "cùng công ty"):
            call_command("convert_shared_annual_balance", "--pool", str(self.b_type.pk), "--alias", str(self.regime.pk),
                         "--year", str(YEAR), "--as-of", f"{YEAR}-11", "--entitlement", "12", "--dry-run", stdout=StringIO())

    def test_15_nv006_history_is_kept(self):
        requests = sorted(LeaveRequest._base_manager.filter(employee_id=self.me).values_list("pk", "leave_type_id", "status", "requested_days"))
        history = AvailableLeave.history.filter(employee_id=self.me.pk).count()
        self.convert(expect=2)
        self.assertEqual(sorted(LeaveRequest._base_manager.filter(employee_id=self.me).values_list("pk", "leave_type_id", "status", "requested_days")), requests)
        self.assertGreaterEqual(AvailableLeave.history.filter(employee_id=self.me.pk).count(), history)
        self.assertFalse(AvailableLeave.history.filter(employee_id=self.me.pk, history_type="-").exists(), "nothing deleted")

    def test_16_nv006_11_of_11_is_read_not_guessed(self):
        out = self.convert(dry=True)
        self.assertIn(f"FUND  employee={self.me.pk} entitlement=12 approved=#{self.monthly.pk}:1 pending=0 current=11 -> new=11", out)
        self.assertIn("action=baseline", out.split(f"employee={self.me.pk}")[1].splitlines()[0], "days kept, baseline written")
        out = self.convert(entitlement="11", as_of="11", dry=True)  # if the 12 must accrue month by month
        self.assertIn(f"employee={self.me.pk} entitlement=11 approved=#{self.monthly.pk}:1 pending=0 current=11 -> new=10", out)
        out = StringIO()
        call_command("convert_shared_annual_balance", "--pool", str(self.monthly.pk), "--alias", str(self.annual.pk),
                     "--year", str(YEAR), "--as-of", f"{YEAR}-11", "--dry-run", stdout=out)
        self.assertIn("Chưa xác định mức quyền lợi", out.getvalue(), "no entitlement given: nothing assumed")

    def test_17_a_new_employee_gets_one_fund(self):
        self.convert(expect=2)
        newcomer = self.employee(self.company_a)
        self.assertEqual(self.fund(newcomer).available_days, 1, "the pool's configured opening days")
        self.assertFalse(AvailableLeave._base_manager.filter(employee_id=newcomer, leave_type_id=self.annual).exists())

    def test_18_an_old_employee_without_a_fund_gets_it_from_the_conversion(self):
        missing = self.employee(self.company_a)
        self.forget(missing, self.monthly)
        self.approved(missing, self.annual, date(YEAR, 2, 2), 3)
        out = self.convert(dry=True)
        self.assertIn(f"employee={missing.pk} entitlement=12 approved=#{self.annual.pk}:3", out)
        self.convert(expect=3)  # NV006 (baseline), the colleague (update), this employee (create)
        self.assertEqual(self.fund(missing).available_days, 9)

    def test_19_get_requests_write_nothing(self):
        self.convert(expect=2)
        hr = make_user("shared_fund_hr", password="fake-pass-1", is_superuser=True)
        self.employee(self.company_a, user=hr)  # sees company A profiles
        self.client.force_login(hr)
        with CaptureQueriesContext(connection) as queries:
            self.options()
            tab = self.client.get(reverse("individual-leave-tab-list", kwargs={"pk": self.me.pk}), HTTP_HX_REQUEST="true")
        self.assertEqual(tab.status_code, 200)
        self.assertEqual(writes(queries, "leave_"), [])
        html = tab.content.decode()
        self.assertIn('data-testid="shared-fund"', html)
        self.assertIn("QUỸ PHÉP NĂM", html)

    def client_for(self, employee):
        user = make_user(f"shared_fund_user_{employee.pk}", password="fake-pass-1")
        employee.employee_user_id = user
        employee.save()
        api = APIClient()
        api.force_authenticate(user)
        return {row["leave_type_id"]["id"]: row for row in api.get("/api/leave/available-leave/?page_size=100").json()["results"]}
