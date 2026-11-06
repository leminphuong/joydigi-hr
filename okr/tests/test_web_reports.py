"""W4: dashboard, report, tree and check-in management for the web.

Default cycle rule with several active cycles, filters shared by every
figure, completion bands, overdue key results, check-in history and
statuses, permissions, and bounded query counts. Fake data only.
"""

import datetime
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from okr.models import OKRCheckin, OKRKeyResult, OKRObjective, OKRTeam, OKRTeamMember
from okr.models.choices import ApprovalStatus, Scope
from okr.services import okrs
from okr.tests.factories import okr_cycle, okr_department, okr_key_result, okr_objective
from okr.tests.test_api import APITestBase
from okr.tests.test_services import as_viewer


def approved_objective(cycle, owner, **fields):
    """An approved objective with two approved 0→100 key results, made
    directly: department and team objectives need rights the fixture's
    manager does not have, and the reports only read them."""
    objective = okr_objective(cycle, owner, approval_status=ApprovalStatus.APPROVED, **fields)
    for index in range(2):
        okr_key_result(objective, owner, title=f"KR {index + 1}", start_value=Decimal("0"),
                       current_value=Decimal("0"), target_value=Decimal("100"), weight=Decimal("50"),
                       approval_status=ApprovalStatus.APPROVED)
    return objective


def check_in(employee, key_result, value, when=None, text="Cap nhat tien do"):
    result = okrs.checkin(as_viewer(employee), key_result.pk, current_value=value, confidence=4, update_text=text)
    if when is not None:
        OKRCheckin._base_manager.filter(pk=result["checkin"].pk).update(created_at=when)
    return result["checkin"]


class DefaultCycleTests(APITestBase):
    def test_cycle_covering_today_wins_over_a_newer_active_one(self):
        today = timezone.localdate()
        future = okr_cycle(self.company, "Quy sau", start_date=today + datetime.timedelta(days=40),
                           end_date=today + datetime.timedelta(days=120), status="active")
        self.cycle.start_date, self.cycle.end_date = today - datetime.timedelta(days=10), today + datetime.timedelta(days=20)
        self.cycle.save()
        for path in ("dashboard/company/", "reports/", "tree/", "checkins/management/"):
            body = self.call(self.admin, "get", path)
            self.assertEqual(body["cycle"]["id"], self.cycle.pk, path)
            selection = body["cycle_selection"]
            self.assertEqual(selection["selected_by"], "default")
            self.assertEqual({c["id"] for c in selection["active_cycles"]}, {self.cycle.pk, future.pk})
            self.assertIn("hôm nay", selection["rule"])
        chosen = self.call(self.admin, "get", f"reports/?cycle={future.pk}")
        self.assertEqual((chosen["cycle"]["id"], chosen["cycle_selection"]["selected_by"]), (future.pk, "request"))
        self.assertEqual(self.call(self.owner, "get", "me/")["active_cycle_id"], self.cycle.pk)

    def test_without_a_current_one_the_newest_active_cycle_is_used(self):
        today = timezone.localdate()
        self.cycle.start_date, self.cycle.end_date = today - datetime.timedelta(days=100), today - datetime.timedelta(days=10)
        self.cycle.save()
        later = okr_cycle(self.company, "Quy toi", start_date=today + datetime.timedelta(days=5),
                          end_date=today + datetime.timedelta(days=90), status="active")
        self.assertEqual(self.call(self.admin, "get", "dashboard/company/")["cycle"]["id"], later.pk)


class DashboardFilterTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.team = OKRTeam.objects.create(company=self.company, name="Team Cham soc", department=self.department)
        self.mine = self.approved(title="Ca nhan cua An")
        self.dept = approved_objective(self.cycle, self.manager, scope=Scope.DEPARTMENT, department=self.department,
                                       parent=self.company_objective, title="Phong ban ky thuat")
        self.team_okr = approved_objective(self.cycle, self.manager, scope=Scope.TEAM, department=self.department,
                                           team=self.team, parent=self.dept, title="Team cham soc")
        self.pending = self.draft(title="Dang cho duyet")
        okrs.submit_objective(as_viewer(self.owner), self.pending.pk)
        kr = self.mine.key_results.order_by("id").first()
        check_in(self.owner, kr, "100")  # one finished KR

    def test_kpis_and_filters(self):
        everything = self.call(self.admin, "get", "dashboard/company/")
        for key in ("objective_stats", "in_progress_count", "pending_approval_count", "key_result_count",
                    "key_results_completed", "stale_count", "selection_progress", "expected_progress", "filters"):
            self.assertIn(key, everything)
        self.assertEqual(everything["objective_stats"]["total"], 4)  # company + three approved
        self.assertEqual(everything["pending_approval_count"], 1)
        self.assertEqual(everything["key_results_completed"], 1)
        self.assertEqual(everything["in_progress_count"],
                         everything["objective_stats"]["total"] - everything["objective_stats"]["completed"])

        team_only = self.call(self.admin, "get", f"dashboard/company/?team={self.team.pk}")
        self.assertEqual(team_only["objective_stats"]["total"], 1)
        self.assertEqual(team_only["filters"], {"team_id": self.team.pk})
        self.assertEqual(team_only["progress"], everything["progress"])  # the official figure is not filtered
        by_owner = self.call(self.admin, "get", f"dashboard/company/?owner={self.owner.pk}")
        self.assertEqual(by_owner["objective_stats"]["total"], 1)
        self.assertEqual(by_owner["pending_approval_count"], 1)
        self.assertEqual(by_owner["selection_progress"], "50.00")  # one objective: KRs at 100 and 0
        self.call(self.admin, "get", "dashboard/company/?scope=galaxy", expect=400)

    def test_other_company_and_drafts_stay_out(self):
        # Company B has no cycle at all, and company A's cycle is not theirs.
        self.assertEqual(self.call(self.outsider, "get", "dashboard/company/", expect=404)["code"], "no_cycle")
        self.call(self.outsider, "get", f"dashboard/company/?cycle={self.cycle.pk}", expect=404)
        # A colleague does not count someone else's submitted draft.
        self.assertEqual(self.call(self.colleague, "get", "dashboard/company/")["pending_approval_count"], 0)
        self.assertEqual(self.call(self.manager, "get", "dashboard/company/")["pending_approval_count"], 1)


class ReportGroupTests(APITestBase):
    def test_groups_bands_overdue_history_and_health_filter(self):
        team = OKRTeam.objects.create(company=self.company, name="Team Ban hang", department=self.department)
        personal = self.approved(title="Ca nhan")
        team_okr = approved_objective(self.cycle, self.manager, scope=Scope.TEAM, department=self.department,
                                      team=team, parent=self.company_objective, title="Team ban hang")
        first, second = personal.key_results.order_by("id")
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        OKRKeyResult.objects.filter(pk=first.pk).update(due_date=yesterday - datetime.timedelta(days=4))
        OKRKeyResult.objects.filter(pk=second.pk).update(due_date=yesterday)
        for value in ("20", "60", "90"):
            check_in(self.owner, first, value)
        check_in(self.owner, second, "100")  # past due but finished: not overdue

        body = self.call(self.colleague, "get", "reports/")
        self.assertEqual({g["label"] for g in body["by_department"]}, {"Phong Ky Thuat", "Chưa xác định"})
        self.assertEqual({g["label"] for g in body["by_team"]}, {"Team Ban hang", "Chưa xác định"})
        bands = {band["label"]: band["count"] for band in body["by_rating"]}
        self.assertEqual(sum(bands.values()), len(body["rows"]))
        self.assertEqual(bands["Excellent"], 1)  # personal: (90 + 100) / 2 = 95%
        self.assertEqual([(k["id"], k["days_late"]) for k in body["overdue_key_results"]], [(first.pk, 5)])
        self.assertEqual(body["checkin_count"], 4)
        latest = body["checkin_history"][0]
        self.assertEqual((latest["key_result"]["id"], latest["owner"]["id"]), (second.pk, self.owner.pk))
        self.assertEqual((latest["previous_progress"], latest["current_progress"]), ("0.00", "100.00"))

        team_rows = self.call(self.colleague, "get", f"reports/?team={team.pk}")
        self.assertEqual([r["id"] for r in team_rows["rows"]], [team_okr.pk])
        self.assertEqual(team_rows["checkin_count"], 0)  # history follows the filter
        self.assertEqual(team_rows["filters"], {"team_id": team.pk})
        healthy = self.call(self.colleague, "get", "reports/?health=completed")
        self.assertEqual(healthy["objective_stats"]["total"], len(healthy["rows"]))
        self.assertTrue(all(r["health"] == "completed" for r in healthy["rows"]))
        self.call(self.colleague, "get", "reports/?health=great", expect=400)

    def test_drafts_and_other_companies_never_reach_the_report(self):
        draft = self.draft(title="Ban nhap rieng")
        for employee in (self.colleague, self.owner, self.admin):
            ids = {r["id"] for r in self.call(employee, "get", "reports/")["rows"]}
            self.assertNotIn(draft.pk, ids)  # approved objectives only
        self.call(self.outsider, "get", f"reports/?cycle={self.cycle.pk}", expect=404)
        self.call(self.outsider, "get", f"tree/?cycle={self.cycle.pk}", expect=404)


class TreeLevelsTests(APITestBase):
    def test_company_department_team_personal_chain(self):
        team = OKRTeam.objects.create(company=self.company, name="Team Ho tro", department=self.department)
        dept = approved_objective(self.cycle, self.manager, scope=Scope.DEPARTMENT, department=self.department,
                                  parent=self.company_objective, title="Phong ban")
        team_okr = approved_objective(self.cycle, self.manager, scope=Scope.TEAM, department=self.department,
                                      team=team, parent=dept, title="Team")
        personal = self.approved(parent_id=team_okr.pk, title="Ca nhan")
        hidden = self.draft(owner=self.colleague, parent_id=team_okr.pk, title="Nhap cua Duc")

        def chain(body):
            root = next(n for n in body["items"] if n["id"] == self.company_objective.pk)
            level2 = next(n for n in root["children"] if n["id"] == dept.pk)
            level3 = next(n for n in level2["children"] if n["id"] == team_okr.pk)
            return root, level2, level3

        _, _, level3 = chain(self.call(self.owner, "get", "tree/"))
        self.assertEqual([n["id"] for n in level3["children"]], [personal.pk])  # Duc's draft is not Owner's to see
        leaf = level3["children"][0]
        self.assertEqual((leaf["owner"]["id"], leaf["cycle"]["id"], len(leaf["key_results"])),
                         (self.owner.pk, self.cycle.pk, 2))
        self.assertIn("due_date", leaf["key_results"][0])
        _, _, level3 = chain(self.call(self.colleague, "get", "tree/"))
        self.assertEqual({n["id"] for n in level3["children"]}, {personal.pk, hidden.pk})


class CheckinManagementTests(APITestBase):
    def setUp(self):
        super().setUp()
        self.objective = self.approved(title="Theo doi check-in")
        self.fresh, self.stale = self.objective.key_results.order_by("id")
        now = timezone.now()
        OKRKeyResult.objects.filter(pk__in=[self.fresh.pk, self.stale.pk]).update(
            created_at=now - datetime.timedelta(days=30)
        )
        # Fresh: 25 check-ins, the newest today. Stale: one, 10 days ago.
        for index in range(25):
            check_in(self.owner, self.fresh, str(index + 1), when=now - datetime.timedelta(hours=25 - index))
        check_in(self.owner, self.stale, "10", when=now - datetime.timedelta(days=10))

    def statuses(self, body):
        found = {}
        for department in body["departments"]:
            for team in department["teams"]:
                for person in team["people"]:
                    for objective in person["objectives"]:
                        for kr in objective["key_results"]:
                            found[kr["id"]] = (kr["checkin_status"], kr["checkin_total"], len(kr["checkins"]))
        return found

    def test_statuses_totals_and_filters(self):
        body = self.call(self.manager, "get", "checkins/management/")
        self.assertTrue(body["can_manage"])
        # The tree embeds the newest five check-ins; checkin_total counts them all.
        self.assertEqual(self.statuses(body), {self.fresh.pk: ("ok", 25, 5), self.stale.pk: ("overdue", 1, 1)})
        self.assertEqual((body["summary"]["checkin_count"], body["summary"]["overdue_count"]), (26, 1))
        overdue = self.call(self.manager, "get", "checkins/management/?status=overdue")
        self.assertEqual(set(self.statuses(overdue)), {self.stale.pk})
        self.assertEqual(overdue["filters"], {"status": "overdue"})
        self.assertEqual(self.statuses(self.call(self.manager, "get", "checkins/management/?status=due")), {})
        nobody = self.call(self.manager, "get", f"checkins/management/?owner={self.colleague.pk}")
        self.assertEqual(nobody["summary"]["employee_count"], 0)  # the colleague is not managed by Binh
        other_department = okr_department(self.company, "Phong khac")
        self.assertEqual(
            self.call(self.admin, "get", f"checkins/management/?department={other_department.pk}")["summary"][
                "employee_count"], 0)
        self.call(self.manager, "get", "checkins/management/?status=late", expect=400)

    def test_due_window_and_never(self):
        due_kr = self.approved(title="Sap den han").key_results.order_by("id").first()
        OKRKeyResult.objects.filter(pk=due_kr.pk).update(created_at=timezone.now() - datetime.timedelta(days=30))
        check_in(self.owner, due_kr, "5", when=timezone.now() - datetime.timedelta(days=6))
        never = self.approved(title="Moi tao").key_results.order_by("id").first()
        found = self.statuses(self.call(self.admin, "get", "checkins/management/"))
        self.assertEqual(found[due_kr.pk][0], "due")
        self.assertEqual(found[never.pk][0], "never")

    def test_employees_do_not_manage_anyone(self):
        body = self.call(self.colleague, "get", "checkins/management/")
        self.assertFalse(body["can_manage"])
        self.assertEqual((body["departments"], body["unassigned"]), ([], []))


class QueryCountTests(APITestBase):
    """The reporting endpoints run a fixed number of queries, however many
    objectives, key results and check-ins there are (no N+1)."""

    def grow(self, count):
        for index in range(count):
            objective = self.approved(title=f"Muc tieu {index}")
            for kr in objective.key_results.all():
                check_in(self.owner, kr, str(10 + index))

    def queries(self, path, employee):
        client = self.client_for(employee)
        with CaptureQueriesContext(connection) as captured:
            response = client.get(f"/api/okr/{path}")
        self.assertEqual(response.status_code, 200, response.content[:300])
        return len(captured.captured_queries)

    def test_counts_do_not_grow_with_the_data(self):
        paths = ("dashboard/company/", "reports/", "tree/", "checkins/management/", "checkins/queue/")
        self.grow(2)
        def measure():
            return {path: self.queries(path, self.owner if "queue" in path else self.manager) for path in paths}

        small = measure()
        self.grow(10)
        large = measure()
        self.assertEqual(small, large)
