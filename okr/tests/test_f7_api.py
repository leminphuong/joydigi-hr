"""F7 (app): what the Flutter "Công ty" tab, the manager's employee view,
notifications and evidence-per-check-in read from the API.

Children of one objective (``objectives/?parent=``), another person's
objectives (``objectives/?owner=``), ``actor`` on notifications and
``checkin_id`` on evidence — each under the viewer's visibility and the
company boundary. Fake data only.
"""

from decimal import Decimal

from okr.models import OKRNotification
from okr.models.choices import ApprovalStatus, Scope
from okr.services import engagement, notifications, okrs
from okr.tests.factories import okr_cycle, okr_key_result, okr_objective
from okr.tests.test_api import APITestBase, OBJECTIVE_KEYS
from okr.tests.test_services import as_viewer


def approved_child(parent, owner, **fields):
    child = okr_objective(parent.cycle, owner, parent=parent, approval_status=ApprovalStatus.APPROVED, **fields)
    okr_key_result(child, owner, start_value=Decimal("0"), current_value=Decimal("40"), target_value=Decimal("100"),
                   weight=Decimal("100"), approval_status=ApprovalStatus.APPROVED)
    return child


class ObjectiveChildrenTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.department_okr = approved_child(self.company_objective, self.manager, scope=Scope.DEPARTMENT,
                                             department=self.department, title="Phong ban dat muc tieu")
        self.owner_draft = self.draft(title="Ban nhap cua An")  # child of the company objective

    def ids(self, employee, path):
        page = self.call(employee, "get", path)
        return {row["id"] for row in page["results"]}, page

    def test_children_follow_visibility(self):
        path = f"objectives/?parent={self.company_objective.pk}&cycle={self.cycle.pk}"
        colleague_ids, page = self.ids(self.colleague, path)
        self.assertEqual(colleague_ids, {self.department_okr.pk}, "a colleague never sees someone's draft")
        for row in page["results"]:
            self.assertEqual(set(row), OBJECTIVE_KEYS)
            self.assertEqual(row["parent_id"], self.company_objective.pk)
        self.save_fixture("objective_children", page)
        self.assertEqual(self.ids(self.owner, path)[0], {self.department_okr.pk, self.owner_draft.pk})
        self.assertEqual(self.ids(self.manager, path)[0], {self.department_okr.pk, self.owner_draft.pk})
        self.assertEqual(self.ids(self.outsider, path)[0], set(), "another company's tree is empty")

    def test_child_count_matches_what_the_viewer_can_open(self):
        def count(employee):
            page = self.call(employee, "get", f"objectives/?scope=company&cycle={self.cycle.pk}")
            row = next(r for r in page["results"] if r["id"] == self.company_objective.pk)
            return row["child_count"]

        self.assertEqual(count(self.colleague), 1)
        self.assertEqual(count(self.manager), 2)

    def test_bad_parent_is_a_field_error(self):
        body = self.call(self.owner, "get", "objectives/?parent=abc", expect=400)
        self.assertEqual(body["fields"], {"parent": "Giá trị không hợp lệ."})


class DepartmentListTests(APITestBase):
    """The "Công ty" filter lists the viewer's company departments only."""

    def test_any_okr_user_lists_own_company_departments(self):
        page = self.call(self.colleague, "get", "departments/?page_size=100")
        names = {row["name"] for row in page["results"]}
        self.assertIn(self.department.department, names)
        for row in page["results"]:
            self.assertEqual(set(row), {"id", "name", "okr_manager"})
        self.save_fixture("departments", page)
        other = self.call(self.outsider, "get", "departments/?page_size=100")
        self.assertNotIn(self.department.pk, {row["id"] for row in other["results"]})


class EmployeeObjectivesTests(APITestBase):
    """The manager's "Quản lý" tab opens one employee's objectives."""

    def test_manager_sees_reports_drafts_colleague_only_approved(self):
        approved = self.approved(title="Da duyet cua An")
        draft = self.draft(title="Nhap cua An")
        path = f"objectives/?owner={self.owner.pk}&cycle={self.cycle.pk}"
        manager_ids = {row["id"] for row in self.call(self.manager, "get", path)["results"]}
        self.assertEqual(manager_ids, {approved.pk, draft.pk})
        colleague_ids = {row["id"] for row in self.call(self.colleague, "get", path)["results"]}
        self.assertEqual(colleague_ids, {approved.pk})
        self.assertEqual(self.call(self.outsider, "get", path)["count"], 0)
        row = next(r for r in self.call(self.manager, "get", path)["results"] if r["id"] == approved.pk)
        self.assertEqual(row["cycle"]["id"], self.cycle.pk)
        self.assertIn(row["health"], {"on_track", "at_risk", "off_track"})

    def test_other_cycle_is_not_mixed_in(self):
        other = okr_cycle(self.company, "Q1 2027", status="active")
        mine = self.approved(title="Quy nay")
        later = okr_objective(other, self.owner, approval_status=ApprovalStatus.APPROVED, title="Quy sau")
        ids = {row["id"] for row in self.call(self.manager, "get", f"objectives/?owner={self.owner.pk}&cycle={self.cycle.pk}")["results"]}
        self.assertIn(mine.pk, ids)
        self.assertNotIn(later.pk, ids)


class NotificationActorTests(APITestBase):
    def latest(self, employee):
        return self.call(employee, "get", "notifications/")["results"][0]

    def test_actor_on_each_kind(self):
        objective = self.draft()
        self.call(self.owner, "post", f"objectives/{objective.pk}/submit/")
        self.assertEqual(self.latest(self.manager)["actor"]["id"], self.owner.pk)
        self.call(self.manager, "post", f"objectives/{objective.pk}/approve/")
        approved = self.latest(self.owner)
        self.assertEqual((approved["type"], approved["actor"]["id"]), ("approved", self.manager.pk))
        self.call(self.manager, "post", "comments/", {"objective": objective.pk, "content": "Tot lam"}, expect=201, format="json")
        comment = self.latest(self.owner)
        self.assertEqual((comment["type"], comment["actor"]["id"]), ("comment", self.manager.pk))
        kr = objective.key_results.order_by("id").first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="20", confidence=3, update_text="Dang lam")
        checkin = self.latest(self.manager)
        self.assertEqual((checkin["type"], checkin["actor"]["id"]), ("checkin", self.owner.pk))

    def test_reminders_and_foreign_actors_have_none(self):
        stored = notifications.send(self.owner, company_id=self.company.pk, notification_type="checkin_reminder",
                                    title="Den han", actor=self.outsider)
        self.assertIsNone(stored.actor_id, "an employee of another company is never named")
        self.assertIsNone(self.latest(self.owner)["actor"])

    def test_deleted_actor_keeps_the_notification(self):
        objective = self.draft()
        self.call(self.owner, "post", f"objectives/{objective.pk}/submit/")
        OKRNotification.objects.filter(recipient=self.manager).update(actor=None)
        self.assertIsNone(self.latest(self.manager)["actor"])


class EvidencePerCheckinTests(APITestBase):
    def test_checkin_id_links_evidence_to_its_checkin(self):
        objective = self.approved()
        first, second = objective.key_results.order_by("id")
        saved = okrs.checkin(as_viewer(self.owner), first.pk, current_value="30", confidence=4, update_text="Tien do")
        checkin_id = saved["checkin"].pk
        linked = self.call(self.owner, "post", "evidence/", {"key_result": first.pk, "url": "https://example.com/a",
                                                             "label": "Bien ban", "checkin": checkin_id},
                           expect=201, format="json")
        self.assertEqual(linked["checkin_id"], checkin_id)
        loose = self.call(self.owner, "post", "evidence/", {"key_result": first.pk, "url": "https://example.com/b",
                                                            "label": "Rieng"}, expect=201, format="json")
        self.assertIsNone(loose["checkin_id"])
        page = self.call(self.colleague, "get", f"evidence/?key_result={first.pk}")
        self.assertEqual({row["id"]: row["checkin_id"] for row in page["results"]},
                         {linked["id"]: checkin_id, loose["id"]: None})
        self.save_fixture("evidence_checkin", page)
        # A check-in of another key result cannot be the link.
        body = self.call(self.owner, "post", "evidence/", {"key_result": second.pk, "url": "https://example.com/c",
                                                           "label": "Sai", "checkin": checkin_id},
                         expect=400, format="json")
        self.assertIn("checkin", body["fields"])

    def test_list_comments_unchanged_by_actor(self):
        # The comment notification names the author; the comment itself is unchanged.
        objective = self.approved()
        engagement.create_comment(as_viewer(self.manager), objective_id=objective.pk, content="Xem lai KR 2")
        self.assertEqual(self.call(self.owner, "get", "notifications/")["results"][0]["actor"]["id"], self.manager.pk)
