"""W3: web administration of cycles, teams, OKR roles and settings.

Permission matrix, cross-company isolation, role history and the closed-cycle
lock. Fake data only.
"""

import json
import re

from django.contrib.auth.models import Group
from django.test import override_settings

from employee.models import Employee
from okr.models import AppendOnlyError, OKRActivity, OKRCycle, OKRRole, OKRRoleEvent
from okr.models.choices import ActivityAction
from okr.tests.factories import okr_cycle, okr_department, okr_employee
from okr.tests.test_api import APITestBase

QUARTER = {"name": "Q1 2027", "cycle_type": "quarter", "start_date": "2027-01-01", "end_date": "2027-03-31"}


class CycleRulesTests(APITestBase):
    def create(self, **data):
        return self.call(self.admin, "post", "cycles/", {**QUARTER, **data}, expect=201, format="json")

    def test_invalid_cycles_are_refused(self):
        cases = [
            ({**QUARTER, "name": " "}, "name"),
            ({**QUARTER, "end_date": "2026-12-31"}, "end_date"),
            ({**QUARTER, "start_date": "not-a-date"}, "start_date"),
            # Same type and overlapping the seeded Q4 2026 cycle.
            ({**QUARTER, "start_date": "2026-12-01"}, "start_date"),
        ]
        for data, field in cases:
            body = self.call(self.admin, "post", "cycles/", data, expect=400, format="json")
            self.assertIn(field, body["fields"], data)
        foreign = okr_cycle(self.other_company, "Ngoai cong ty")
        body = self.call(self.admin, "post", "cycles/", {**QUARTER, "parent_id": foreign.pk}, expect=400, format="json")
        self.assertIn("parent_id", body["fields"])
        # A different type may overlap (a month inside a quarter).
        self.call(self.admin, "post", "cycles/", {**QUARTER, "name": "Thang 12", "cycle_type": "month",
                                                  "start_date": "2026-12-01", "end_date": "2026-12-31"},
                  expect=201, format="json")

    def test_closed_cycle_is_locked_until_reopened(self):
        created = self.create()
        path = f"cycles/{created['id']}/"
        self.call(self.admin, "post", f"{path}status/", {"status": "active"}, format="json")
        self.call(self.admin, "post", f"{path}status/", {"status": "closed"}, format="json")
        refused = self.call(self.admin, "put", path, {**QUARTER, "name": "Sua sau khi dong"}, expect=400, format="json")
        self.assertEqual(refused["code"], "cycle_closed")
        self.assertEqual(OKRCycle.objects.get(pk=created["id"]).name, "Q1 2027")
        # Reopening is the admin's explicit step, and it is logged.
        self.call(self.admin, "post", f"{path}status/", {"status": "active"}, format="json")
        self.call(self.admin, "put", path, {**QUARTER, "name": "Q1/2027"}, format="json")
        moves = list(
            OKRActivity.objects.filter(cycle_id=created["id"], action=ActivityAction.CYCLE_STATUS_CHANGED)
            .order_by("created_at", "id")
            .values_list("metadata", flat=True)
        )
        self.assertEqual([(m["from"], m["to"]) for m in moves],
                         [("draft", "active"), ("active", "closed"), ("closed", "active")])
        # A cycle never goes back to draft.
        self.call(self.admin, "post", f"{path}status/", {"status": "draft"}, expect=400, format="json")

    def test_changes_stay_on_the_target_cycle(self):
        other = okr_cycle(self.company, "Q2 2027", start_date="2027-04-01", end_date="2027-06-30", status="draft")
        self.draft()
        created = self.create()
        before = {c.pk: (c.status, c.name) for c in OKRCycle.objects.filter(company=self.company)}
        self.call(self.admin, "post", f"cycles/{created['id']}/status/", {"status": "active"}, format="json")
        self.call(self.admin, "post", f"cycles/{self.cycle.pk}/clone/", {"destination_cycle_id": other.pk}, format="json")
        after = {c.pk: (c.status, c.name) for c in OKRCycle.objects.filter(company=self.company)}
        changed = {pk for pk in before if before[pk] != after[pk]}
        self.assertEqual(changed, {created["id"]})
        source_count = self.cycle.objectives.filter(deleted_at__isnull=True).count()
        self.assertEqual(source_count, 2)  # the clone copied, it did not move anything

    def test_status_filter_and_counts(self):
        created = self.create()
        drafts = self.call(self.manager, "get", "cycles/?status=draft")["results"]
        self.assertEqual([c["id"] for c in drafts], [created["id"]])
        self.assertEqual(drafts[0]["objective_count"], 0)
        active = self.call(self.owner, "get", "cycles/?status=active")["results"]
        self.assertEqual([c["id"] for c in active], [self.cycle.pk])
        self.call(self.owner, "get", "cycles/?status=archived", expect=400)


class PermissionMatrixTests(APITestBase):
    """Backend decides, whatever the browser shows (prompt section 9)."""

    def test_admin_writes_matrix(self):
        team = self.call(self.admin, "post", "teams/", {"name": "Team ma tran"}, expect=201, format="json")
        writes = [
            ("post", "cycles/", QUARTER),
            ("put", f"cycles/{self.cycle.pk}/", {**QUARTER, "name": "x"}),
            ("post", f"cycles/{self.cycle.pk}/status/", {"status": "closed"}),
            ("post", "teams/", {"name": "Team X"}),
            ("put", f"teams/{team['id']}/", {"name": "Team Y"}),
            ("post", f"teams/{team['id']}/status/", {"status": "inactive"}),
            ("put", f"teams/{team['id']}/members/", {"employee_ids": [self.owner.pk]}),
            ("put", f"departments/{self.department.pk}/okr-manager/", {"manager_id": self.manager.pk}),
            ("post", "roles/", {"employee_id": self.colleague.pk, "role": "manager"}),
            ("put", "settings/", {"warning_threshold": "30"}),
        ]
        for employee in (self.owner, self.manager, self.colleague):
            for method, path, data in writes:
                with self.subTest(who=employee.pk, call=f"{method} {path}"):
                    self.call(employee, method, path, data, expect=403, format="json")
        for path in ("roles/", "roles/history/"):
            for employee in (self.owner, self.manager):
                self.call(employee, "get", path, expect=403)
            self.call(self.admin, "get", path)
        # Reading cycles, teams, departments and settings is open to every employee.
        for path in ("cycles/", "teams/", "departments/", "settings/"):
            self.call(self.owner, "get", path)
        # Approvals: managers and admins only.
        self.call(self.owner, "get", "approvals/", expect=403)
        self.call(self.manager, "get", "approvals/")

    def test_other_company_admin_sees_nothing_of_ours(self):
        team = self.call(self.admin, "post", "teams/", {"name": "Team cong ty A"}, expect=201, format="json")
        self.call(self.admin, "post", "roles/", {"employee_id": self.colleague.pk, "role": "manager"},
                  expect=201, format="json")
        self.call(self.outsider, "put", f"cycles/{self.cycle.pk}/", QUARTER, expect=404, format="json")
        self.call(self.outsider, "post", f"cycles/{self.cycle.pk}/status/", {"status": "closed"}, expect=404,
                  format="json")
        self.call(self.outsider, "put", f"teams/{team['id']}/", {"name": "Hijack"}, expect=404, format="json")
        self.call(self.outsider, "put", f"teams/{team['id']}/members/", {"employee_ids": []}, expect=404,
                  format="json")
        self.call(self.outsider, "put", f"departments/{self.department.pk}/okr-manager/",
                  {"manager_id": self.outsider.pk}, expect=404, format="json")
        # Our people cannot be given roles by their admin, nor theirs by ours.
        self.call(self.outsider, "post", "roles/", {"employee_id": self.colleague.pk, "role": "admin"},
                  expect=400, format="json")
        self.call(self.admin, "post", "roles/", {"employee_id": self.outsider.pk, "role": "manager"},
                  expect=400, format="json")
        theirs = self.call(self.outsider, "get", "roles/history/")
        self.assertEqual(theirs["count"], 0)
        self.assertEqual(
            {r["employee"]["id"] for r in self.call(self.outsider, "get", "roles/")["results"]}, {self.outsider.pk}
        )
        self.assertNotIn(team["id"], {t["id"] for t in self.call(self.outsider, "get", "teams/")["results"]})


class RoleAdminTests(APITestBase):
    def test_hr_admin_and_superuser_get_no_okr_rights(self):
        from base.roles import ADMIN_ROLE

        user = self.colleague.employee_user_id
        user.groups.add(Group.objects.get_or_create(name=ADMIN_ROLE)[0])
        user.is_superuser = True
        user.save()
        self.call(self.colleague, "get", "roles/", expect=403)
        self.call(self.colleague, "post", "cycles/", QUARTER, expect=403, format="json")
        self.assertEqual(self.call(self.colleague, "get", "me/")["is_admin"], False)

    def test_history_of_grants_and_revocations(self):
        granted = self.call(self.admin, "post", "roles/", {"employee_id": self.colleague.pk, "role": "admin"},
                            expect=201, format="json")
        self.assertEqual(granted["granted_by"]["id"], self.admin.pk)
        listed = {r["id"]: r for r in self.call(self.admin, "get", "roles/")["results"]}
        self.assertEqual(listed[granted["id"]]["granted_by"]["id"], self.admin.pk)
        # Two admins now, so the first one may go; the last one may not.
        self.call(self.colleague, "delete", f"roles/{granted['id']}/")
        admin_role = OKRRole.objects.get(company=self.company, role="admin")
        self.call(self.admin, "delete", f"roles/{admin_role.pk}/", expect=409)

        history = self.call(self.admin, "get", "roles/history/")["results"]
        self.assertEqual(
            [(e["employee"]["id"], e["role"], e["action"], e["actor"]["id"]) for e in history],
            [(self.colleague.pk, "admin", "revoked", self.colleague.pk),
             (self.colleague.pk, "admin", "granted", self.admin.pk)],
        )
        self.assertEqual(set(history[0]), {"id", "employee", "role", "action", "department", "actor", "created_at"})

    def test_department_manager_changes_are_logged(self):
        url = f"departments/{self.department.pk}/okr-manager/"
        self.call(self.admin, "put", url, {"manager_id": self.manager.pk}, format="json")
        self.call(self.admin, "put", url, {"manager_id": self.manager.pk}, format="json")  # no change, no event
        self.call(self.admin, "put", url, {"manager_id": self.colleague.pk}, format="json")
        self.call(self.admin, "put", url, {"manager_id": None}, format="json")
        events = [
            (e["employee"]["id"], e["action"], e["department"]["id"], e["role"])
            for e in reversed(self.call(self.admin, "get", "roles/history/")["results"])
        ]
        self.assertEqual(events, [
            (self.manager.pk, "granted", self.department.pk, "department_manager"),
            (self.manager.pk, "revoked", self.department.pk, "department_manager"),
            (self.colleague.pk, "granted", self.department.pk, "department_manager"),
            (self.colleague.pk, "revoked", self.department.pk, "department_manager"),
        ])

    def test_history_is_append_only_and_owned_by_the_employee(self):
        self.call(self.admin, "post", "roles/", {"employee_id": self.colleague.pk, "role": "manager"},
                  expect=201, format="json")
        event = OKRRoleEvent.objects.get(employee=self.colleague)
        with self.assertRaises(AppendOnlyError):
            event.save()
        with self.assertRaises(AppendOnlyError):
            OKRRoleEvent.objects.filter(pk=event.pk).delete()
        # HR's account deletion treats the history as the employee's own data.
        from employee.services import account_deletion

        owned = {(model._meta.label, field) for model, field, _ in account_deletion.OWNED_CASCADE_RELATIONS}
        self.assertIn(("okr.OKRRoleEvent", "employee"), owned)
        blockers = {model._meta.label for model, _, _ in account_deletion.OWNED_BLOCKING_RELATIONS}
        self.assertNotIn("okr.OKRRoleEvent", blockers)


class TeamAdminTests(APITestBase):
    def test_list_carries_members_and_company_checks(self):
        team = self.call(self.admin, "post", "teams/", {"name": "Team Van hanh", "department_id": self.department.pk,
                                                       "manager_id": self.manager.pk}, expect=201, format="json")
        self.call(self.admin, "put", f"teams/{team['id']}/members/",
                  {"employee_ids": [self.owner.pk, self.colleague.pk]}, format="json")
        row = next(t for t in self.call(self.owner, "get", "teams/")["results"] if t["id"] == team["id"])
        self.assertEqual(row["member_count"], 2)
        self.assertEqual({m["id"] for m in row["members"]}, {self.owner.pk, self.colleague.pk})
        self.assertEqual(row["manager"]["id"], self.manager.pk)
        # Remove one member by sending the shorter list.
        self.call(self.admin, "put", f"teams/{team['id']}/members/", {"employee_ids": [self.owner.pk]}, format="json")
        self.assertEqual([m["id"] for m in self.call(self.owner, "get", f"teams/{team['id']}/")["members"]],
                         [self.owner.pk])

        foreign_department = okr_department(self.other_company, "Phong cong ty B")
        bad = self.call(self.admin, "put", f"teams/{team['id']}/", {"name": "Team Van hanh",
                                                                    "department_id": foreign_department.pk,
                                                                    "manager_id": self.outsider.pk},
                        expect=400, format="json")
        self.assertEqual(set(bad["fields"]), {"department_id", "manager_id"})
        leaver = okr_employee(self.company, "w3_leaver", self.department)
        Employee._base_manager.filter(pk=leaver.pk).update(is_active=False)
        self.call(self.admin, "put", f"teams/{team['id']}/members/", {"employee_ids": [leaver.pk]},
                  expect=400, format="json")

    def test_hide_restore_and_filters(self):
        team = self.call(self.admin, "post", "teams/", {"name": "Team An"}, expect=201, format="json")
        self.call(self.admin, "post", f"teams/{team['id']}/status/", {"status": "inactive"}, format="json")
        self.assertEqual([t["id"] for t in self.call(self.owner, "get", "teams/?status=inactive")["results"]],
                         [team["id"]])
        self.call(self.admin, "post", f"teams/{team['id']}/status/", {"status": "active"}, format="json")
        self.assertIn(team["id"], {t["id"] for t in self.call(self.owner, "get", "teams/?status=active")["results"]})
        self.call(self.admin, "post", f"teams/{team['id']}/status/", {"status": "deleted"}, expect=400,
                  format="json")


class SettingsAdminTests(APITestBase):
    def test_reminder_schedule_is_reported_apart_from_the_setting(self):
        body = self.call(self.owner, "get", "settings/")
        self.assertEqual(body["reminder_schedule"], {"scheduled": False, "command": "okr_checkin_reminders",
                                                     "channels": ["in_app"], "email_available": False})
        with override_settings(OKR_REMINDERS_SCHEDULED=True):
            self.assertTrue(self.call(self.owner, "get", "settings/")["reminder_schedule"]["scheduled"])

    def test_rating_labels_and_threshold_reach_the_web_page(self):
        ranges = self.call(self.admin, "get", "settings/")["rating_ranges"]
        renamed = [{**band, "label": f"Muc {index}"} for index, band in enumerate(ranges)]
        saved = self.call(self.admin, "put", "settings/", {"warning_threshold": "22.5", "rating_ranges": renamed},
                          format="json")
        self.assertEqual([band["label"] for band in saved["rating_ranges"]], ["Muc 0", "Muc 1", "Muc 2", "Muc 3"])
        gap = [{**band} for band in renamed]
        gap[1]["min"] = "0.35"
        self.call(self.admin, "put", "settings/", {"rating_ranges": gap}, expect=400, format="json")

        self.client.force_login(self.owner.employee_user_id)
        html = self.client.get("/okr/").content.decode()
        config = json.loads(re.search(r'<script id="joyokr-config" type="application/json">(.*?)</script>', html,
                                      re.S).group(1))
        self.assertEqual(config["warningThreshold"], 22.5)


class AdminShellTests(APITestBase):
    def shell(self, employee):
        self.client.force_login(employee.employee_user_id)
        return self.client.get("/okr/").content.decode()

    def test_admin_links_follow_okr_roles(self):
        admin_html = self.shell(self.admin)
        for route in ("admin/roles", "admin/departments", "admin/teams", "admin/cycles", "admin/settings"):
            self.assertIn(f'href="/okr/{route}"', admin_html)
        manager_html = self.shell(self.manager)
        self.assertIn('href="/okr/admin/approvals"', manager_html)
        for route in ("admin/roles", "admin/cycles", "admin/settings"):
            self.assertNotIn(f'href="/okr/{route}"', manager_html)
        self.assertNotIn("admin/", self.shell(self.owner))
        self.assertEqual(self.client.get("/okr/admin/roles").status_code, 200)  # deep link; the API decides

    def test_assets_carry_a_version(self):
        # Static names are not hashed in HR, so a stale forms.js must not
        # survive an update next to a new views.js.
        html = self.shell(self.owner)
        versions = set(re.findall(r"okr_web/(?:js|css)/[a-z]+\.(?:js|css)\?v=([0-9a-f]{12})", html))
        self.assertEqual(len(versions), 1)
        self.assertEqual(len(re.findall(r"okr_web/(?:js|css)/[a-z]+\.(?:js|css)\?v=", html)), 9)
