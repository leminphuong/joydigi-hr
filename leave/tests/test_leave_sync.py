"""Leave-type sync for every eligible employee, and leave requests that fail
with an answer the employee can act on (2026-11-09).

What an admin does on the HR web must reach every eligible employee without
anyone assigning balances by hand, never twice, and never by rewriting a
balance or a past request:

* creating a type, re-activating it, moving it to another company (or to all
  companies) or relaxing its conditions opens the missing balances;
* editing its name or days does not touch existing balances;
* a new, re-activated or transferred employee gets their company's types;
* the app lists only types the employee can use now, with remaining days and
  a status, plus the eligible types HR has not opened yet (with a message);
* a request with a type that is inactive, of another company, not opened, or
  from an account without work information is refused in Vietnamese with
  "liên hệ HR", and a rejected/cancelled request no longer blocks new dates.

Fake data only.
"""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from base.models import Company
from employee.models import Employee, EmployeeWorkInformation
from joydigi.testkit import make_user
from leave.models import AvailableLeave, LeaveRequest, LeaveType, LeaveTypeCondition
from leave.services import ensure_available_leave

TOMORROW = date.today() + timedelta(days=1)


class SyncFixtures(TestCase):
    def setUp(self):
        self.company_a = Company.objects.create(company="Sync A")
        self.company_b = Company.objects.create(company="Sync B")
        self._n = 0

    def employee(self, company, *, active=True, user=None, gender="male"):
        self._n += 1
        with self.captureOnCommitCallbacks(execute=True):
            employee = Employee.objects.create(
                employee_first_name=f"NV{self._n}",
                employee_last_name="Test",
                email=f"sync{self._n}@example.test",
                phone="0900000000",
                is_active=active,
                gender=gender,
                employee_user_id=user,
            )
            work_info, _ = EmployeeWorkInformation._base_manager.get_or_create(employee_id=employee)
            work_info.company_id = company
            work_info.save()
        employee.refresh_from_db()
        return employee

    def leave_type(self, name, company=None, **extra):
        values = {"name": name, "company_id": company, "total_days": 12, "payment": "paid"}
        values.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            return LeaveType.objects.create(**values)

    def save(self, obj, **changes):
        for field, value in changes.items():
            setattr(obj, field, value)
        with self.captureOnCommitCallbacks(execute=True):
            obj.save()
        return obj

    def has(self, employee, leave_type):
        return AvailableLeave._base_manager.filter(employee_id=employee, leave_type_id=leave_type).exists()

    def balance(self, employee, leave_type):
        return AvailableLeave._base_manager.get(employee_id=employee, leave_type_id=leave_type)


class AdminChangesReachEveryEligibleEmployeeTests(SyncFixtures):
    def test_one_type_many_employees(self):
        people = [self.employee(self.company_a) for _ in range(5)]
        annual = self.leave_type("Nghi phep nam", self.company_a)
        self.assertTrue(all(self.has(p, annual) for p in people))

    def test_many_types_many_companies(self):
        a, b = self.employee(self.company_a), self.employee(self.company_b)
        shared = self.leave_type("Nghi che do")
        only_a = self.leave_type("Phep A", self.company_a)
        only_b = self.leave_type("Phep B", self.company_b)
        self.assertTrue(self.has(a, shared) and self.has(a, only_a) and not self.has(a, only_b))
        self.assertTrue(self.has(b, shared) and self.has(b, only_b) and not self.has(b, only_a))

    def test_reactivated_type_reaches_people_hired_while_it_was_off(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        self.save(annual, is_active=False)
        newcomer = self.employee(self.company_a)
        self.assertFalse(self.has(newcomer, annual), "an inactive type is not opened")
        self.save(annual, is_active=True)
        self.assertTrue(self.has(newcomer, annual), "re-activating opens the missing balances")

    def test_type_moved_to_all_companies_reaches_the_other_company(self):
        b = self.employee(self.company_b)
        annual = self.leave_type("Nghi phep nam", self.company_a)
        self.assertFalse(self.has(b, annual))
        self.save(annual, company_id=None)
        self.assertTrue(self.has(b, annual))

    def test_relaxed_condition_reaches_newly_eligible_people(self):
        woman, man = self.employee(self.company_a, gender="female"), self.employee(self.company_a, gender="male")
        condition = LeaveTypeCondition.objects.create(condition_type="gender", value="female")
        with self.captureOnCommitCallbacks(execute=True):
            maternity = LeaveType.objects.create(name="Thai san", company_id=self.company_a, total_days=180, payment="paid")
            maternity.conditions.add(condition)
        with self.captureOnCommitCallbacks(execute=True):
            ensure_available_leave(woman, maternity)
        self.assertFalse(self.has(man, maternity))
        with self.captureOnCommitCallbacks(execute=True):
            maternity.conditions.remove(condition)
        self.assertTrue(self.has(man, maternity), "removing the restriction opens it for the others")

    def test_editing_days_never_rewrites_existing_balances(self):
        a = self.employee(self.company_a)
        annual = self.leave_type("Nghi phep nam", self.company_a, total_days=12)
        AvailableLeave._base_manager.filter(employee_id=a, leave_type_id=annual).update(available_days=4.5)
        self.save(annual, total_days=15, name="Nghi phep nam 2026")
        self.assertEqual(self.balance(a, annual).available_days, 4.5, "saving a type never re-adds days")
        newcomer = self.employee(self.company_a)
        self.assertEqual(self.balance(newcomer, annual).available_days, 15, "new balances start at the new total")


class EmployeeChangesTests(SyncFixtures):
    def test_new_employee_gets_company_and_shared_types(self):
        shared, only_a = self.leave_type("Nghi che do"), self.leave_type("Phep A", self.company_a)
        a = self.employee(self.company_a)
        self.assertTrue(self.has(a, shared) and self.has(a, only_a))

    def test_transferred_employee_gets_new_company_types(self):
        only_b = self.leave_type("Phep B", self.company_b)
        a = self.employee(self.company_a)
        with self.captureOnCommitCallbacks(execute=True):
            work = EmployeeWorkInformation._base_manager.get(employee_id=a)
            work.company_id = self.company_b
            work.save()
        self.assertTrue(self.has(a, only_b))

    def test_reactivated_employee_gets_types_opened_meanwhile(self):
        sleeper = self.employee(self.company_a, active=False)
        annual = self.leave_type("Nghi phep nam", self.company_a)
        self.assertFalse(self.has(sleeper, annual))
        self.save(sleeper, is_active=True)
        self.assertTrue(self.has(sleeper, annual))


class IdempotenceAndConcurrencyTests(SyncFixtures):
    def test_repeated_sync_never_duplicates(self):
        from leave.services import sync_employee, sync_leave_type

        a = self.employee(self.company_a)
        annual = self.leave_type("Nghi phep nam", self.company_a)
        for _ in range(3):
            sync_leave_type(annual)
            sync_employee(a)
        self.assertEqual(AvailableLeave._base_manager.filter(employee_id=a, leave_type_id=annual).count(), 1)

    def test_two_admins_at_once_the_second_insert_returns_the_existing_row(self):
        """Simulates the race: both see 'no row', the first inserts, the second
        hits the unique constraint and must return the first row, not crash."""
        a = self.employee(self.company_a)
        annual = self.leave_type("Nghi phep nam", self.company_a)
        first = self.balance(a, annual)
        import leave.services as services

        real_lookup = services._existing_available_leave
        calls = []

        def racing(employee, leave_type):  # 1st look: "no row yet"; later: the truth
            calls.append(1)
            return None if len(calls) == 1 else real_lookup(employee, leave_type)

        with patch("leave.services._existing_available_leave", side_effect=racing):
            row, created = ensure_available_leave(a, annual)
        self.assertFalse(created)
        self.assertEqual(row.pk, first.pk)
        self.assertEqual(AvailableLeave._base_manager.filter(employee_id=a, leave_type_id=annual).count(), 1)


class ApiFixtures(SyncFixtures):
    def setUp(self):
        super().setUp()
        self.user = make_user("leave_sync_emp", password="leave-fake-pass-1")
        self.me = self.employee(self.company_a, user=self.user)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def options(self):
        response = self.api.get("/api/leave/available-leave/?page_size=100")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def request(self, leave_type, day=TOMORROW):
        return self.api.post(
            "/api/leave/user-request/",
            {"leave_type_id": leave_type.pk, "start_date": str(day), "start_date_breakdown": "full_day",
             "end_date": str(day), "end_date_breakdown": "full_day", "description": "test"},
            format="json",
        )


class AvailableLeaveStatusTests(ApiFixtures):
    def test_rows_keep_old_keys_and_add_remaining_days_and_status(self):
        annual = self.leave_type("Nghi phep nam", self.company_a, total_days=12)
        row = next(r for r in self.options()["results"] if r["leave_type_id"]["id"] == annual.pk)
        for key in ("id", "leave_type_id", "icon", "available_days", "carryforward_days", "total_leave_days"):
            self.assertIn(key, row, "app 1.0.1/1.0.2 read these keys")
        self.assertEqual(row["remaining_days"], 12)
        self.assertEqual(row["granted_days"], 12)
        self.assertTrue(row["requestable"])
        self.assertEqual(row["status"], "available")

    def test_used_up_balance_is_listed_but_not_requestable(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id=annual).update(
            available_days=0, carryforward_days=0, total_leave_days=0)
        row = next(r for r in self.options()["results"] if r["leave_type_id"]["id"] == annual.pk)
        self.assertEqual(row["status"], "no_balance")
        self.assertFalse(row["requestable"])
        self.assertIn("Hết số ngày", row["status_message"])

    def test_inactive_and_other_company_types_are_not_offered(self):
        old = self.leave_type("Phep cu", self.company_a)
        self.save(old, is_active=False)
        mine_before = self.leave_type("Phep cong ty A", self.company_a)
        with self.captureOnCommitCallbacks(execute=True):  # transfer to company B
            work = EmployeeWorkInformation._base_manager.get(employee_id=self.me)
            work.company_id = self.company_b
            work.save()
        ids = {r["leave_type_id"]["id"] for r in self.options()["results"]}
        self.assertNotIn(old.pk, ids, "inactive")
        self.assertNotIn(mine_before.pk, ids, "belongs to the company the employee left")
        self.assertTrue(self.has(self.me, mine_before), "the old balance itself is kept (history)")

    def test_eligible_type_hr_has_not_opened_is_listed_separately_with_a_message(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id=annual).delete()  # legacy gap
        body = self.options()
        self.assertNotIn(annual.pk, {r["leave_type_id"]["id"] for r in body["results"]})
        missing = {item["id"]: item for item in body["unassigned"]}
        self.assertIn(annual.pk, missing)
        self.assertFalse(missing[annual.pk]["requestable"])
        self.assertIn("liên hệ HR", missing[annual.pk]["status_message"])

    def test_account_without_work_information_gets_a_clear_message_not_an_error(self):
        EmployeeWorkInformation._base_manager.filter(employee_id=self.me).delete()
        response = self.api.get("/api/leave/available-leave/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("liên hệ HR", response.json()["message"])


class LeaveRequestValidationTests(ApiFixtures):
    def error_text(self, response):
        self.assertEqual(response.status_code, 400, response.content)
        return str(response.json())

    def test_not_opened_type_is_refused_in_vietnamese(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        AvailableLeave._base_manager.filter(employee_id=self.me, leave_type_id=annual).delete()
        text = self.error_text(self.request(annual))
        self.assertIn("chưa được cấp", text)
        self.assertIn("liên hệ HR", text)

    def test_inactive_type_is_refused(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        self.save(annual, is_active=False)
        self.assertIn("ngừng áp dụng", self.error_text(self.request(annual)))

    def test_other_company_type_is_refused_even_with_an_old_balance(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        with self.captureOnCommitCallbacks(execute=True):
            work = EmployeeWorkInformation._base_manager.get(employee_id=self.me)
            work.company_id = self.company_b
            work.save()
        self.assertIn("không áp dụng cho công ty", self.error_text(self.request(annual)))

    def test_missing_work_information_is_refused_with_hr_hint(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        EmployeeWorkInformation._base_manager.filter(employee_id=self.me).delete()
        self.assertIn("liên hệ HR", self.error_text(self.request(annual)))

    def test_rejected_or_cancelled_request_does_not_block_the_same_dates(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        first = self.request(annual)
        self.assertEqual(first.status_code, 201, first.content)
        LeaveRequest._base_manager.filter(pk=first.json()["id"]).update(status="rejected")
        again = self.request(annual)
        self.assertEqual(again.status_code, 201, again.content)

    def test_a_pending_request_still_blocks_the_same_dates(self):
        annual = self.leave_type("Nghi phep nam", self.company_a)
        self.assertEqual(self.request(annual).status_code, 201)
        self.assertEqual(self.request(annual).status_code, 400, "double tap / same dates")


# -- found in the diff review (2026-11-09) ------------------------------------


class EmployeeWithoutCompanyTests(ApiFixtures):
    """Work information present but no company on it: shared types keep
    working as before; only a company's own type is refused."""

    def setUp(self):
        super().setUp()
        self.shared = self.leave_type("Phep chung")
        self.own = self.leave_type("Phep cong ty A", self.company_a)
        with self.captureOnCommitCallbacks(execute=True):
            work = EmployeeWorkInformation._base_manager.get(employee_id=self.me)
            work.company_id = None
            work.save()

    def test_shared_type_is_listed_and_can_be_requested(self):
        body = self.options()
        self.assertIn(self.shared.pk, {r["leave_type_id"]["id"] for r in body["results"]})
        self.assertNotIn(self.own.pk, {r["leave_type_id"]["id"] for r in body["results"]})
        self.assertIn("chưa có công ty", body["message"])
        self.assertEqual(self.request(self.shared).status_code, 201)

    def test_company_type_is_refused_with_the_profile_hint(self):
        text = str(self.request(self.own).json())
        self.assertIn("chưa có công ty", text)
        self.assertIn("liên hệ HR", text)


class EmployeeSaveDoesNotReopenRemovedBalancesTests(SyncFixtures):
    def test_ordinary_profile_edit_leaves_a_removed_balance_removed(self):
        a = self.employee(self.company_a)
        annual = self.leave_type("Nghi phep nam", self.company_a)
        AvailableLeave._base_manager.filter(employee_id=a, leave_type_id=annual).delete()  # HR removed it
        self.save(a, phone="0911111111")
        self.assertFalse(self.has(a, annual), "a phone change must not re-open it")

    def test_a_change_that_decides_eligibility_opens_the_type(self):
        woman_only = LeaveTypeCondition.objects.create(condition_type="gender", value="female")
        a = self.employee(self.company_a, gender="male")
        with self.captureOnCommitCallbacks(execute=True):
            maternity = LeaveType.objects.create(name="Thai san", company_id=self.company_a, total_days=180,
                                                 payment="paid")
            maternity.conditions.add(woman_only)
        self.assertFalse(self.has(a, maternity))
        self.save(a, gender="female")
        self.assertTrue(self.has(a, maternity))


class ConditionEditsTests(SyncFixtures):
    def _restricted_type(self, condition):
        with self.captureOnCommitCallbacks(execute=True):
            leave_type = LeaveType.objects.create(name="Thai san", company_id=self.company_a, total_days=180,
                                                  payment="paid")
            leave_type.conditions.add(condition)
        return leave_type

    def test_editing_a_condition_value_reaches_newly_eligible_people(self):
        man = self.employee(self.company_a, gender="male")
        condition = LeaveTypeCondition.objects.create(condition_type="gender", value="female")
        maternity = self._restricted_type(condition)
        self.assertFalse(self.has(man, maternity))
        self.save(condition, value="male")
        self.assertTrue(self.has(man, maternity))

    def test_clearing_from_the_condition_side_reaches_newly_eligible_people(self):
        man = self.employee(self.company_a, gender="male")
        condition = LeaveTypeCondition.objects.create(condition_type="gender", value="female")
        maternity = self._restricted_type(condition)
        with self.captureOnCommitCallbacks(execute=True):
            condition.leavetype_set.clear()
        self.assertTrue(self.has(man, maternity))


class CompensatoryPrefersActiveTypeTests(SyncFixtures):
    def test_an_active_type_wins_over_an_older_switched_off_one(self):
        from leave.services import compensatory_leave_type_for

        old = self.leave_type("Compensatory Leave Type", is_compensatory_leave=True)
        newer = self.leave_type("Compensatory Leave Type", self.company_a, is_compensatory_leave=True)
        self.save(old, is_active=False)  # the admin keeps the newer one
        self.assertEqual(compensatory_leave_type_for("all").pk, newer.pk)
        self.assertEqual(LeaveType._base_manager.filter(is_compensatory_leave=True).count(), 2, "nothing created")
