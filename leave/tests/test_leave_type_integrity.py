"""Leave types an employee can request: complete, not duplicated, and never
dependent on which company an HR admin happens to have selected.

Reported 2026-11-08 (iOS app, many employees): the create-leave dropdown
offered "Compensatory Leave Type" twice and no "Nghỉ phép năm". The app lists
exactly the employee's ``AvailableLeave`` rows (GET /api/leave/available-leave/),
so the causes are on the server:

1. Auto-assignment (signals, ``leave.services``) and the check-in portal's
   annual-leave sync read Employee/LeaveType/AvailableLeave through the
   company-filtered default managers, i.e. through the company picked in the
   *admin's web session*. Whoever saved a leave type or an employee decided
   who got it; a type saved while another company was selected reached
   nobody, and an existing balance could be missed and created twice.
2. The compensatory-leave settings pages ``get_or_create`` a compensatory
   type per selected company *and* one with no company when "all" is
   selected, although the model allows only one (``LeaveType.clean``).
3. The API listed inactive types too, unordered, 20 per page, and the app
   read only the first page.

Fake data only.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from base.models import Company
from employee.models import Employee, EmployeeWorkInformation
from joydigi.joydigi_middlewares import set_selected_company
from joydigi.testkit import make_user
from leave.models import AvailableLeave, LeaveType
from leave.services import ensure_available_leave


class SelectedCompanyMixin:
    def selected(self, company):
        """Run with an HR web session's selected company (str id or 'all')."""
        test = self

        class _Ctx:
            def __enter__(self):
                set_selected_company(company)

            def __exit__(self, *exc):
                set_selected_company(None)

        test.addCleanup(set_selected_company, None)
        return _Ctx()


class Fixtures(SelectedCompanyMixin, TestCase):
    def setUp(self):
        self.company_a = Company.objects.create(company="Cong Ty A")
        self.company_b = Company.objects.create(company="Cong Ty B")

    def employee(self, first, company, user=None):
        with self.captureOnCommitCallbacks(execute=True):
            employee = Employee.objects.create(
                employee_first_name=first,
                employee_last_name="Test",
                email=f"{first.lower()}@example.test",
                phone="0900000000",
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

    def type_names(self, employee):
        return sorted(
            AvailableLeave._base_manager.filter(employee_id=employee).values_list("leave_type_id__name", flat=True)
        )


class AssignmentIgnoresTheAdminsSelectedCompanyTests(Fixtures):
    def test_leave_type_saved_while_another_company_is_selected_still_reaches_its_company(self):
        an = self.employee("An", self.company_a)
        with self.selected(str(self.company_b.id)):
            self.leave_type("Nghi phep nam", self.company_a)
        self.assertIn("Nghi phep nam", self.type_names(an))

    def test_employee_saved_while_another_company_is_selected_gets_their_companys_types(self):
        self.leave_type("Nghi phep nam", self.company_a)
        with self.selected(str(self.company_b.id)):
            binh = self.employee("Binh", self.company_a)
        self.assertIn("Nghi phep nam", self.type_names(binh))

    def test_an_existing_balance_is_found_whatever_company_is_selected(self):
        an = self.employee("An", self.company_a)
        annual = self.leave_type("Nghi phep nam", self.company_a)
        AvailableLeave._base_manager.filter(employee_id=an, leave_type_id=annual).update(available_days=3)
        with self.selected(str(self.company_b.id)):
            row, created = ensure_available_leave(an, annual)  # used to try a second row
        self.assertFalse(created)
        self.assertEqual(row.available_days, 3, "an existing balance is never touched")

    def test_another_companys_type_still_never_crosses_over(self):
        an = self.employee("An", self.company_a)
        with self.selected("all"):
            self.leave_type("Phep rieng B", self.company_b)
        self.assertNotIn("Phep rieng B", self.type_names(an))


class CompensatoryLeaveTypeIsUniqueTests(Fixtures):
    def test_settings_pages_reuse_the_existing_compensatory_type(self):
        from leave.services import compensatory_leave_type_for

        first = compensatory_leave_type_for("all")
        second = compensatory_leave_type_for(str(self.company_a.id))
        third = compensatory_leave_type_for(str(self.company_b.id))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.pk, third.pk)
        self.assertEqual(LeaveType._base_manager.filter(is_compensatory_leave=True).count(), 1)

    def test_first_creation_follows_the_selected_company(self):
        from leave.services import compensatory_leave_type_for

        created = compensatory_leave_type_for(str(self.company_a.id))
        self.assertEqual(created.company_id, self.company_a)
        self.assertTrue(created.is_compensatory_leave)

    def test_web_settings_page_no_longer_creates_a_second_type(self):
        from django.test import Client

        from joydigi.testkit import make_employee

        admin = make_user("leave_admin_cmp", password="leave-fake-pass-1", is_superuser=True)
        make_employee(company=self.company_a, email="leave_admin_cmp@example.test", user=admin)
        client = Client()
        client.force_login(admin)
        for selected in ("all", str(self.company_a.id), "all", str(self.company_b.id)):
            session = client.session
            session["selected_company"] = selected
            session.save()
            response = client.get("/leave/compensatory-leave-settings-view/")
            self.assertEqual(response.status_code, 200, f"the settings view must really run ({selected})")
        self.assertEqual(LeaveType._base_manager.filter(is_compensatory_leave=True).count(), 1)


class AvailableLeaveApiTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.user = make_user("leave_api_emp", password="leave-fake-pass-1")
        self.emp = self.employee("Chi", self.company_a, user=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def names(self, query=""):
        body = self.client.get(f"/api/leave/available-leave/{query}").json()
        return [row["leave_type_id"]["name"] for row in body["results"]], body

    def test_inactive_types_are_not_offered(self):
        self.leave_type("Nghi phep nam", self.company_a)
        old = self.leave_type("Phep cu", self.company_a)
        LeaveType._base_manager.filter(pk=old.pk).update(is_active=False)
        names, _ = self.names()
        self.assertIn("Nghi phep nam", names)
        self.assertNotIn("Phep cu", names, "an inactive type cannot be requested")

    def test_listed_in_name_order(self):
        for name in ("Nghi om", "Nghi bu", "Nghi phep nam"):
            self.leave_type(name, self.company_a)
        names, _ = self.names()
        self.assertEqual(names, sorted(names))

    def test_more_than_a_page_is_reachable_with_page_size(self):
        for i in range(25):
            self.leave_type(f"Loai {i:02d}", self.company_a)
        first, body = self.names()
        self.assertEqual(len(first), 20)
        self.assertEqual(body["count"], 25)
        self.assertIsNotNone(body["next"])
        everything, body = self.names("?page_size=100")
        self.assertEqual(len(everything), 25)
        self.assertIsNone(body["next"])
