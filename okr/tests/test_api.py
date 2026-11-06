"""Gate 3: ``/api/okr/`` integration tests through the real URL, JWT
authentication and error handling. Fake data only.

Set ``OKR_CONTRACT_FIXTURES=<dir>`` to also write the JSON bodies these
tests receive; the app's DTO tests parse them (two-sided contract check).
"""

import json
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from okr.models import OKRCheckin, OKREvidence, OKRNotification
from okr.models.choices import ApprovalStatus
from okr.services import okrs
from okr.tests.factories import okr_company, okr_employee
from okr.tests.test_services import ServiceTestBase, as_viewer

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

OBJECTIVE_KEYS = {
    "id", "title", "scope", "approval_status", "progress", "expected_progress", "health", "provisional",
    "key_result_count", "weight", "owner", "cycle", "department_name", "team_name", "parent_id", "child_count",
}
DETAIL_KEYS = OBJECTIVE_KEYS | {
    "description", "priority", "department_id", "team_id", "parent", "key_results", "permissions",
    "comment_count", "rejection_reason",
}
KEY_RESULT_KEYS = {
    "id", "objective_id", "objective_title", "title", "unit", "start_value", "current_value", "target_value",
    "weight", "progress", "expected_progress", "health", "due_date", "approval_status", "owner",
    "last_checkin_at", "permissions", "description", "priority",
}
CHECKIN_KEYS = {
    "id", "created_at", "author", "previous_value", "current_value", "previous_progress", "current_progress",
    "confidence", "update_text", "blockers", "next_steps",
}
ATTENTION_KEYS = {
    "key_result_id", "key_result_title", "objective_title", "reason", "progress", "expected_progress",
    "days_overdue", "last_checkin_at",
}
ERROR_KEYS = {"code", "message", "fields"}


class APITestBase(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.private_root = tempfile.mkdtemp(prefix="okr_api_private_")
        self.addCleanup(shutil.rmtree, self.private_root, True)
        self.storage = override_settings(OKR_PRIVATE_MEDIA_ROOT=self.private_root)
        self.storage.enable()
        self.addCleanup(self.storage.disable)

    def client_for(self, employee=None, user=None):
        client = APIClient()
        user = user or employee.employee_user_id
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
        return client

    def call(self, employee, method, path, data=None, expect=200, **extra):
        client = self.client_for(employee)
        response = getattr(client, method)(f"/api/okr/{path}", data, **extra)
        body = response.json() if response.get("Content-Type", "").startswith("application/json") else None
        self.assertEqual(response.status_code, expect, body)
        if expect >= 400:
            self.assertEqual(set(body), ERROR_KEYS)
            self.assertTrue(body["message"])
        return body

    def save_fixture(self, name, body):
        target = os.environ.get("OKR_CONTRACT_FIXTURES")
        if target:
            path = Path(target) / f"{name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


class AuthAndShapeTests(APITestBase):
    def test_requires_a_valid_token(self):
        response = APIClient().get("/api/okr/me/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")
        bad = APIClient()
        bad.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
        self.assertEqual(bad.get("/api/okr/me/").status_code, 401)

    def test_revoked_session_is_rejected(self):
        client = self.client_for(self.owner)
        user = self.owner.employee_user_id
        user.session_version += 1
        user.save(update_fields=["session_version"])
        self.assertEqual(client.get("/api/okr/me/").status_code, 401)

    def test_account_without_employee_gets_403(self):
        from joydigi.testkit import make_user

        user = make_user("api_no_employee", password="okr-fake-pass-1", email="api_no_employee@example.com")
        response = self.client_for(user=user).get("/api/okr/me/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "no_employee")

    def test_me_reports_roles(self):
        body = self.call(self.owner, "get", "me/")
        self.assertEqual(
            body,
            {
                "employee_id": self.owner.pk,
                "name": self.owner.get_full_name(),
                "can_manage": False,
                "can_approve": False,
                "is_admin": False,
                "capabilities": ["view", "edit_own", "checkin", "comment", "view_reports"],
                "active_cycle_id": self.cycle.pk,
            },
        )
        self.assertTrue(self.call(self.manager, "get", "me/")["can_approve"])
        admin = self.call(self.admin, "get", "me/")
        self.assertTrue(admin["can_manage"] and admin["is_admin"])
        self.assertIn("manage_cycles", admin["capabilities"])
        self.save_fixture("me", body)

    def test_unknown_method_and_page_use_the_error_shape(self):
        self.call(self.owner, "delete", "me/", expect=405)
        self.call(self.owner, "get", "objectives/?page=99", expect=404)
        self.call(self.owner, "get", "objectives/?cycle=abc", expect=400)
        self.call(self.owner, "get", "objectives/?status=deleted", expect=400)


class ReadEndpointTests(APITestBase):
    def test_cycles_are_company_scoped(self):
        body = self.call(self.owner, "get", "cycles/")
        self.assertEqual(set(body), {"count", "next", "previous", "results"})
        self.assertEqual([c["id"] for c in body["results"]], [self.cycle.pk])
        self.assertEqual(
            set(body["results"][0]),
            {"id", "name", "cycle_type", "start_date", "end_date", "status", "parent_id", "objective_count"},
        )
        self.save_fixture("cycles", body)

    def test_dashboard(self):
        objective = self.approved()
        kr = objective.key_results.order_by("id").first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="100", confidence=5, update_text="Xong")
        body = self.call(self.owner, "get", "dashboard/")
        self.assertEqual(body["progress"], "50.00")
        self.assertEqual(body["cycle"]["id"], self.cycle.pk)
        self.assertEqual(set(body["featured"][0]), OBJECTIVE_KEYS)
        self.save_fixture("dashboard", body)
        stranger = okr_employee(okr_company("OKR No Cycle"), "api_no_cycle")
        self.assertEqual(self.call(stranger, "get", "dashboard/", expect=404)["code"], "no_cycle")

    def test_objective_list_filters_and_visibility(self):
        draft = self.draft(title="Ban nhap rieng")
        approved = self.approved(title="Da duyet chung")
        mine = self.call(self.owner, "get", "objectives/?owner=me")
        self.assertEqual({o["id"] for o in mine["results"]}, {draft.pk, approved.pk})
        self.assertEqual(set(mine["results"][0]), OBJECTIVE_KEYS)
        drafts = self.call(self.owner, "get", "objectives/?owner=me&status=draft")
        self.assertEqual([o["id"] for o in drafts["results"]], [draft.pk])
        self.assertEqual(self.call(self.owner, "get", "objectives/?q=rieng")["count"], 1)
        colleague = {o["id"] for o in self.call(self.colleague, "get", "objectives/")["results"]}
        self.assertEqual(colleague, {self.company_objective.pk, approved.pk})
        self.assertEqual(self.call(self.outsider, "get", "objectives/")["count"], 0)
        parent = next(o for o in self.call(self.admin, "get", "objectives/?scope=company")["results"])
        self.assertEqual(parent["child_count"], 2)
        self.assertEqual(self.call(self.colleague, "get", f"objectives/{self.company_objective.pk}/")["child_count"], 1)
        self.save_fixture("objectives", mine)

    def test_objective_detail(self):
        objective = self.draft()
        body = self.call(self.owner, "get", f"objectives/{objective.pk}/")
        self.assertEqual(set(body), DETAIL_KEYS)
        self.assertEqual(
            body["permissions"], {"can_submit": True, "can_comment": True, "can_approve": False, "can_edit": True}
        )
        self.assertEqual(body["parent"], {"id": self.company_objective.pk, "title": self.company_objective.title})
        self.assertTrue(body["provisional"])
        self.assertIsNone(body["health"])
        self.assertEqual(set(body["key_results"][0]), KEY_RESULT_KEYS)
        self.save_fixture("objective_detail", body)
        # Same company but not visible, and another company: both 404.
        self.call(self.colleague, "get", f"objectives/{objective.pk}/", expect=404)
        self.call(self.outsider, "get", f"objectives/{objective.pk}/", expect=404)

    def test_key_result_detail(self):
        kr = self.approved().key_results.order_by("id").first()
        body = self.call(self.owner, "get", f"key-results/{kr.pk}/")
        self.assertEqual(set(body), KEY_RESULT_KEYS)
        # Approved: progress goes through check-ins, the definition is locked.
        self.assertEqual(body["permissions"], {"can_checkin": True, "can_add_evidence": True, "can_edit": False})
        self.assertEqual(body["health"] is None, False)
        self.assertEqual(
            self.call(self.colleague, "get", f"key-results/{kr.pk}/")["permissions"],
            {"can_checkin": False, "can_add_evidence": False, "can_edit": False},
        )
        self.call(self.outsider, "get", f"key-results/{kr.pk}/", expect=404)
        self.save_fixture("key_result", body)


class WorkflowEndpointTests(APITestBase):
    def test_submit_approve_reject_over_http(self):
        objective = self.draft()
        self.call(self.colleague, "post", f"objectives/{objective.pk}/submit/", expect=403)
        submitted = self.call(self.owner, "post", f"objectives/{objective.pk}/submit/")
        self.assertEqual(submitted["objective"]["approval_status"], "submitted")
        self.assertEqual(self.call(self.owner, "post", f"objectives/{objective.pk}/submit/", expect=409)["code"], "invalid_transition")
        approvals = self.call(self.manager, "get", "approvals/")
        self.assertEqual(approvals["count"], 1)
        item = approvals["results"][0]
        self.assertEqual(set(item), {"objective", "submitted_at", "key_results", "checks", "can_approve", "block_reason"})
        self.assertEqual(item["checks"], {"key_result_count_ok": True, "weight_total": "100.00", "weight_ok": True, "alignment_ok": True})
        self.save_fixture("approvals", approvals)
        self.call(self.owner, "get", "approvals/", expect=403)
        rejected = self.call(self.manager, "post", f"objectives/{objective.pk}/reject/", {"reason": ""}, expect=400)
        self.assertEqual(rejected["code"], "invalid_rejection")
        self.call(self.manager, "post", f"objectives/{objective.pk}/reject/", {"reason": "Bo sung chi so"}, format="json")
        detail = self.call(self.owner, "get", f"objectives/{objective.pk}/")
        self.assertEqual(detail["rejection_reason"], "Bo sung chi so")
        self.call(self.owner, "post", f"objectives/{objective.pk}/submit/")
        self.call(self.outsider, "post", f"objectives/{objective.pk}/approve/", expect=404)
        approved = self.call(self.manager, "post", f"objectives/{objective.pk}/approve/")
        self.assertEqual(approved["objective"]["approval_status"], "approved")

    def test_checkin_create_replay_and_validation(self):
        kr = self.approved().key_results.order_by("id").first()
        payload = {"key_result": kr.pk, "current_value": "62.5", "confidence": 4, "update_text": "Tien trien tot", "client_request_id": "app-1"}
        created = self.call(self.owner, "post", "checkins/", payload, expect=201, format="json")
        self.assertEqual(set(created), {"checkin", "key_result"})
        self.assertEqual(set(created["checkin"]), CHECKIN_KEYS)
        self.assertEqual(created["key_result"]["progress"], "62.50")
        self.assertEqual(created["checkin"]["current_value"], "62.5")
        self.save_fixture("checkin_created", created)
        replay = self.call(self.owner, "post", "checkins/", payload, expect=200, format="json")
        self.assertEqual(replay["checkin"]["id"], created["checkin"]["id"])
        self.assertEqual(OKRCheckin.objects.filter(key_result=kr).count(), 1)
        error = self.call(self.owner, "post", "checkins/", {**payload, "client_request_id": "app-2", "confidence": 9, "update_text": ""}, expect=400, format="json")
        self.assertEqual(set(error["fields"]), {"confidence", "update_text"})
        self.call(self.manager, "post", "checkins/", {**payload, "client_request_id": "m-1"}, expect=403, format="json")
        self.call(self.outsider, "post", "checkins/", {**payload, "client_request_id": "o-1"}, expect=404, format="json")
        page = self.call(self.owner, "get", f"checkins/?key_result={kr.pk}")
        self.assertEqual(page["count"], 1)
        self.call(self.owner, "get", "checkins/", expect=400)
        self.save_fixture("checkins", page)


class EngagementEndpointTests(APITestBase):
    def test_comments_and_activity(self):
        objective = self.approved()
        created = self.call(self.colleague, "post", "comments/", {"objective": objective.pk, "content": "Rat tot"}, expect=201, format="json")
        self.assertEqual(set(created), {"id", "content", "created_at", "author", "parent_id"})
        reply = self.call(self.owner, "post", "comments/", {"objective": objective.pk, "content": "Cam on", "parent": created["id"]}, expect=201, format="json")
        self.assertEqual(reply["parent_id"], created["id"])
        self.call(self.owner, "post", "comments/", {"objective": objective.pk, "content": "x", "parent": reply["id"]}, expect=400, format="json")
        self.call(self.owner, "post", "comments/", {"content": "x"}, expect=400, format="json")
        self.call(self.outsider, "post", "comments/", {"objective": objective.pk, "content": "x"}, expect=404, format="json")
        comments = self.call(self.owner, "get", f"comments/?objective={objective.pk}")
        self.assertEqual(comments["count"], 2)
        self.assertEqual(self.call(self.owner, "get", f"objectives/{objective.pk}/")["comment_count"], 2)
        self.assertTrue(OKRNotification.objects.filter(recipient=self.owner, notification_type="comment").exists())
        activity = self.call(self.owner, "get", f"activity/?objective={objective.pk}")
        self.assertEqual({"submitted", "approved", "commented"} <= {a["action"] for a in activity["results"]}, True)
        self.assertEqual(set(activity["results"][0]), {"id", "action", "created_at", "actor", "summary", "key_result"})
        # The objective's timeline also carries its key results' events.
        kr = objective.key_results.order_by("id").first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="62.5", confidence=4, update_text="Tot")
        feed = self.call(self.owner, "get", f"activity/?objective={objective.pk}")["results"]
        self.assertEqual(feed[0]["action"], "checked_in")
        self.assertEqual(feed[0]["key_result"], {"id": kr.pk, "title": kr.title})
        self.assertEqual(feed[0]["summary"], "Check-in: 0% → 62,5%")
        self.save_fixture("comments", comments)
        self.save_fixture("activity", activity)

    def test_evidence_link_upload_and_authorised_download(self):
        kr = self.approved().key_results.order_by("id").first()
        link = self.call(self.owner, "post", "evidence/", {"key_result": kr.pk, "evidence_type": "url", "url": "https://example.com/bao-cao", "label": "Bao cao"}, expect=201, format="json")
        self.assertEqual(link["url"], "https://example.com/bao-cao")
        self.call(self.owner, "post", "evidence/", {"key_result": kr.pk, "evidence_type": "url", "url": "javascript:alert(1)", "label": "x"}, expect=400, format="json")
        upload = SimpleUploadedFile("anh chup.png", PNG, content_type="image/png")
        stored = self.call(self.owner, "post", "evidence/", {"key_result": kr.pk, "evidence_type": "file", "file": upload}, expect=201, format="multipart")
        self.assertEqual((stored["file_name"], stored["file_size"]), ("anh chup.png", len(PNG)))
        self.assertEqual(stored["download_path"], f"/okr/evidence/{stored['id']}/download/")
        evidence = OKREvidence.objects.get(pk=stored["id"])
        self.assertTrue(str(Path(evidence.file.path)).startswith(os.path.abspath(self.private_root)))
        response = self.client_for(self.colleague).get(f"/api{stored['download_path']}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(b"".join(response.streaming_content), PNG)
        self.assertEqual(self.client_for(self.outsider).get(f"/api{stored['download_path']}").status_code, 404)
        self.assertEqual(APIClient().get(f"/api{stored['download_path']}").status_code, 401)
        fake = SimpleUploadedFile("virus.png", b"MZ" + b"\x00" * 40, content_type="image/png")
        self.call(self.owner, "post", "evidence/", {"key_result": kr.pk, "file": fake}, expect=400, format="multipart")
        exe = SimpleUploadedFile("tool.exe", b"MZ" + b"\x00" * 40)
        self.call(self.owner, "post", "evidence/", {"key_result": kr.pk, "file": exe}, expect=400, format="multipart")
        self.call(self.colleague, "post", "evidence/", {"key_result": kr.pk, "url": "https://example.com", "label": "x"}, expect=403, format="json")
        page = self.call(self.colleague, "get", f"evidence/?key_result={kr.pk}")
        self.assertEqual(page["count"], 2)
        self.save_fixture("evidence", page)

    def test_draft_evidence_is_not_downloadable_by_colleagues(self):
        kr = self.draft().key_results.order_by("id").first()
        upload = SimpleUploadedFile("bang.pdf", b"%PDF-1.7\n" + b"0" * 32, content_type="application/pdf")
        stored = self.call(self.owner, "post", "evidence/", {"key_result": kr.pk, "file": upload}, expect=201, format="multipart")
        self.assertEqual(self.client_for(self.colleague).get(f"/api{stored['download_path']}").status_code, 404)
        self.assertEqual(self.client_for(self.manager).get(f"/api{stored['download_path']}").status_code, 200)


class ManagerAndNotificationEndpointTests(APITestBase):
    def test_team_report(self):
        objective = self.approved()
        kr = objective.key_results.order_by("id").first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="10", confidence=2, update_text="Cham")
        body = self.call(self.manager, "get", "reports/team/")
        self.assertEqual(body["member_count"], 1)
        self.assertEqual(body["members"][0]["employee"]["id"], self.owner.pk)
        self.assertEqual(set(body), {"member_count", "on_track", "at_risk", "off_track", "pending_approval_count", "expected_progress", "attention", "members"})
        for item in body["attention"]:
            self.assertEqual(set(item), ATTENTION_KEYS | {"owner"})
        self.save_fixture("team_report", body)
        self.call(self.owner, "get", "reports/team/", expect=403)

    def test_notifications_list_read_and_isolation(self):
        objective = self.draft()
        self.call(self.owner, "post", f"objectives/{objective.pk}/submit/")
        page = self.call(self.manager, "get", "notifications/")
        self.assertEqual((page["count"], page["unread_count"]), (1, 1))
        item = page["results"][0]
        self.assertEqual(set(item), {"id", "type", "title", "message", "is_read", "created_at", "reference", "actor"})
        self.assertEqual(item["reference"], {"type": "objective", "id": objective.pk, "available": True})
        # F7: the submitter is named.
        self.assertEqual(item["actor"], {"id": self.owner.pk, "name": self.owner.get_full_name()})
        self.save_fixture("notifications", page)
        self.call(self.colleague, "post", f"notifications/{item['id']}/read/", expect=404)
        self.call(self.outsider, "post", f"notifications/{item['id']}/read/", expect=404)
        self.call(self.manager, "post", f"notifications/{item['id']}/read/")
        self.assertTrue(OKRNotification.objects.get(pk=item["id"]).is_read)
        self.assertEqual(self.call(self.manager, "get", "notifications/")["unread_count"], 0)
        self.call(self.admin, "post", "notifications/read-all/")
        self.assertFalse(OKRNotification.objects.filter(recipient=self.admin, is_read=False).exists())
        self.assertEqual(self.call(self.outsider, "get", "notifications/")["count"], 0)

    def test_listing_notifications_restores_missing_approval_requests(self):
        objective = self.draft()
        okrs.submit_objective(as_viewer(self.owner), objective.pk)
        OKRNotification.objects.filter(recipient=self.manager).delete()
        page = self.call(self.manager, "get", "notifications/")
        self.assertEqual([n["reference"]["id"] for n in page["results"]], [objective.pk])
        self.assertEqual(self.call(self.manager, "get", "notifications/")["count"], 1)

    def test_decimal_strings_and_pagination(self):
        for index in range(3):
            self.draft(title=f"Muc tieu {index}")
        body = self.call(self.owner, "get", "objectives/?owner=me&page_size=2")
        self.assertEqual((body["count"], len(body["results"])), (3, 2))
        self.assertIsNotNone(body["next"])
        self.assertIsInstance(body["results"][0]["progress"], str)
        self.assertEqual(Decimal(body["results"][0]["weight"]), Decimal("100.00"))
        self.assertEqual(body["results"][0]["approval_status"], ApprovalStatus.DRAFT)
