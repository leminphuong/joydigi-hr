"""Web -> App assignment: an OKR assigned on the web (Django /okr/, HR
session) reaches the assignee in AppCheckin2 (JWT) through the same
/api/okr/ and the same database, and follows a change of owner.

Web calls ride on the HR login session (what /okr/ sends), app calls on a
JWT (what Flutter sends). The 14 steps of the brief, in the only order the
rules allow (a check-in needs an approved objective).
"""

from django.test import Client

from okr.models import OKRActivity, OKRCheckin, OKREvidence, OKRKeyResult, OKRNotification, OKRTeam, OKRTeamMember
from okr.models.choices import ReferenceType
from okr.tests.factories import okr_employee
from okr.tests.test_api import APITestBase


class AssignmentFlowBase(APITestBase):
    def setUp(self):
        super().setUp()
        self.team = OKRTeam.objects.create(company=self.company, department=self.department, name="Team Ban Hang",
                                           manager=self.manager, status="active")
        for member in (self.owner, self.colleague):
            OKRTeamMember.objects.create(company=self.company, team=self.team, employee=member)
        self.a, self.b = self.owner, self.colleague

    # -- the two clients ---------------------------------------------------------------

    def web(self, employee, method, path, data=None, expect=200):
        client = Client()
        client.force_login(employee.employee_user_id)
        call = getattr(client, method)
        response = (call(f"/api/okr/{path}", data=data, content_type="application/json") if data is not None
                    else call(f"/api/okr/{path}"))
        self.assertEqual(response.status_code, expect, response.content[:400])
        return response.json() if response.content else None

    def app(self, employee, method, path, data=None, expect=200):
        return self.call(employee, method, path, data, expect=expect, format="json")

    def mine(self, employee):
        return {o["id"] for o in self.app(employee, "get", "objectives/?owner=me&page_size=100")["results"]}

    def assign_on_web(self, owner):
        body = self.web(self.admin, "post", "objectives/", {
            "title": "Tang doanh so Team Ban Hang", "cycle_id": self.cycle.pk, "scope": "team",
            "department_id": self.department.pk, "team_id": self.team.pk, "owner_id": owner.pk,
            "parent_id": self.company_objective.pk, "weight": "100", "priority": "high",
        }, expect=201)
        for index in (1, 2):
            # The web form now proposes the objective's owner for each key result.
            self.web(self.admin, "post", "key-results/", {
                "objective_id": body["id"], "title": f"KR {index}", "unit": "%", "start_value": "0",
                "target_value": "100", "weight": "50", "owner_id": owner.pk,
            }, expect=201)
        return body["id"]

    def edit_on_web(self, objective_id, **changes):
        current = self.web(self.admin, "get", f"objectives/{objective_id}/")
        body = {"title": current["title"], "description": current["description"], "cycle_id": current["cycle"]["id"],
                "scope": current["scope"], "department_id": current["department_id"], "team_id": current["team_id"],
                "parent_id": (current["parent"] or {}).get("id"), "owner_id": current["owner"]["id"],
                "weight": current["weight"], "priority": current["priority"]}
        body.update(changes)
        return self.web(self.admin, "put", f"objectives/{objective_id}/", body)

    def key_results(self, objective_id):
        return list(OKRKeyResult.objects.filter(objective_id=objective_id).order_by("id"))


class WebToAppAssignmentTests(AssignmentFlowBase):
    def test_the_fourteen_steps(self):
        # 1-4. Admin creates the objective on the web: cycle, department, team, owner A.
        objective_id = self.assign_on_web(self.a)
        detail = self.web(self.admin, "get", f"objectives/{objective_id}/")
        self.assertEqual((detail["department_id"], detail["team_id"], detail["owner"]["id"], detail["approval_status"]),
                         (self.department.pk, self.team.pk, self.a.pk, "draft"))
        self.assertEqual({kr.owner_id for kr in self.key_results(objective_id)}, {self.a.pk})
        # A is told in the app.
        self.assertTrue(OKRNotification.objects.filter(recipient=self.a, notification_type="assigned",
                                                       reference_type=ReferenceType.OBJECTIVE,
                                                       reference_id=objective_id).exists())
        # 5. A opens the app and sees it under "Của tôi" (a draft, only A and managers see it).
        self.assertIn(objective_id, self.mine(self.a))
        self.app(self.a, "get", f"objectives/{objective_id}/")
        # 6. B is not assigned: not in B's "Của tôi", and a draft stays hidden from B.
        self.assertNotIn(objective_id, self.mine(self.b))
        self.app(self.b, "get", f"objectives/{objective_id}/", expect=404)

        # 7. Admin changes the owner from A to B on the web.
        self.edit_on_web(objective_id, owner_id=self.b.pk)
        # 8. B sees it, A no longer has it; A's key results moved to B with it.
        self.assertIn(objective_id, self.mine(self.b))
        self.assertNotIn(objective_id, self.mine(self.a))
        self.app(self.a, "get", f"objectives/{objective_id}/", expect=404)
        self.assertEqual({kr.owner_id for kr in self.key_results(objective_id)}, {self.b.pk})
        change = OKRActivity.objects.filter(objective_id=objective_id, action="updated").latest("id")
        self.assertEqual(change.metadata["owner_changed"]["from"], self.a.pk)

        # Submit (the owner, in the app) and the manager approves on the web (step 11).
        self.app(self.b, "post", f"objectives/{objective_id}/submit/")
        self.assertEqual(self.app(self.b, "get", f"objectives/{objective_id}/")["approval_status"], "submitted")
        self.web(self.manager, "post", f"objectives/{objective_id}/approve/")
        # 12. The app shows the new status.
        self.assertEqual(self.app(self.b, "get", f"objectives/{objective_id}/")["approval_status"], "approved")

        # 9. B checks in from the app; A (no longer owner) cannot.
        kr = self.key_results(objective_id)[0]
        self.app(self.a, "post", "checkins/", {"key_result": kr.pk, "current_value": "40", "confidence": 4,
                                              "update_text": "Khong con phu trach"}, expect=403)
        result = self.app(self.b, "post", "checkins/", {"key_result": kr.pk, "current_value": "60", "confidence": 4,
                                                       "update_text": "Da chot 3 hop dong"}, expect=201)
        self.assertEqual(result["key_result"]["progress"], "60.00")
        # 10. The web shows the new progress (manager and admin views).
        for viewer in (self.manager, self.admin):
            detail = self.web(viewer, "get", f"objectives/{objective_id}/")
            self.assertEqual(detail["progress"], "30.00")
            self.assertEqual(detail["key_results"][0]["progress"], "60.00")
        history = self.web(self.manager, "get", f"checkins/?key_result={kr.pk}")
        self.assertEqual([row["current_value"] for row in history["results"]], ["60"])

        # 13. Another company sees nothing, from the web or the app.
        self.web(self.outsider, "get", f"objectives/{objective_id}/", expect=404)
        self.app(self.outsider, "get", f"objectives/{objective_id}/", expect=404)
        self.assertNotIn(objective_id, {o["id"] for o in self.app(self.outsider, "get", "objectives/")["results"]})
        # 14. Approved: visible company-wide ("Công ty"), still only B's in "Của tôi".
        third = okr_employee(self.company, "assign_third", self.department)
        self.app(third, "get", f"objectives/{objective_id}/")
        self.assertNotIn(objective_id, self.mine(third))
        self.assertNotIn(objective_id, self.mine(self.a))


class AssignmentRuleTests(AssignmentFlowBase):
    def test_a_key_result_given_to_someone_puts_the_objective_in_their_list_once_visible(self):
        objective_id = self.assign_on_web(self.b)
        second = self.key_results(objective_id)[1]
        self.web(self.admin, "put", f"key-results/{second.pk}/", {
            "objective_id": objective_id, "title": second.title, "unit": "%", "start_value": "0",
            "target_value": "100", "weight": "50", "owner_id": self.a.pk})
        # A draft is not visible to A (not owner, not a manager): not listed yet.
        self.assertNotIn(objective_id, self.mine(self.a))
        self.app(self.b, "post", f"objectives/{objective_id}/submit/")
        self.web(self.manager, "post", f"objectives/{objective_id}/approve/")
        # Approved: A owns a key result in it, so it is in A's "Của tôi" and A checks in on it.
        self.assertIn(objective_id, self.mine(self.a))
        self.app(self.a, "post", "checkins/", {"key_result": second.pk, "current_value": "10", "confidence": 3,
                                              "update_text": "Bat dau"}, expect=201)
        # Listed once, not once per key result.
        body = self.app(self.a, "get", "objectives/?owner=me&page_size=100")
        self.assertEqual([o["id"] for o in body["results"]].count(objective_id), 1)

    def test_owner_change_keeps_key_results_given_to_someone_else(self):
        objective_id = self.assign_on_web(self.a)
        first, second = self.key_results(objective_id)
        OKRKeyResult.objects.filter(pk=second.pk).update(owner=self.manager)
        self.edit_on_web(objective_id, owner_id=self.b.pk)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.owner_id, second.owner_id), (self.b.pk, self.manager.pk))

    def test_only_an_admin_reassigns_and_never_to_an_inactive_or_foreign_employee(self):
        objective_id = self.assign_on_web(self.a)
        # A manager or the owner cannot move the assignment (association fields are admin-only).
        current = self.web(self.admin, "get", f"objectives/{objective_id}/")
        body = {"title": current["title"], "cycle_id": self.cycle.pk, "scope": "team",
                "department_id": self.department.pk, "team_id": self.team.pk, "owner_id": self.b.pk,
                "parent_id": self.company_objective.pk}
        self.web(self.a, "put", f"objectives/{objective_id}/", body)
        self.assertEqual(self.web(self.admin, "get", f"objectives/{objective_id}/")["owner"]["id"], self.a.pk)
        self.web(self.admin, "put", f"objectives/{objective_id}/", {**body, "owner_id": self.outsider.pk}, expect=400)
        self.b.is_active = False
        self.b.save()
        self.web(self.admin, "put", f"objectives/{objective_id}/", body, expect=400)
        self.assertEqual({kr.owner_id for kr in self.key_results(objective_id)}, {self.a.pk})


class KeyResultHistoryOnReassignmentTests(AssignmentFlowBase):
    """Changing the objective's owner never moves a key result that already has
    history (a check-in or evidence) without an explicit hand-over."""

    def approved_for_a(self):
        objective_id = self.assign_on_web(self.a)
        self.app(self.a, "post", f"objectives/{objective_id}/submit/")
        self.web(self.manager, "post", f"objectives/{objective_id}/approve/")
        return objective_id

    def checkin(self, employee, kr, value, expect=201):
        return self.app(employee, "post", "checkins/", {"key_result": kr.pk, "current_value": value, "confidence": 4,
                                                       "update_text": f"Cap nhat {value}"}, expect=expect)

    def history(self, kr):
        return list(OKRCheckin.objects.filter(key_result=kr).order_by("id").values_list("author_id", "current_value"))

    def test_key_result_with_checkins_stays_with_who_did_the_work(self):
        objective_id = self.approved_for_a()
        worked, untouched = self.key_results(objective_id)
        self.checkin(self.a, worked, "40")
        before = self.history(worked)
        self.edit_on_web(objective_id, owner_id=self.b.pk)
        worked.refresh_from_db()
        untouched.refresh_from_db()
        self.assertEqual((worked.owner_id, untouched.owner_id), (self.a.pk, self.b.pk))
        self.assertEqual(self.history(worked), before, "check-in history is never rewritten")
        change = OKRActivity.objects.filter(objective_id=objective_id, action="updated").latest("id")
        self.assertEqual(change.metadata["owner_changed"]["kept_with_history"], [worked.pk])
        self.assertEqual(change.metadata["owner_changed"]["key_results"], [untouched.pk])
        # A still answers for the key result A worked on; B takes the rest.
        self.checkin(self.a, worked, "50")
        self.checkin(self.b, worked, "55", expect=403)
        self.checkin(self.b, untouched, "10")
        self.checkin(self.a, untouched, "20", expect=403)
        self.assertIn(objective_id, self.mine(self.a), "A still has a key result in it")
        self.assertIn(objective_id, self.mine(self.b))

    def test_key_result_with_evidence_only_stays_and_its_evidence_stays_reachable(self):
        objective_id = self.assign_on_web(self.a)  # a draft: no check-in possible yet
        with_file, plain = self.key_results(objective_id)
        self.app(self.a, "post", "evidence/", {"key_result": with_file.pk, "evidence_type": "url",
                                              "url": "https://docs.example.com/bao-cao", "label": "Bao cao"},
                 expect=201)
        self.edit_on_web(objective_id, owner_id=self.b.pk)
        with_file.refresh_from_db()
        plain.refresh_from_db()
        self.assertEqual((with_file.owner_id, plain.owner_id), (self.a.pk, self.b.pk))
        evidence = OKREvidence.objects.get(key_result=with_file)
        self.assertEqual(evidence.author_id, self.a.pk)
        # The author keeps the key result and its evidence; the team manager sees it too.
        self.assertEqual(self.app(self.a, "get", f"evidence/?key_result={with_file.pk}")["count"], 1)
        self.assertEqual(self.web(self.manager, "get", f"evidence/?key_result={with_file.pk}")["count"], 1)
        # JOYOKR rule, kept: a draft key result is visible to its own owner and to managers,
        # so B sees the key result A still holds once the objective is approved.
        self.app(self.b, "get", f"evidence/?key_result={with_file.pk}", expect=404)

    def test_explicit_hand_over_moves_only_the_key_result_and_keeps_its_history(self):
        objective_id = self.approved_for_a()
        worked = self.key_results(objective_id)[0]
        self.checkin(self.a, worked, "40")
        before = self.history(worked)
        self.edit_on_web(objective_id, owner_id=self.b.pk)
        # The admin decides to hand the worked key result over too: an edit of that key result.
        current = self.web(self.admin, "get", f"key-results/{worked.pk}/")
        self.web(self.admin, "put", f"key-results/{worked.pk}/", {
            "objective_id": objective_id, "title": current["title"], "unit": current["unit"],
            "start_value": current["start_value"], "target_value": current["target_value"],
            "weight": current["weight"], "due_date": current["due_date"], "owner_id": self.b.pk})
        worked.refresh_from_db()
        self.assertEqual(worked.owner_id, self.b.pk)
        self.assertEqual(self.history(worked), before, "the earlier check-ins still name A")
        self.checkin(self.b, worked, "60")
        self.checkin(self.a, worked, "70", expect=403)
        # A keeps read access to the approved objective and its history.
        self.assertEqual(self.app(self.a, "get", f"checkins/?key_result={worked.pk}")["count"], 2)
        self.assertNotIn(objective_id, self.mine(self.a))

