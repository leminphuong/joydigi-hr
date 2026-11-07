"""OKR pilot gate (okr/pilot.py): OKR_ACCESS_MODE=all|pilot and
OKR_PILOT_EMPLOYEE_IDS. Web (/okr/, HR session) and REST (/api/okr/, JWT)
are both gated; HR itself is not. The 12 required checks, plus the edges
(empty list, unknown or empty mode closes to everyone, superuser,
revocation on the next request).
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from okr import pilot
from okr.models import OKRCheckin, OKRKeyResult, OKRNotification, OKRRole, OKRTeam, OKRTeamMember
from okr.services import notifications
from okr.templatetags.okr_web import okr_access
from okr.tests.factories import okr_employee
from okr.tests.test_api import APITestBase


def pilot_mode(*employees):
    return override_settings(OKR_ACCESS_MODE="pilot",
                             OKR_PILOT_EMPLOYEE_IDS=",".join(str(e.pk) for e in employees))


class PilotGateBase(APITestBase):
    def setUp(self):
        super().setUp()
        self.team = OKRTeam.objects.create(company=self.company, department=self.department, name="Team Pilot",
                                           manager=self.manager, status="active")
        for member in (self.owner, self.colleague):
            OKRTeamMember.objects.create(company=self.company, team=self.team, employee=member)

    def web(self, employee, method, path, data=None, expect=200):
        client = Client()
        client.force_login(employee.employee_user_id)
        call = getattr(client, method)
        response = (call(f"/api/okr/{path}", data=data, content_type="application/json") if data is not None
                    else call(f"/api/okr/{path}"))
        self.assertEqual(response.status_code, expect, response.content[:300])
        return response.json() if response.content else None

    def page(self, employee):
        client = Client()
        client.force_login(employee.employee_user_id)
        return client.get("/okr/")

    def denied(self, employee, path="me/"):
        body = self.call(employee, "get", path, expect=403)
        self.assertEqual(body["code"], pilot.NOT_ENABLED_CODE)
        return body

    def create_for(self, owner, expect=201):
        return self.web(self.admin, "post", "objectives/", {
            "title": "Pilot - Tang doanh so", "cycle_id": self.cycle.pk, "scope": "team",
            "department_id": self.department.pk, "team_id": self.team.pk, "owner_id": owner.pk,
            "parent_id": self.company_objective.pk}, expect=expect)

    def add_key_results(self, objective_id, owner):
        for index in (1, 2):
            self.web(self.admin, "post", "key-results/", {
                "objective_id": objective_id, "title": f"KR {index}", "unit": "%", "start_value": "0",
                "target_value": "100", "weight": "50", "owner_id": owner.pk}, expect=201)


class PilotModeFlowTests(PilotGateBase):
    def test_the_pilot_flow_and_who_is_kept_out(self):
        with pilot_mode(self.admin, self.manager, self.owner):
            # 1-3. A pilot admin creates a team objective and assigns it to a pilot employee.
            created = self.create_for(self.owner)
            self.add_key_results(created["id"], self.owner)
            # Assigning to someone outside the pilot is refused with a warning, nothing created.
            refused = self.create_for(self.colleague, expect=400)
            self.assertEqual(refused["fields"]["owner_id"], pilot.OWNER_NOT_IN_PILOT)
            # 4. The pilot employee sees it in the app.
            mine = self.call(self.owner, "get", "objectives/?owner=me")
            self.assertIn(created["id"], {o["id"] for o in mine["results"]})
            # 5. Outside the pilot: every OKR door is closed, app and web alike.
            for path in ("me/", "objectives/", f"objectives/{created['id']}/", "key-results/", "dashboard/",
                         "approvals/", "notifications/", "reports/", "people/", "roles/", "settings/",
                         "cycles/", "teams/", "tree/", "checkins/management/"):
                self.denied(self.colleague, path)
            self.web(self.colleague, "get", "me/", expect=403)
            page = self.page(self.colleague)
            self.assertEqual(page.status_code, 403)
            self.assertIn("chạy thử nghiệm", page.content.decode())
            self.assertFalse(okr_access(self.colleague.employee_user_id), "no OKR entry in the HR menu")
            self.assertTrue(okr_access(self.owner.employee_user_id))
            # 6. The manager approves within their rights.
            self.call(self.owner, "post", f"objectives/{created['id']}/submit/")
            self.web(self.manager, "post", f"objectives/{created['id']}/approve/")
            # 7-8. The employee checks in from the app; the web shows the progress.
            kr = OKRKeyResult.objects.filter(objective_id=created["id"]).order_by("id").first()
            self.call(self.owner, "post", "checkins/", {"key_result": kr.pk, "current_value": "60", "confidence": 4,
                                                        "update_text": "Pilot check-in"}, expect=201, format="json")
            self.assertEqual(self.web(self.admin, "get", f"objectives/{created['id']}/")["progress"], "30.00")
            # 9. Reassignment follows the same rule: only to a pilot employee.
            current = self.web(self.admin, "get", f"objectives/{created['id']}/")
            body = {"title": current["title"], "cycle_id": self.cycle.pk, "scope": "team",
                    "department_id": self.department.pk, "team_id": self.team.pk,
                    "parent_id": self.company_objective.pk, "owner_id": self.colleague.pk}
            moved = self.web(self.admin, "put", f"objectives/{created['id']}/", body, expect=400)
            self.assertEqual(moved["fields"]["owner_id"], pilot.OWNER_NOT_IN_PILOT)
        with pilot_mode(self.admin, self.manager, self.owner, self.colleague):
            self.web(self.admin, "put", f"objectives/{created['id']}/", body)

    def test_another_company_stays_out_even_when_listed(self):
        with pilot_mode(self.admin, self.owner, self.outsider):
            created = self.create_for(self.owner)
            self.call(self.outsider, "get", "me/")  # listed: OKR opens for their own company
            self.call(self.outsider, "get", f"objectives/{created['id']}/", expect=404)
            self.assertEqual(self.call(self.outsider, "get", "objectives/")["count"], 0)

    def test_removing_someone_takes_effect_on_their_next_request(self):
        with pilot_mode(self.owner):
            self.call(self.owner, "get", "me/")
        with pilot_mode():  # list reloaded without them
            self.denied(self.owner)
            self.assertEqual(self.page(self.owner).status_code, 403)

    def test_all_mode_behaves_as_before(self):
        for settings_override in (override_settings(), override_settings(OKR_ACCESS_MODE="all",
                                                                          OKR_PILOT_EMPLOYEE_IDS="")):
            with settings_override:
                for employee in (self.owner, self.colleague, self.manager):
                    self.call(employee, "get", "me/")
                    self.assertEqual(self.page(employee).status_code, 200)
                self.assertTrue(okr_access(self.colleague.employee_user_id))
                self.create_for(self.colleague)


class PilotGateEdgeTests(PilotGateBase):
    def test_empty_list_closes_okr_to_everyone_including_admins(self):
        with pilot_mode():
            for employee in (self.admin, self.manager, self.owner):
                self.denied(employee)

    def test_unknown_or_empty_mode_closes_okr_to_everyone_even_the_listed(self):
        listed = ",".join(str(e.pk) for e in (self.admin, self.manager, self.owner))
        for value in ("tat-ca", "pilto", "off", "", "  "):
            with self.subTest(mode=value), override_settings(OKR_ACCESS_MODE=value, OKR_PILOT_EMPLOYEE_IDS=listed):
                with self.assertLogs("okr.pilot", "WARNING"):
                    self.assertEqual(pilot.mode(), pilot.MODE_CLOSED)
                with self.assertLogs("okr.pilot", "WARNING"):
                    for employee in (self.admin, self.manager, self.owner, self.colleague):
                        self.denied(employee)
                    self.assertEqual(self.page(self.owner).status_code, 403)
                    self.assertFalse(okr_access(self.owner.employee_user_id))
                    self.assertEqual(pilot.pilot_employee_ids(), frozenset())
                    self.assertFalse(pilot.allows(self.owner.pk))

    def test_closed_gate_refuses_role_grants_from_the_command_line(self):
        authoriser = self.admin.employee_user_id
        authoriser.is_superuser = True
        authoriser.save()
        for settings_override in (pilot_mode(), override_settings(OKR_ACCESS_MODE="tat-ca",
                                                                  OKR_PILOT_EMPLOYEE_IDS=str(self.colleague.pk))):
            with settings_override, self.assertRaises(CommandError):
                call_command("okr_grant_role", "--employee", str(self.colleague.pk), "--role", "manager",
                             "--granted-by", authoriser.username, "--yes", stdout=StringIO())
        self.assertFalse(OKRRole.objects.filter(employee=self.colleague).exists())

    def test_superuser_and_okr_roles_do_not_bypass_the_gate(self):
        user = self.colleague.employee_user_id
        user.is_superuser = True
        user.is_staff = True
        user.save()
        OKRRole.objects.create(company=self.company, employee=self.colleague, role="admin")
        with pilot_mode(self.owner):
            self.denied(self.colleague)
            self.assertEqual(self.page(self.colleague).status_code, 403)

    def test_the_gate_never_grants_a_role(self):
        roles = set(OKRRole.objects.values_list("employee_id", "role"))
        with pilot_mode(self.owner, self.colleague):
            self.call(self.colleague, "get", "me/")
            self.assertNotIn("approve", self.call(self.colleague, "get", "me/").get("capabilities", []))
        self.assertEqual(set(OKRRole.objects.values_list("employee_id", "role")), roles)

    def test_pickers_roles_teams_and_notifications_stay_inside_the_pilot(self):
        with pilot_mode(self.admin, self.owner):
            people = {row["id"] for row in self.web(self.admin, "get", "people/?page_size=100")["results"]}
            self.assertEqual(people, {self.admin.pk, self.owner.pk})
            refused = self.web(self.admin, "post", "roles/", {"employee_id": self.colleague.pk, "role": "manager"},
                               expect=400)
            self.assertEqual(refused["fields"]["employee_id"], pilot.NOT_IN_PILOT)
            self.web(self.admin, "put", f"teams/{self.team.pk}/members/",
                     {"employee_ids": [self.owner.pk, self.colleague.pk]}, expect=400)
            self.assertIsNone(notifications.send(self.colleague, company_id=self.company.pk,
                                                 notification_type="assigned", title="x"))
            self.assertFalse(OKRNotification.objects.filter(recipient=self.colleague).exists())

    def test_hr_and_attendance_are_not_gated(self):
        today = timezone.localdate()
        for settings_override in (pilot_mode(self.owner), pilot_mode(),
                                  override_settings(OKR_ACCESS_MODE="tat-ca", OKR_PILOT_EMPLOYEE_IDS="")):
            with settings_override:
                client = APIClient()
                client.credentials(
                    HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(self.colleague.employee_user_id)}")
                response = client.get(reverse("api-timesheet-month"), {"year": today.year, "month": today.month})
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertNotIn(pilot.NOT_ENABLED_CODE, response.content.decode(errors="ignore"))
        self.assertFalse(OKRCheckin.objects.exists())


class PilotSettingParsingTests(PilotGateBase):
    def test_ids_from_text_or_list_and_bad_values_are_ignored(self):
        with override_settings(OKR_ACCESS_MODE=" Pilot ", OKR_PILOT_EMPLOYEE_IDS=f" {self.owner.pk}; x, -3 ,"):
            self.assertEqual(pilot.pilot_employee_ids(), frozenset({self.owner.pk}))
            self.assertTrue(pilot.allows(self.owner.pk))
            self.assertFalse(pilot.allows(None))
        with override_settings(OKR_ACCESS_MODE="pilot", OKR_PILOT_EMPLOYEE_IDS=[self.owner.pk, str(self.manager.pk)]):
            self.assertEqual(pilot.pilot_employee_ids(), frozenset({self.owner.pk, self.manager.pk}))
