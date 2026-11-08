"""Which HR departments OKR shows a viewer (Web filters, app "Công ty" tab).

Reported 2026-11-08: the iOS app listed only "Tất cả phòng ban" and "ADS".
The app renders exactly what GET /api/okr/departments/ returns, so the list is
decided here. A department belongs to a company's OKR when it is

* linked to that company in HR, or
* linked to no company (HR treats those as shared), or
* the department of at least one ACTIVE employee of that company.

The third rule is the fix: HR's web lists departments by the company picked in
the session (a superuser sees every company), so a department can be linked to
one company record while the people working in it belong to another. Before,
OKR hid such departments from the filters and refused them for department
objectives and OKR managers, while the check-in tree still grouped those
people under them. A department only linked to another company, with none of
the viewer's people in it, stays hidden: nothing leaks across companies.
Fake data only.
"""

from decimal import Decimal

from base.models import Department
from employee.models import Employee, EmployeeWorkInformation
from okr.models import OKRTeam, OKRTeamMember
from okr.models.base import department_allows_company
from okr.models.choices import ApprovalStatus, Scope
from okr.services import organization, reports
from okr.services.cycles import resolve_cycle
from okr.tests.factories import okr_company, okr_cycle, okr_department, okr_employee, okr_key_result, okr_objective, okr_role
from okr.tests.test_api import APITestBase
from okr.tests.test_services import as_viewer


def shared_department(name):
    return Department.objects.create(department=name)  # linked to no company


def department_of_other_company(company, name):
    return okr_department(company, name)


def move_to(employee, department):
    EmployeeWorkInformation._base_manager.filter(employee_id=employee.pk).update(department_id=department)


class DepartmentScopeTests(APITestBase):
    def ids(self, employee, query="?page_size=100"):
        page = self.call(employee, "get", f"departments/{query}")
        return [row["id"] for row in page["results"]], page

    def expected_ids(self, company):
        """The rule, written straight against the HR tables (the 'database' side)."""
        in_use = set(
            EmployeeWorkInformation._base_manager.filter(
                company_id=company, employee_id__is_active=True, department_id__isnull=False
            ).values_list("department_id", flat=True)
        )
        rows = []
        for department in Department._base_manager.filter(is_active=True).order_by("department", "id"):
            linked = set(department.company_id.values_list("pk", flat=True))
            if not linked or company.pk in linked or department.pk in in_use:
                rows.append(department.pk)
        return rows

    def test_one_department(self):
        listed, page = self.ids(self.colleague)
        self.assertEqual(listed, self.expected_ids(self.company))
        self.assertIn(self.department.pk, listed)
        self.assertEqual(page["count"], len(listed))

    def test_many_departments_without_objectives_are_listed(self):
        extra = [okr_department(self.company, f"Phong {i:02d}") for i in range(5)]
        listed, _ = self.ids(self.colleague)
        for department in extra:
            self.assertIn(department.pk, listed, "a department with no objective is still a filter option")
        self.assertEqual(listed, self.expected_ids(self.company))

    def test_more_than_twenty_departments_page_and_page_size(self):
        for i in range(24):
            okr_department(self.company, f"Ban {i:02d}")
        expected = self.expected_ids(self.company)
        self.assertGreater(len(expected), 20)
        first = self.call(self.colleague, "get", "departments/")
        self.assertEqual(first["count"], len(expected), "count is the full total, not the page")
        self.assertEqual(len(first["results"]), 20)
        self.assertIsNotNone(first["next"], "a client must be told there is more")
        all_rows, _ = self.ids(self.colleague)  # what the app (page_size=100) and the web (per_page 100) ask
        self.assertEqual(all_rows, expected)
        walked, page_number = [], 1
        while True:  # following `next`, as the fixed clients do
            page = self.call(self.colleague, "get", f"departments/?page={page_number}")
            walked += [row["id"] for row in page["results"]]
            if not page["next"]:
                break
            page_number += 1
        self.assertEqual(walked, expected)

    def test_several_companies_never_mix(self):
        mine = okr_department(self.company, "Kinh Doanh A")
        theirs = department_of_other_company(self.other_company, "Kinh Doanh B")
        shared = shared_department("Dung Chung")
        listed, _ = self.ids(self.colleague)
        outsider, _ = self.ids(self.outsider)
        self.assertIn(mine.pk, listed)
        self.assertNotIn(theirs.pk, listed, "another company's department is not shown")
        self.assertIn(shared.pk, listed)
        self.assertIn(theirs.pk, outsider)
        self.assertNotIn(mine.pk, outsider)
        self.assertIn(shared.pk, outsider)
        self.assertEqual(listed, self.expected_ids(self.company))
        self.assertEqual(outsider, self.expected_ids(self.other_company))

    def test_department_linked_elsewhere_but_staffed_by_our_people_is_listed(self):
        """The reproduced bug: linked to company B in HR, worked in by company A."""
        misfiled = department_of_other_company(self.other_company, "Marketing")
        move_to(self.colleague, misfiled)
        listed, _ = self.ids(self.colleague)
        self.assertIn(misfiled.pk, listed)
        for viewer in (self.admin, self.manager, self.owner):  # admin, manager and employee see the same list
            self.assertEqual(self.ids(viewer)[0], listed)
        outsider, _ = self.ids(self.outsider)
        self.assertIn(misfiled.pk, outsider, "it is still company B's department too")

    def test_an_inactive_employee_or_department_does_not_pull_a_department_in(self):
        misfiled = department_of_other_company(self.other_company, "Kho Van")
        leaver = okr_employee(self.company, "dept_leaver", misfiled)
        Employee._base_manager.filter(pk=leaver.pk).update(is_active=False)
        self.assertNotIn(misfiled.pk, self.ids(self.colleague)[0])
        archived = okr_department(self.company, "Da Luu Tru")
        Department._base_manager.filter(pk=archived.pk).update(is_active=False)
        self.assertNotIn(archived.pk, self.ids(self.colleague)[0])

    def test_every_department_used_elsewhere_in_okr_is_in_the_list(self):
        """Filters, department objectives, OKR managers and the check-in tree
        agree on one rule."""
        misfiled = department_of_other_company(self.other_company, "Thiet Ke")
        move_to(self.colleague, misfiled)
        listed = set(self.ids(self.colleague)[0])
        self.assertTrue(department_allows_company(misfiled, self.company.pk))
        # an admin can name its OKR manager ...
        self.call(self.admin, "put", f"departments/{misfiled.pk}/okr-manager/",
                  {"manager_id": self.manager.pk}, format="json")
        # ... and a department objective can be filed under it
        created = self.call(self.admin, "post", "objectives/",
                            {"title": "Muc tieu Thiet Ke", "cycle_id": self.cycle.pk, "scope": "department",
                             "department_id": misfiled.pk, "parent_id": self.company_objective.pk},
                            expect=201, format="json")
        self.assertEqual(created["department_name"], "Thiet Ke")
        # the check-in tree groups people only under listed departments
        viewer = as_viewer(self.admin)
        tree = reports.checkin_management(viewer, resolve_cycle(viewer))
        tree_ids = {node["department"].pk for node in tree["departments"]}
        self.assertTrue(tree_ids <= listed, (tree_ids, listed))
        self.assertIn(misfiled.pk, tree_ids)

    def test_team_in_another_department_than_its_member(self):
        sales = okr_department(self.company, "Ban Hang")
        team = OKRTeam.objects.create(company=self.company, name="Team Ban Hang", department=sales, manager=self.manager)
        OKRTeamMember.objects.create(company=self.company, team=team, employee=self.colleague)
        listed = set(self.ids(self.colleague)[0])
        self.assertIn(sales.pk, listed, "the team's department")
        self.assertIn(self.department.pk, listed, "the member's own HR department")

    def test_department_of_another_company_cannot_be_used_for_okr(self):
        theirs = department_of_other_company(self.other_company, "Rieng B")
        self.assertFalse(department_allows_company(theirs, self.company.pk))
        self.call(self.admin, "put", f"departments/{theirs.pk}/okr-manager/",
                  {"manager_id": self.manager.pk}, format="json", expect=404)
        body = self.call(self.admin, "post", "objectives/",
                         {"title": "Khong duoc", "cycle_id": self.cycle.pk, "scope": "department",
                          "department_id": theirs.pk, "parent_id": self.company_objective.pk},
                         expect=400, format="json")
        self.assertIn("department_id", body["fields"])

    def test_department_list_query_count_does_not_grow_with_rows(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(30):
            okr_department(self.company, f"Q {i:02d}")
        viewer = as_viewer(self.colleague)
        organization.departments(viewer)  # warm the viewer's cached lookups
        with CaptureQueriesContext(connection) as small:
            organization.departments(viewer)
        for i in range(30):
            okr_department(self.company, f"R {i:02d}")
        with CaptureQueriesContext(connection) as large:
            organization.departments(viewer)
        self.assertEqual(
            len(small), len(large),
            ("no query per department (N+1)", [q["sql"][:90] for q in small], [q["sql"][:90] for q in large]),
        )


class NoCycleMessageTests(APITestBase):
    """'No cycle' tells apart: none created, only drafts, only closed. The
    code stays no_cycle (the app matches on it)."""

    def message(self, employee):
        body = self.call(employee, "get", "dashboard/", expect=404)
        self.assertEqual(body["code"], "no_cycle")
        return body["message"]

    def test_no_cycle_created(self):
        stranger = okr_employee(okr_company("OKR Khong Chu Ky"), "nc_none")
        self.assertIn("chưa tạo chu kỳ nào", self.message(stranger))

    def test_only_draft_cycles(self):
        company = okr_company("OKR Chu Ky Nhap")
        okr_cycle(company, "Q1 2027", status="draft")
        self.assertIn("1 chu kỳ đang ở trạng thái Nháp", self.message(okr_employee(company, "nc_draft")))

    def test_only_closed_cycles(self):
        company = okr_company("OKR Chu Ky Dong")
        okr_cycle(company, "Q3 2026", status="closed")
        self.assertIn("đều đã đóng", self.message(okr_employee(company, "nc_closed")))
