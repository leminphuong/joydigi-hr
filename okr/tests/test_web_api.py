"""Phase 1 (web): the administration and reporting endpoints the JOYOKR web
UI needs, over JWT and over the HR web session with CSRF. Fake data only."""

import datetime

from django.utils import timezone
from rest_framework.test import APIClient

from okr.models import OKRDepartmentRole, OKRKeyResult, OKRObjective, OKRRole, OKRSettings, OKRTeam
from okr.models.choices import ApprovalStatus, Scope
from okr.services import okrs
from okr.tests.factories import okr_cycle, okr_employee, okr_objective
from okr.tests.test_api import APITestBase
from okr.tests.test_services import as_viewer

CSRF_SECRET = "a" * 32  # an unmasked CSRF secret, as Django accepts in the header


class SessionAuthTests(APITestBase):
    def session_client(self, employee, *, with_csrf: bool):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(employee.employee_user_id)
        if with_csrf:
            client.cookies["csrftoken"] = CSRF_SECRET
            client.credentials(HTTP_X_CSRFTOKEN=CSRF_SECRET)
        return client

    def test_web_session_reads_and_writes_with_csrf(self):
        client = self.session_client(self.owner, with_csrf=True)
        me = client.get("/api/okr/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["capabilities"], ["view", "edit_own", "checkin", "comment", "view_reports"])
        created = client.post(
            "/api/okr/objectives/",
            {"title": "Muc tieu tu web", "cycle_id": self.cycle.pk, "parent_id": self.company_objective.pk},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.json())

    def test_web_session_write_without_csrf_is_refused(self):
        client = self.session_client(self.owner, with_csrf=False)
        self.assertEqual(client.get("/api/okr/me/").status_code, 200)
        refused = client.post("/api/okr/objectives/", {"title": "x", "cycle_id": self.cycle.pk}, format="json")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["code"], "csrf_failed")
        self.assertFalse(OKRObjective.objects.filter(title="x").exists())

    def test_app_jwt_needs_no_csrf_and_anonymous_gets_401(self):
        self.call(self.owner, "post", "objectives/", {"title": "Tu app", "cycle_id": self.cycle.pk,
                                                      "parent_id": self.company_objective.pk}, expect=201, format="json")
        self.assertEqual(APIClient().get("/api/okr/objectives/").status_code, 401)


class ObjectiveAndKeyResultWriteTests(APITestBase):
    def create(self, employee=None, **data):
        values = {"title": "Muc tieu web", "cycle_id": self.cycle.pk, "scope": "personal",
                  "parent_id": self.company_objective.pk}
        values.update(data)
        return self.call(employee or self.owner, "post", "objectives/", values, expect=201, format="json")

    def test_create_update_delete_objective(self):
        body = self.create()
        self.assertEqual((body["owner"]["id"], body["approval_status"]), (self.owner.pk, "draft"))
        updated = self.call(self.owner, "put", f"objectives/{body['id']}/",
                            {"title": "Ten moi", "parent_id": self.company_objective.pk}, format="json")
        self.assertEqual(updated["title"], "Ten moi")
        # The manager may see a report's draft but not edit a personal objective.
        self.call(self.manager, "put", f"objectives/{body['id']}/", {"title": "x"}, expect=403, format="json")
        self.call(self.colleague, "put", f"objectives/{body['id']}/", {"title": "x"}, expect=404, format="json")
        self.call(self.outsider, "delete", f"objectives/{body['id']}/", expect=404)
        self.call(self.owner, "delete", f"objectives/{body['id']}/")
        self.assertEqual(self.call(self.owner, "get", f"objectives/{body['id']}/", expect=404)["code"], "deleted")

    def test_validation_and_scope_rules(self):
        error = self.call(self.owner, "post", "objectives/", {"title": "", "cycle_id": 999999}, expect=400, format="json")
        self.assertEqual(set(error["fields"]), {"title", "cycle_id"})
        self.call(self.colleague, "post", "objectives/",
                  {"title": "Cong ty", "cycle_id": self.cycle.pk, "scope": "company"}, expect=403, format="json")
        other_cycle = okr_cycle(self.other_company, "Q4 B web")
        self.call(self.owner, "post", "objectives/",
                  {"title": "x", "cycle_id": other_cycle.pk}, expect=400, format="json")
        self.call(self.owner, "post", "objectives/", ["not", "an", "object"], expect=400, format="json")

    def test_locked_after_submit(self):
        objective = self.draft()
        okrs.submit_objective(as_viewer(self.owner), objective.pk)
        self.assertEqual(
            self.call(self.owner, "put", f"objectives/{objective.pk}/", {"title": "x"}, expect=409, format="json")["code"],
            "objective_locked",
        )
        kr = objective.key_results.first()
        self.assertEqual(
            self.call(self.owner, "put", f"key-results/{kr.pk}/", {"title": "x"}, expect=409, format="json")["code"],
            "key_result_locked",
        )

    def test_key_result_crud_and_normalize(self):
        body = self.create()
        kr_values = {"objective_id": body["id"], "title": "KR web", "unit": "%", "start_value": "0",
                     "target_value": "100", "weight": "40"}
        first = self.call(self.owner, "post", "key-results/", kr_values, expect=201, format="json")
        self.assertEqual(first["permissions"]["can_add_evidence"], True)
        self.call(self.owner, "post", "key-results/", {**kr_values, "weight": "60"}, expect=201, format="json")
        error = self.call(self.owner, "post", "key-results/", {**kr_values, "unit": "", "target_value": "0"},
                          expect=400, format="json")
        self.assertEqual(set(error["fields"]), {"unit", "target_value"})
        self.call(self.colleague, "post", "key-results/", kr_values, expect=404, format="json")
        updated = self.call(self.owner, "put", f"key-results/{first['id']}/",
                            {**kr_values, "title": "KR doi ten", "current_value": "25"}, format="json")
        self.assertEqual((updated["title"], updated["progress"]), ("KR doi ten", "25.00"))
        normalized = self.call(self.owner, "post", f"objectives/{body['id']}/normalize-kr-weights/")
        self.assertEqual([kr["weight"] for kr in normalized["key_results"]], ["50.00", "50.00"])
        self.call(self.owner, "delete", f"key-results/{first['id']}/")
        self.assertTrue(OKRKeyResult.all_objects.get(pk=first["id"]).is_deleted)

    def test_web_edit_round_trip_keeps_every_field(self):
        """The web form PUTs back what it read: nothing may get lost."""
        body = self.create(description="Mo ta muc tieu", priority="high")
        detail = self.call(self.owner, "get", f"objectives/{body['id']}/")
        self.assertEqual((detail["priority"], detail["permissions"]["can_edit"]), ("high", True))
        kr = self.call(self.owner, "post", "key-results/", {
            "objective_id": body["id"], "title": "KR co mo ta", "description": "Giu lai mo ta", "unit": "%",
            "start_value": "0", "target_value": "100", "weight": "50", "priority": "high",
        }, expect=201, format="json")
        self.assertEqual((kr["description"], kr["priority"], kr["permissions"]["can_edit"]), ("Giu lai mo ta", "high", True))
        form = {field: kr[field] for field in ("title", "description", "unit", "start_value", "current_value",
                                               "target_value", "weight", "priority", "due_date")}
        saved = self.call(self.owner, "put", f"key-results/{kr['id']}/",
                          {**form, "objective_id": body["id"], "title": "KR doi ten"}, format="json")
        self.assertEqual((saved["title"], saved["description"], saved["priority"]), ("KR doi ten", "Giu lai mo ta", "high"))
        objective_form = {field: detail[field] for field in ("title", "description", "scope", "weight", "priority")}
        self.call(self.owner, "put", f"objectives/{body['id']}/",
                  {**objective_form, "cycle_id": self.cycle.pk, "parent_id": detail["parent_id"]}, format="json")
        again = self.call(self.owner, "get", f"objectives/{body['id']}/")
        self.assertEqual((again["description"], again["priority"]), ("Mo ta muc tieu", "high"))

    def test_objective_filters_for_the_web(self):
        mine = self.draft(title="Ban nhap A")
        self.approved(title="Da duyet B")
        manager_view = self.call(self.manager, "get", f"objectives/?owner={self.owner.pk}")
        self.assertEqual({o["id"] for o in manager_view["results"]} >= {mine.pk}, True)
        colleague_view = self.call(self.colleague, "get", f"objectives/?owner={self.owner.pk}")
        self.assertNotIn(mine.pk, {o["id"] for o in colleague_view["results"]})
        titles = [o["title"] for o in self.call(self.owner, "get", "objectives/?owner=me&order_by=title")["results"]]
        self.assertEqual(titles, sorted(titles))
        self.call(self.owner, "get", "objectives/?order_by=random", expect=400)


class CycleAdminTests(APITestBase):
    data = {"name": "Q1 2027", "cycle_type": "quarter", "start_date": "2027-01-01", "end_date": "2027-03-31"}

    def test_cycle_lifecycle(self):
        self.call(self.manager, "post", "cycles/", self.data, expect=403, format="json")
        created = self.call(self.admin, "post", "cycles/", self.data, expect=201, format="json")
        self.assertEqual(created["status"], "draft")
        self.call(self.admin, "put", f"cycles/{created['id']}/", {**self.data, "name": "Quy 1/2027"}, format="json")
        self.assertEqual(self.call(self.owner, "get", f"cycles/{created['id']}/")["name"], "Quy 1/2027")
        self.call(self.admin, "post", f"cycles/{created['id']}/status/", {"status": "closed"}, expect=400, format="json")
        self.draft()
        cloned = self.call(self.admin, "post", f"cycles/{self.cycle.pk}/clone/",
                           {"destination_cycle_id": created["id"]}, format="json")
        self.assertEqual(cloned["objective_count"], 2)
        self.call(self.admin, "post", f"cycles/{created['id']}/status/", {"status": "active"}, format="json")
        listed = {c["id"]: c for c in self.call(self.owner, "get", "cycles/")["results"]}
        self.assertEqual(listed[created["id"]]["objective_count"], 2)
        self.call(self.outsider, "get", f"cycles/{created['id']}/", expect=404)


class OrganizationTests(APITestBase):
    def test_people_and_departments_come_from_hr(self):
        people = self.call(self.colleague, "get", "people/?page_size=100")
        ids = {p["id"] for p in people["results"]}
        self.assertTrue({self.owner.pk, self.manager.pk, self.admin.pk} <= ids)
        self.assertNotIn(self.outsider.pk, ids)
        self.assertEqual(set(people["results"][0]), {"id", "name", "department_id", "department_name", "manager_id"})
        departments = self.call(self.colleague, "get", "departments/")["results"]
        self.assertIn(self.department.pk, {d["id"] for d in departments})
        self.call(self.manager, "put", f"departments/{self.department.pk}/okr-manager/",
                  {"manager_id": self.manager.pk}, expect=403, format="json")
        set_ = self.call(self.admin, "put", f"departments/{self.department.pk}/okr-manager/",
                         {"manager_id": self.manager.pk}, format="json")
        self.assertEqual(set_["okr_manager"]["id"], self.manager.pk)
        self.call(self.admin, "put", f"departments/{self.department.pk}/okr-manager/",
                  {"manager_id": self.outsider.pk}, expect=400, format="json")
        self.call(self.admin, "put", f"departments/{self.department.pk}/okr-manager/", {"manager_id": None}, format="json")
        self.assertFalse(OKRDepartmentRole.objects.filter(department=self.department).exists())

    def test_teams(self):
        self.call(self.owner, "post", "teams/", {"name": "Team A"}, expect=403, format="json")
        team = self.call(self.admin, "post", "teams/", {"name": "Team Ho tro", "department_id": self.department.pk,
                                                       "manager_id": self.manager.pk}, expect=201, format="json")
        members = self.call(self.admin, "put", f"teams/{team['id']}/members/",
                            {"employee_ids": [self.owner.pk, self.colleague.pk]}, format="json")
        self.assertEqual({m["id"] for m in members["members"]}, {self.owner.pk, self.colleague.pk})
        self.call(self.admin, "put", f"teams/{team['id']}/members/", {"employee_ids": [self.outsider.pk]},
                  expect=400, format="json")
        hidden = self.call(self.admin, "post", f"teams/{team['id']}/status/", {"status": "inactive"}, format="json")
        self.assertEqual(hidden["status"], "inactive")
        self.assertEqual(self.call(self.owner, "get", "teams/?status=active")["count"], 0)
        self.call(self.outsider, "get", f"teams/{team['id']}/", expect=404)

    def test_roles(self):
        self.call(self.manager, "get", "roles/", expect=403)
        granted = self.call(self.admin, "post", "roles/", {"employee_id": self.colleague.pk, "role": "manager"},
                            expect=201, format="json")
        self.call(self.admin, "post", "roles/", {"employee_id": self.colleague.pk, "role": "manager"},
                  expect=409, format="json")
        self.call(self.admin, "post", "roles/", {"employee_id": self.outsider.pk, "role": "admin"},
                  expect=400, format="json")
        self.call(self.admin, "delete", f"roles/{granted['id']}/")
        admin_role = OKRRole.objects.get(company=self.company, role="admin")
        self.assertEqual(self.call(self.admin, "delete", f"roles/{admin_role.pk}/", expect=409)["code"], "last_admin")
        self.call(self.outsider, "delete", f"roles/{admin_role.pk}/", expect=404)

    def test_settings(self):
        defaults = self.call(self.owner, "get", "settings/")
        self.assertEqual((defaults["warning_threshold"], defaults["reminder_frequency"]), ("15.00", "weekly"))
        self.call(self.owner, "put", "settings/", {"warning_threshold": "20"}, expect=403, format="json")
        bad = self.call(self.admin, "put", "settings/", {"warning_threshold": "120", "rating_ranges": [{"min": 0}]},
                        expect=400, format="json")
        self.assertEqual(set(bad["fields"]), {"warning_threshold", "rating_ranges"})
        saved = self.call(self.admin, "put", "settings/", {"warning_threshold": "20", "reminder_frequency": "biweekly"},
                          format="json")
        self.assertEqual(saved["warning_threshold"], "20.00")
        self.assertEqual(OKRSettings.objects.get(company=self.company).reminder_frequency, "biweekly")


class ReportingTests(APITestBase):
    def test_company_dashboard(self):
        objective = self.approved()
        kr = objective.key_results.order_by("id").first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="80", confidence=4, update_text="Tot")
        body = self.call(self.colleague, "get", "dashboard/company/")
        self.assertEqual(body["objective_stats"]["total"], 2)
        self.assertEqual(body["progress"]["objective_count"], 1)
        self.assertEqual(body["key_result_count"], 2)
        self.assertEqual({o["id"] for o in body["featured"]}, {objective.pk, self.company_objective.pk})
        self.assertEqual(self.call(self.outsider, "get", "dashboard/company/", expect=404)["code"], "no_cycle")

    def test_report_with_trend_and_filters(self):
        objective = self.approved()
        kr = objective.key_results.order_by("id").first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="60", confidence=4, update_text="Tot")
        body = self.call(self.manager, "get", "reports/")
        self.assertEqual(len(body["trend"]), 1)
        self.assertEqual(body["trend"][0]["date"], timezone.localdate().isoformat())
        self.assertEqual({row["label"] for row in body["by_scope"]}, {"personal", "company"})
        narrowed = self.call(self.manager, "get", f"reports/?owner={self.owner.pk}")
        self.assertEqual([row["id"] for row in narrowed["rows"]], [objective.pk])
        self.call(self.manager, "get", "reports/?scope=galaxy", expect=400)

    def test_tree_links_and_unlinked(self):
        child = self.approved()
        orphan = okr_objective(self.cycle, self.owner, title="Khong lien ket", approval_status=ApprovalStatus.APPROVED)
        body = self.call(self.colleague, "get", "tree/")
        roots = body["items"]
        self.assertEqual([r["id"] for r in roots], [self.company_objective.pk])
        self.assertEqual([c["id"] for c in roots[0]["children"]], [child.pk])
        self.assertEqual(len(roots[0]["children"][0]["key_results"]), 2)
        # The tree shows each KR's deadline, as JOYOKR does.
        kr = child.key_results.order_by("pk").first()
        kr.due_date = datetime.date(2027, 1, 4)
        kr.save(update_fields=["due_date"])
        tree_kr = next(
            k for k in self.call(self.colleague, "get", "tree/")["items"][0]["children"][0]["key_results"]
            if k["id"] == kr.pk
        )
        self.assertEqual(tree_kr["due_date"], "2027-01-04")
        self.assertEqual([u["id"] for u in body["unlinked_items"]], [orphan.pk])
        draft = self.draft(title="Ban nhap")
        self.assertNotIn(draft.pk, self._ids(self.call(self.colleague, "get", "tree/")))
        self.assertIn(draft.pk, self._ids(self.call(self.owner, "get", "tree/")))

    def _ids(self, body):
        found = []

        def walk(nodes):
            for node in nodes:
                found.append(node["id"])
                walk(node["children"])

        walk(body["items"])
        walk(body["unlinked_items"])
        return found

    def test_checkin_queue_and_management(self):
        objective = self.approved()
        queue = self.call(self.owner, "get", "checkins/queue/")
        self.assertEqual(queue["count"], 2)
        self.assertIn("latest_checkin", queue["results"][0])
        self.assertEqual(self.call(self.colleague, "get", "checkins/queue/")["count"], 0)
        kr = objective.key_results.order_by("id").first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="30", confidence=3, update_text="Dang lam")
        managed = self.call(self.manager, "get", "checkins/management/")
        self.assertTrue(managed["can_manage"])
        self.assertEqual(managed["summary"]["key_result_count"], 2)
        self.assertEqual(managed["summary"]["checkin_count"], 1)
        people = [p for d in managed["departments"] for t in d["teams"] for p in t["people"]] + managed["unassigned"]
        self.assertEqual([p["employee"]["id"] for p in people], [self.owner.pk])
        self.assertFalse(self.call(self.colleague, "get", "checkins/management/")["can_manage"])
        everyone = self.call(self.admin, "get", "checkins/management/")
        self.assertEqual(everyone["scope"], "company")
        self.assertGreaterEqual(everyone["summary"]["employee_count"], 4)
