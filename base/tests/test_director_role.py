"""The Director role (2026-11-17).

* Recognised by the marker permission ``base.director_role``, never by a
  group name: a "Giám đốc" group someone created by hand stays an ordinary
  group (``base 0017`` names the role group differently then).
* The admin role never carries the marker, although ``post_migrate`` gives
  it every permission of the ``base`` app.
* A Director has the admin's business rights in the companies of the
  assignment only, and nothing of a superuser.
* Only a superuser grants or revokes it; the marker survives a superuser's
  edits of the role's permissions.

Fake data only.
"""

import importlib

from django.apps import apps as global_apps
from django.contrib.auth.models import Group, Permission
from django.test import Client, TestCase
from django.urls import reverse

from base.auth_backends import get_effective_permission_codenames
from base.forms import UserGroupForm
from base.models import CompanyGroupAssignment
from base.roles import (
    ADMIN_ROLE,
    LEADER_ROLE,
    can_open_approval_hub,
    director_groups,
    director_marker_permission,
    ensure_standard_roles,
    is_checkin_admin,
    is_checkin_director,
    is_director_group,
)
from joydigi.testkit import make_company, make_employee, make_user
from joydigi.testkit.roles import director_group
from joydigi_audit.middleware import _role_of
from leave.request_access import holds_right_in_company, is_admin_for

director_migration = importlib.import_module("base.migrations.0017_director_role")


def give(user, company, group):
    CompanyGroupAssignment.objects.get_or_create(user=user, company=company, group=group)
    CompanyGroupAssignment.sync_user_group_membership(user, group)


class DirectorWorld:
    def build(self):
        ensure_standard_roles()
        self.director_group = director_group()
        self.admin_group = Group.objects.get(name=ADMIN_ROLE)
        self.company = make_company("Director Co A")
        self.other_company = make_company("Director Co B")
        self.director_user = make_user("dir_vu", password="x-Dir-Vu-1")
        self.director = make_employee(company=self.company, email="vu@dir.test", user=self.director_user)
        give(self.director_user, self.company, self.director_group)
        self.staff_a = make_employee(company=self.company, email="a@dir.test")
        self.staff_b = make_employee(company=self.other_company, email="b@dir.test")
        self.root_user = make_user("dir_root", password="x-Dir-Root-1", is_superuser=True)
        self.root = make_employee(company=self.company, email="root@dir.test", user=self.root_user)

    def web(self, user):
        client = Client()
        client.force_login(user)
        return client


class DirectorGroupTests(DirectorWorld, TestCase):
    def setUp(self):
        self.build()

    def test_migration_created_one_empty_director_group(self):
        groups = list(director_groups())
        self.assertEqual(len(groups), 1)
        self.assertTrue(is_director_group(groups[0]))

    def test_the_admin_role_never_carries_the_marker(self):
        marker = director_marker_permission()
        self.assertIsNotNone(marker)
        self.assertFalse(self.admin_group.permissions.filter(pk=marker.pk).exists())
        self.assertFalse(is_director_group(self.admin_group))
        # even if someone adds it, a named role is never the Director role
        self.admin_group.permissions.add(marker)
        self.assertFalse(is_director_group(self.admin_group))

    def test_an_existing_group_named_giam_doc_is_not_elevated(self):
        # Start from a database where somebody created "Giám đốc" by hand.
        Group.objects.filter(pk=self.director_group.pk).delete()
        handmade = Group.objects.create(name="Giám đốc")
        member = make_user("dir_handmade", password="x-Dir-Hand-1")
        handmade.user_set.add(member)
        perms_before = set(handmade.permissions.values_list("pk", flat=True))

        director_migration.create_director_role(global_apps, None)

        handmade.refresh_from_db()
        self.assertEqual(handmade.name, "Giám đốc")
        self.assertEqual(set(handmade.permissions.values_list("pk", flat=True)), perms_before)
        self.assertFalse(is_director_group(handmade))
        self.assertFalse(is_checkin_director(member))
        created = director_groups().get()
        self.assertEqual(created.name, "Giám đốc (vai trò hệ thống)")
        self.assertFalse(created.user_set.exists(), "nobody gets the role automatically")

    def test_the_migration_is_idempotent(self):
        director_migration.create_director_role(global_apps, None)
        self.assertEqual(director_groups().count(), 1)

    def test_reversing_keeps_a_director_group_somebody_holds(self):
        director_migration.remove_empty_director_role(global_apps, None)
        self.assertEqual(director_groups().count(), 1)


class DirectorRightsTests(DirectorWorld, TestCase):
    def setUp(self):
        self.build()

    def test_admin_rights_in_the_assigned_company_only(self):
        self.assertTrue(is_checkin_director(self.director_user, self.company.pk))
        self.assertFalse(is_checkin_director(self.director_user, self.other_company.pk))
        self.assertTrue(is_checkin_admin(self.director_user))
        self.assertTrue(is_admin_for(self.director_user, self.staff_a))
        self.assertFalse(is_admin_for(self.director_user, self.staff_b))
        self.assertTrue(holds_right_in_company(self.director_user, self.company.pk, "leave.view_leaverequest"))
        self.assertFalse(holds_right_in_company(self.director_user, self.other_company.pk, "leave.view_leaverequest"))
        self.assertTrue(can_open_approval_hub(self.director_user))

    def test_inherits_the_admin_business_permissions_but_not_system_ones(self):
        codenames = set(get_effective_permission_codenames(self.director_user, self.company.pk))
        admin_business = set(
            self.admin_group.permissions.exclude(content_type__app_label__in=("admin", "auth", "contenttypes", "sessions"))
            .values_list("codename", flat=True)
        )
        self.assertTrue(admin_business, "post_migrate gives the admin role business permissions")
        self.assertTrue(admin_business <= codenames)
        self.assertNotIn("add_group", codenames)
        self.assertNotIn("change_user", codenames)
        self.assertEqual(set(get_effective_permission_codenames(self.director_user, self.other_company.pk))
                         & admin_business, set())

    def test_has_perm_follows_the_company(self):
        user = type(self.director_user).objects.get(pk=self.director_user.pk)
        self.assertTrue(user.has_perm("leave.change_leaverequest"))  # no request: work company A
        self.assertFalse(user.has_perm("auth.change_group"))

    def test_not_a_superuser_and_no_system_pages(self):
        self.director_user.refresh_from_db()
        self.assertFalse(self.director_user.is_superuser)
        self.assertFalse(self.director_user.is_staff)
        response = self.web(self.director_user).get(reverse("user-group-view"))
        self.assertNotEqual(response.status_code, 200)
        response = self.web(self.director_user).get("/admin/")
        self.assertNotEqual(response.status_code, 200)

    def test_the_activity_log_names_the_role(self):
        self.assertEqual(_role_of(self.director_user), "director")


class DirectorGrantTests(DirectorWorld, TestCase):
    def setUp(self):
        self.build()

    def test_a_director_cannot_grant_the_director_role(self):
        response = self.web(self.director_user).post(
            "/employee/allocation-assign-group-user/",
            {"employee": self.staff_a.pk, "group": [self.director_group.pk]},
        )
        self.assertNotEqual(response.status_code, 200)
        self.assertFalse(CompanyGroupAssignment.objects.filter(
            user=self.staff_a.employee_user_id, group=self.director_group).exists())

    def test_a_director_cannot_revoke_a_role(self):
        other_user = self.staff_a.employee_user_id
        give(other_user, self.company, self.director_group)
        self.web(self.director_user).post(reverse("group-remove-user", args=[other_user.pk, self.director_group.pk]))
        self.assertTrue(CompanyGroupAssignment.objects.filter(user=other_user, group=self.director_group).exists())

    def test_the_superuser_revokes_it(self):
        response = self.web(self.root_user).post(
            reverse("group-remove-user", args=[self.director_user.pk, self.director_group.pk])
            + f"?company_id={self.company.pk}"
        )
        self.assertLess(response.status_code, 500)
        self.assertFalse(CompanyGroupAssignment.objects.filter(
            user=self.director_user, group=self.director_group, company=self.company).exists())

    def test_the_marker_survives_a_superuser_permission_edit(self):
        form = UserGroupForm({"name": self.director_group.name, "permissions": ["view_leaverequest"]},
                             instance=self.director_group)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertTrue(is_director_group(self.director_group))

    def test_the_marker_cannot_be_ticked_onto_another_role(self):
        leader = Group.objects.get(name=LEADER_ROLE)
        form = UserGroupForm({"name": leader.name, "permissions": ["director_role"]}, instance=leader)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        marker = director_marker_permission()
        self.assertFalse(leader.permissions.filter(pk=marker.pk).exists())

    def test_the_marker_cannot_be_removed_one_by_one(self):
        marker = director_marker_permission()
        self.web(self.root_user).post(
            reverse("group-permission-remove", args=[marker.pk, self.director_group.pk])
        )
        self.assertTrue(is_director_group(self.director_group))
