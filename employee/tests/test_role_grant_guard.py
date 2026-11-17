"""Who may grant or revoke a role (2026-11-17, P0).

``/employee/allocation-assign-group-user/`` let in anybody who is the
reporting manager of one employee (``manager_can_enter``), and its form
(``AddToUserGroupForm``) replaces every role of the chosen employee in every
company: a team leader could make himself admin, or Director, everywhere.

Now only a superuser grants or revokes a role, checked in the view AND in
both role forms (``base.roles.role_grant_problem``). Every negative test is
paired with the superuser who still can. Fake data only.
"""

from django.contrib.auth.models import Group, Permission
from django.http import QueryDict
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from base.forms import AddToUserGroupForm, AssignUserGroup
from base.models import CompanyGroupAssignment
from base.roles import (
    ADMIN_ROLE,
    EMPLOYEE_ROLE,
    LEADER_ROLE,
    director_groups,
    ensure_standard_roles,
    role_grant_problem,
)
from employee.models import EmployeeWorkInformation
from joydigi.joydigi_middlewares import _thread_locals
from joydigi.testkit import make_company, make_employee, make_user
from joydigi.testkit.roles import director_group

ASSIGN_URL = "/employee/allocation-assign-group-user/"


def give_role(user, company, group):
    CompanyGroupAssignment.objects.get_or_create(user=user, company=company, group=group)
    CompanyGroupAssignment.sync_user_group_membership(user, group)


class RoleWorld:
    @classmethod
    def setUpTestData(cls):
        ensure_standard_roles()
        cls.admin_group = Group.objects.get(name=ADMIN_ROLE)
        cls.leader_group = Group.objects.get(name=LEADER_ROLE)
        cls.employee_group = Group.objects.get(name=EMPLOYEE_ROLE)
        cls.director_group = director_group()
        cls.company = make_company("Grant Co A")
        cls.other_company = make_company("Grant Co B")
        cls.root_user = make_user("grant_root", password="x-Grant-Root-1", is_superuser=True)
        cls.root = make_employee(company=cls.company, email="root@grant.test", user=cls.root_user)
        cls.leader_user = make_user("grant_leader", password="x-Grant-Leader-1")
        cls.leader = make_employee(company=cls.company, email="leader@grant.test", user=cls.leader_user)
        cls.member_user = make_user("grant_member", password="x-Grant-Member-1")
        cls.member = make_employee(company=cls.company, email="member@grant.test", user=cls.member_user)
        EmployeeWorkInformation.objects.filter(employee_id=cls.member).update(reporting_manager_id=cls.leader)
        give_role(cls.leader_user, cls.company, cls.leader_group)
        # an HR admin of company A, not a superuser, holding employee.change_employee
        cls.hr_user = make_user("grant_hr", password="x-Grant-Hr-1")
        cls.hr = make_employee(company=cls.company, email="hr@grant.test", user=cls.hr_user)
        give_role(cls.hr_user, cls.company, cls.admin_group)
        cls.hr_user.user_permissions.add(
            Permission.objects.get(content_type__app_label="employee", codename="change_employee")
        )

    def web(self, user):
        client = Client()
        client.force_login(user)
        return client

    def roles_of(self, user):
        return set(
            CompanyGroupAssignment.objects.filter(user=user).values_list("company_id", "group__name")
        )

    def act_as(self, user):
        request = RequestFactory().get("/")
        request.user = user
        request.session = {}
        _thread_locals.request = request
        self.addCleanup(lambda: setattr(_thread_locals, "request", None))


class AllocationAssignRouteTests(RoleWorld, TestCase):
    def test_the_director_group_exists_for_these_tests(self):
        self.assertIsNotNone(self.director_group)

    def test_a_team_leader_cannot_make_himself_admin(self):
        before = self.roles_of(self.leader_user)
        response = self.web(self.leader_user).post(
            ASSIGN_URL, {"employee": self.leader.pk, "group": [self.admin_group.pk]}
        )
        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(self.roles_of(self.leader_user), before)
        self.assertFalse(self.leader_user.groups.filter(name=ADMIN_ROLE).exists())

    def test_a_team_leader_cannot_make_himself_director(self):
        before = self.roles_of(self.leader_user)
        self.web(self.leader_user).post(
            ASSIGN_URL, {"employee": self.leader.pk, "group": [self.director_group.pk]}
        )
        self.assertEqual(self.roles_of(self.leader_user), before)
        self.assertFalse(self.leader_user.groups.filter(pk=self.director_group.pk).exists())

    def test_a_team_leader_cannot_change_the_roles_of_his_team(self):
        before = self.roles_of(self.member_user)
        self.web(self.leader_user).post(
            ASSIGN_URL, {"employee": self.member.pk, "group": [self.admin_group.pk]}
        )
        self.assertEqual(self.roles_of(self.member_user), before)

    def test_a_team_leader_does_not_even_get_the_form(self):
        response = self.web(self.leader_user).get(ASSIGN_URL, {"employee": self.member.pk})
        self.assertNotContains(response, 'name="group"', status_code=response.status_code)

    def test_an_hr_admin_with_change_employee_cannot_grant_roles_here(self):
        before = self.roles_of(self.member_user)
        self.web(self.hr_user).post(ASSIGN_URL, {"employee": self.member.pk, "group": [self.admin_group.pk]})
        self.assertEqual(self.roles_of(self.member_user), before)

    def test_the_superuser_still_assigns_roles_here(self):
        response = self.web(self.root_user).post(
            ASSIGN_URL, {"employee": self.member.pk, "group": [self.leader_group.pk]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn((self.company.pk, LEADER_ROLE), self.roles_of(self.member_user))


class RoleFormGuardTests(RoleWorld, TestCase):
    def assign_form(self, *, employees, group, companies=()):
        data = QueryDict(mutable=True)
        data.setlist("employee", [str(e.pk) for e in employees])
        data["group"] = str(group.pk)
        data.setlist("companies", [str(c.pk) for c in companies])
        return AssignUserGroup(data)

    def test_add_to_user_group_form_refuses_any_non_superuser(self):
        self.act_as(self.hr_user)
        form = AddToUserGroupForm({"employee": self.member.pk, "group": [self.leader_group.pk]})
        self.assertFalse(form.is_valid())

    def test_add_to_user_group_form_accepts_the_superuser(self):
        self.act_as(self.root_user)
        form = AddToUserGroupForm({"employee": self.member.pk, "group": [self.leader_group.pk]})
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_non_superuser_cannot_grant_the_admin_role(self):
        self.act_as(self.hr_user)
        form = self.assign_form(employees=[self.member], group=self.admin_group, companies=[self.company])
        self.assertFalse(form.is_valid())

    def test_a_non_superuser_cannot_grant_the_director_role(self):
        self.act_as(self.hr_user)
        form = self.assign_form(employees=[self.member], group=self.director_group, companies=[self.company])
        self.assertFalse(form.is_valid())

    def test_nobody_but_a_superuser_changes_his_own_role(self):
        self.act_as(self.hr_user)
        form = self.assign_form(employees=[self.hr], group=self.leader_group, companies=[self.company])
        self.assertFalse(form.is_valid())

    def test_a_non_superuser_cannot_grant_in_a_company_he_does_not_manage(self):
        self.act_as(self.hr_user)
        form = self.assign_form(employees=[self.member], group=self.leader_group, companies=[self.other_company])
        self.assertFalse(form.is_valid())

    def test_the_superuser_grants_the_director_role_in_one_company(self):
        self.act_as(self.root_user)
        form = self.assign_form(employees=[self.member], group=self.director_group, companies=[self.company])
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            set(CompanyGroupAssignment.objects.filter(user=self.member_user, group=self.director_group)
                .values_list("company_id", flat=True)),
            {self.company.pk},
        )


class RoleRevokeRouteTests(RoleWorld, TestCase):
    def test_delete_group_permission_alone_no_longer_clears_roles(self):
        self.hr_user.user_permissions.add(Permission.objects.get(content_type__app_label="auth", codename="delete_group"))
        before = set(self.leader_user.groups.values_list("name", flat=True))
        self.web(self.hr_user).post(
            reverse("employee-user-group-assign-delete", args=[self.leader_user.pk])
        )
        self.assertEqual(set(self.leader_user.groups.values_list("name", flat=True)), before)

    def test_the_superuser_revoke_also_removes_the_company_roles(self):
        self.web(self.root_user).post(
            reverse("employee-user-group-assign-delete", args=[self.leader_user.pk])
        )
        self.assertEqual(self.roles_of(self.leader_user), set())
        self.assertFalse(self.leader_user.groups.exists())

    def test_a_non_superuser_cannot_remove_someone_from_a_role(self):
        before = self.roles_of(self.leader_user)
        self.web(self.hr_user).post(
            reverse("group-remove-user", args=[self.leader_user.pk, self.leader_group.pk])
        )
        self.assertEqual(self.roles_of(self.leader_user), before)


class RoleGrantProblemTests(RoleWorld, TestCase):
    def test_rules(self):
        self.assertIsNone(role_grant_problem(None, [self.admin_group]))
        self.assertIsNone(role_grant_problem(self.root_user, [self.director_group], [self.root_user]))
        self.assertIsNotNone(role_grant_problem(self.hr_user, [self.admin_group], [self.member_user]))
        self.assertIsNotNone(role_grant_problem(self.hr_user, [self.director_group], [self.member_user]))
        self.assertIsNotNone(role_grant_problem(self.hr_user, [self.leader_group], [self.hr_user]))
        self.assertIsNotNone(role_grant_problem(self.hr_user, [self.leader_group], [self.member_user], all_companies=True))
        # superuser only: even a harmless role, even for an admin
        self.assertIsNotNone(role_grant_problem(self.hr_user, [self.leader_group], [self.member_user]))
