"""Automatic leave balances for every employee — the 2026-11-11 fix.

Same shape as production JOYDIGI (fake data): company A with NGHỈ PHÉP THÁNG,
Nghỉ Phép Năm and Nghỉ Chế Độ active, compensatory switched off; company B
with its own type. ``self.me`` plays NV006: it holds only the monthly type
(11 / 11) and never had the other two.

Numbered like the report (LEAVE_AUTO_SYNC_FIX_JOYDIGI.md, section 4). The
Flutter side (17) is tested in the app repository.
"""

from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from base.checkin_portal import _sync_annual_leave
from base.models import CheckInPolicy
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_user
from leave.balance_sync import plan_missing_balances, run_scheduled_sync
from leave.forms import UpdateLeaveTypeForm
from leave.models import AvailableLeave, LeaveRequest, LeaveType
from leave.services import ensure_available_leave
from leave.tests.test_leave_sync import ApiFixtures

AUTO_ON = override_settings(LEAVE_AUTO_SYNC_ENABLED=True)
NEXT_MONDAY = date.today() + timedelta(days=(7 - date.today().weekday()) or 7)


def writes(queries, *tables):
    """INSERT / UPDATE / DELETE statements touching any of ``tables``."""
    found = []
    for query in queries:
        sql = query["sql"].lstrip()
        if sql[:6].upper() in ("INSERT", "UPDATE", "DELETE") and any(t in sql for t in tables):
            found.append(sql[:120])
    return found


class AutoSyncFixtures(ApiFixtures):
    def setUp(self):
        super().setUp()
        self.monthly = self.leave_type("NGHI PHEP THANG", self.company_a, total_days=2, count=2)
        # "After the Admin's approved fix": 12 days configured (production: 1).
        self.annual = self.leave_type("Nghỉ Phép Năm", self.company_a, total_days=12, count=12)
        self.regime = self.leave_type("Nghi Che Do", self.company_a, total_days=30, count=30)
        self.comp = self.leave_type("Compensatory Leave Type", self.company_a, is_compensatory_leave=True, total_days=1)
        self.b_type = self.leave_type("Phep B", self.company_b, total_days=12, count=12)
        self.save(self.comp, is_active=False)
        self.colleague = self.employee(self.company_a)
        self.other_company = self.employee(self.company_b)
        AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id=self.monthly).update(
            available_days=11, total_leave_days=11
        )
        # The legacy gap (NV006): never opened — no row, no history.
        self.never_opened(self.me, self.annual, self.regime)

    def never_opened(self, employee, *types):
        AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id__in=types).delete()
        AvailableLeave.history.filter(employee_id=employee.pk, leave_type_id__in=[t.pk for t in types]).delete()

    def revoke(self, employee, leave_type):
        """What HR's delete button does: a row delete, recorded in history."""
        AvailableLeave._base_manager.get(employee_id=employee, leave_type_id=leave_type).delete()

    def balances(self, employee=None):
        rows = AvailableLeave._base_manager.all()
        if employee is not None:
            rows = rows.filter(employee_id=employee)
        return sorted(
            rows.values_list("employee_id", "leave_type_id", "available_days", "carryforward_days", "total_leave_days")
        )

    def days(self, employee, leave_type):
        return self.balance(employee, leave_type).available_days

    def result_names(self, body):
        return sorted(row["leave_type_id"]["name"] for row in body["results"])


class AutoSyncScenarioTests(AutoSyncFixtures):
    @AUTO_ON
    def test_01_nv006_gets_every_active_type(self):
        self.assertEqual(self.result_names(self.options()), ["NGHI PHEP THANG"])
        summary = run_scheduled_sync()
        self.assertEqual(summary["opened"], 2)
        body = self.options()
        self.assertEqual(self.result_names(body), ["NGHI PHEP THANG", "Nghi Che Do", "Nghỉ Phép Năm"])
        self.assertEqual(body["unassigned"], [])

    @AUTO_ON
    def test_02_the_monthly_11_of_11_is_kept(self):
        run_scheduled_sync()
        row = self.balance(self.me, self.monthly)
        self.assertEqual((row.available_days, row.total_leave_days), (11, 11))

    @AUTO_ON
    def test_03_annual_leave_gets_the_configured_and_approved_days(self):
        run_scheduled_sync()
        self.assertEqual(self.days(self.me, self.annual), 12)

    def test_03b_the_leave_type_form_derives_seeded_days_from_what_hr_typed(self):
        # The form shows "Tổng số ngày" (count) and keeps total_days — what
        # balances are seeded with — in a hidden input a script fills. A
        # stale hidden value (the production 1-day symptom) no longer wins.
        from leave.forms import _derive_total_days

        for count, period, stale, expected in ((12, "day", 1, 12), (1, "month", 1, 30), (2, "year", 1, 730)):
            data = {"limit_leave": True, "count": count, "period_in": period, "total_days": stale}
            _derive_total_days(data)
            self.assertEqual(data["total_days"], expected, (count, period))
        unlimited = {"limit_leave": False, "count": 1, "period_in": "day", "total_days": 1e5}
        _derive_total_days(unlimited)
        self.assertEqual(unlimited["total_days"], 1e5, "an unlimited type keeps its sentinel")
        from leave.forms import LeaveTypeForm

        for form_class in (LeaveTypeForm, UpdateLeaveTypeForm):  # both forms apply the rule
            self.assertIn("_derive_total_days", form_class.clean.__code__.co_names, form_class.__name__)

    @AUTO_ON
    def test_04_regime_leave_gets_30(self):
        run_scheduled_sync()
        self.assertEqual(self.days(self.me, self.regime), 30)

    @AUTO_ON
    def test_05_compensatory_leave_stays_off_and_is_never_granted(self):
        self.never_opened(self.me, self.comp)
        run_scheduled_sync()
        self.comp.refresh_from_db()
        self.assertFalse(self.comp.is_active)
        self.assertFalse(self.has(self.me, self.comp))
        # Even an active compensatory type is not handed out by the sync.
        self.save(self.comp, is_active=True)
        self.never_opened(self.me, self.comp)
        run_scheduled_sync()
        self.assertFalse(self.has(self.me, self.comp))

    def test_06_a_new_employee_gets_every_type_at_once(self):
        newcomer = self.employee(self.company_a)
        self.assertEqual(self.days(newcomer, self.annual), 12)
        self.assertEqual(self.days(newcomer, self.regime), 30)
        self.assertEqual(self.days(newcomer, self.monthly), 2)
        self.assertFalse(self.has(newcomer, self.b_type))

    @AUTO_ON
    def test_07_an_old_employee_with_a_gap_is_completed(self):
        self.never_opened(self.colleague, self.regime)
        run_scheduled_sync()
        self.assertEqual(self.days(self.colleague, self.regime), 30)

    @AUTO_ON
    def test_08_never_twice(self):
        run_scheduled_sync()
        run_scheduled_sync()
        self.assertEqual(run_scheduled_sync()["would_open"], 0)
        for leave_type in (self.annual, self.regime, self.monthly):
            self.assertEqual(AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id=leave_type).count(), 1)
        # Two callers at once: the second gets the existing row back.
        first, created = ensure_available_leave(self.colleague, self.annual)
        again, created_again = ensure_available_leave(self.colleague, self.annual)
        self.assertEqual((first.pk, created_again), (again.pk, False))

    @AUTO_ON
    def test_09_never_another_company(self):
        run_scheduled_sync()
        self.assertFalse(self.has(self.other_company, self.annual))
        self.assertFalse(self.has(self.me, self.b_type))

    @AUTO_ON
    def test_10_a_revoked_balance_is_not_restored_by_any_path(self):
        self.revoke(self.colleague, self.annual)
        run_scheduled_sync()
        self.assertFalse(self.has(self.colleague, self.annual), "scheduled sync")
        self.save(self.annual, is_active=False)
        self.save(self.annual, is_active=True)
        self.assertFalse(self.has(self.colleague, self.annual), "re-activating the type")
        with self.captureOnCommitCallbacks(execute=True):
            work = EmployeeWorkInformation._base_manager.get(employee_id=self.colleague)
            work.company_id = self.company_b
            work.save()
        with self.captureOnCommitCallbacks(execute=True):
            work.company_id = self.company_a
            work.save()
        self.assertFalse(self.has(self.colleague, self.annual), "a transfer and back")
        out = StringIO()
        call_command("backfill_available_leave", "--dry-run", "--use-type-total", stdout=out)
        self.assertIn(f"REVOKED BEFORE  employee={self.colleague.pk} leave_type={self.annual.pk}", out.getvalue())

    @AUTO_ON
    def test_11_an_existing_balance_is_never_overwritten(self):
        AvailableLeave._base_manager.filter(employee_id=self.colleague, leave_type_id=self.annual).update(
            available_days=3, carryforward_days=1, total_leave_days=4
        )
        before = self.balances(self.colleague)
        run_scheduled_sync()
        _sync_annual_leave(self.company_a, 20)  # the type's days change...
        run_scheduled_sync()
        self.assertEqual(self.balances(self.colleague), before, "...balances do not")

    def test_12_saving_a_profile_adds_nothing(self):
        before = self.balances()
        work = EmployeeWorkInformation._base_manager.get(employee_id=self.me)
        for joined in (date(2024, 1, 2), date(2024, 1, 3), None):
            with self.captureOnCommitCallbacks(execute=True):
                work.date_joining = joined
                work.save()
        self.assertEqual(self.balances(), before, "no gap opened, no day added")
        self.assertFalse(self.has(self.me, self.annual))

    def test_13_get_requests_write_nothing(self):
        hr = make_user("leave_autosync_hr", password="fake-pass-1", is_superuser=True)
        self.employee(self.company_a, user=hr)
        self.client.force_login(hr)
        policies = CheckInPolicy._base_manager.count()
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(reverse("checkin-settings")).status_code, 200)
            self.options()
            tab = self.client.get(reverse("individual-leave-tab-list", kwargs={"pk": self.me.pk}), HTTP_HX_REQUEST="true")
            self.assertEqual(tab.status_code, 200)
        self.assertEqual(writes(queries, "leave_", "base_checkinpolicy"), [])
        self.assertEqual(CheckInPolicy._base_manager.count(), policies, "a GET no longer creates the policy")

    def test_14_no_duplicate_leave_type_and_no_days_added(self):
        types, before = LeaveType._base_manager.count(), self.balances()
        b_annual = self.leave_type("Nghỉ phép năm", self.company_b, total_days=5, count=5)
        types += 1
        preview = _sync_annual_leave(self.company_a, 14)
        self.assertEqual(preview["status"], "change")
        self.assertEqual(LeaveType._base_manager.count(), types, "the differently-written name is found, not re-created")
        self.annual.refresh_from_db()
        self.assertEqual((self.annual.total_days, self.annual.count), (14, 14))
        b_annual.refresh_from_db()
        self.assertEqual(b_annual.total_days, 5, "the other company's type is untouched")
        self.assertEqual([row for row in self.balances() if row[1] != b_annual.pk], before, "no balance changed")
        LeaveType._base_manager.filter(pk=self.annual.pk).update(name="Phep nam (cu)")
        self.assertEqual(_sync_annual_leave(self.company_a, 12)["status"], "none")
        self.assertEqual(LeaveType._base_manager.count(), types, "no type created when there is none")

    @AUTO_ON
    def test_15_the_api_answers_consistently(self):
        body = self.options()
        self.assertEqual(sorted(u["name"] for u in body["unassigned"]), ["Nghi Che Do", "Nghỉ Phép Năm"])
        self.assertTrue(all(u["status"] == "not_assigned" for u in body["unassigned"]))
        run_scheduled_sync()
        rows = {row["leave_type_id"]["name"]: row for row in self.options()["results"]}
        self.assertEqual(rows["Nghỉ Phép Năm"]["remaining_days"], 12)
        self.assertEqual(rows["NGHI PHEP THANG"]["remaining_days"], 11)
        self.assertTrue(all(row["requestable"] for row in rows.values()))
        self.assertNotIn("Compensatory Leave Type", rows)

    @AUTO_ON
    def test_16_web_hr_profile_tab_shows_the_right_types(self):
        hr = make_user("leave_autosync_hr2", password="fake-pass-1", is_superuser=True)
        self.employee(self.company_a, user=hr)
        self.client.force_login(hr)
        self.never_opened(self.me, self.comp)
        AvailableLeave._base_manager.create(employee_id=self.me, leave_type_id=self.comp, available_days=1)
        url = reverse("individual-leave-tab-list", kwargs={"pk": self.me.pk})
        before = self.client.get(url, HTTP_HX_REQUEST="true").content.decode()
        self.assertIn('data-testid="missing-leave-types"', before)
        self.assertNotIn("Compensatory Leave Type", before, "switched-off Nghỉ Bù is not shown")
        run_scheduled_sync()
        after = self.client.get(url, HTTP_HX_REQUEST="true").content.decode()
        for name in ("NGHI PHEP THANG", "Nghỉ Phép Năm", "Nghi Che Do"):
            self.assertIn(name, after)
        self.assertNotIn('data-testid="missing-leave-types"', after)
        self.assertNotIn("Compensatory Leave Type", after)

    @AUTO_ON
    def test_18_a_valid_request_is_accepted(self):
        run_scheduled_sync()
        response = self.request(self.annual, day=NEXT_MONDAY)
        self.assertEqual(response.status_code, 201, response.content)
        response = self.request(self.regime, day=NEXT_MONDAY + timedelta(days=1))
        self.assertEqual(response.status_code, 201, response.content)

    @AUTO_ON
    def test_19_a_request_over_the_balance_is_refused(self):
        run_scheduled_sync()
        AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id=self.annual).update(
            available_days=1, total_leave_days=1
        )
        response = self.api.post(
            "/api/leave/user-request/",
            {"leave_type_id": self.annual.pk, "start_date": str(NEXT_MONDAY), "start_date_breakdown": "full_day",
             "end_date": str(NEXT_MONDAY + timedelta(days=2)), "end_date_breakdown": "full_day", "description": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(LeaveRequest._base_manager.filter(employee_id=self.me, leave_type_id=self.annual).exists())

    def test_20_dry_runs_write_nothing(self):
        before = self.balances()
        with CaptureQueriesContext(connection) as queries:
            out = StringIO()
            call_command("backfill_available_leave", "--dry-run", "--use-type-total", stdout=out)
            self.assertIsNone(run_scheduled_sync(), "the scheduled sync is off unless switched on")
            plan_missing_balances(use_type_total=True)
        self.assertEqual(writes(queries, "leave_"), [])
        self.assertEqual(self.balances(), before)
        self.assertIn(f"WOULD CREATE  employee={self.me.pk} leave_type={self.annual.pk}", out.getvalue())


class ScheduledSyncLimitTests(AutoSyncFixtures):
    @AUTO_ON
    def test_a_type_with_impossible_days_is_left_out(self):
        for total in (0, 400):
            LeaveType._base_manager.filter(pk=self.annual.pk).update(total_days=total)
            summary = run_scheduled_sync()
            self.assertEqual(summary["rejected_config"], 1)
            self.assertFalse(self.has(self.me, self.annual))
        self.assertEqual(self.days(self.me, self.regime), 30, "the other type is still opened")

    @AUTO_ON
    @override_settings(LEAVE_AUTO_SYNC_MAX_ROWS=1)
    def test_too_many_missing_rows_open_nothing(self):
        summary = run_scheduled_sync()
        self.assertEqual((summary["opened"], summary["refused"]), (0, "too many rows"))
        self.assertFalse(self.has(self.me, self.annual))

    def test_a_new_type_with_impossible_days_is_not_handed_out(self):
        odd = self.leave_type("Phep sai", self.company_a, total_days=0, count=0)
        self.assertFalse(self.has(self.me, odd))
        self.assertFalse(self.has(self.colleague, odd))

    def test_the_balance_history_says_what_opened_it(self):
        newcomer = self.employee(self.company_a)
        record = AvailableLeave.history.filter(employee_id=newcomer.pk, leave_type_id=self.annual.pk).first()
        self.assertTrue(record.history_change_reason.startswith("Tự động"), record.history_change_reason)


class CheckInSettingsAnnualLeaveTests(AutoSyncFixtures):
    def setUp(self):
        super().setUp()
        self.hr = make_user("leave_autosync_hr3", password="fake-pass-1", is_superuser=True)
        self.employee(self.company_a, user=self.hr)
        self.client.force_login(self.hr)
        LeaveType._base_manager.filter(pk=self.annual.pk).update(total_days=1, count=1)  # production today

    def post(self, days, decision=None):
        data = {"action": "policy", "late_threshold_minutes": 10, "annual_leave_days": days}
        if decision:
            data["annual_leave_decision"] = decision
        return self.client.post(reverse("checkin-settings"), data)

    def test_a_change_is_previewed_first_and_saves_nothing(self):
        before, policies = self.balances(), CheckInPolicy._base_manager.count()
        response = self.post(12)
        self.assertContains(response, 'data-testid="annual-leave-preview"')
        self.assertContains(response, "giữ nguyên")
        self.annual.refresh_from_db()
        self.assertEqual(self.annual.total_days, 1)
        self.assertEqual(CheckInPolicy._base_manager.count(), policies)
        self.assertEqual(self.balances(), before)

    def test_confirming_changes_the_type_only(self):
        before = self.balances()
        self.post(12, "confirm:12")
        self.annual.refresh_from_db()
        self.assertEqual((self.annual.total_days, self.annual.count), (12, 12))
        self.assertEqual(self.balances(), before, "no balance gets the difference added")

    def test_keeping_the_type_saves_only_the_policy(self):
        self.post(12, "skip")
        self.annual.refresh_from_db()
        self.assertEqual(self.annual.total_days, 1)
        self.assertEqual(CheckInPolicy._base_manager.get(company_id=self.company_a).annual_leave_days, 12)
