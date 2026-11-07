"""M2: JOYOKR (WordPress) → Django import, on fake data shaped exactly like
the ``joyokr_*`` tables (schema of theme v1.6.45).

Covers: proposals never verify, company scoping, dry run keeps nothing,
unverified users block, the full tree + history + files, idempotent
re-run and delta, roles only on request and never admin, API visibility
and company isolation, rollback (and its refusal), the read-only
inventory, and the exporter never touching passwords.
"""

import copy
import hashlib
import io
import json
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError

from okr.legacy_import.export import ExportError, load_export
from okr.legacy_import.importer import ImportBlocked, ImportRun
from okr.legacy_import.mapping import load_mapping, propose
from okr.legacy_import.rollback import RollbackRefused, rollback_batch
from okr.models import (
    OKRActivity,
    OKRCheckin,
    OKRComment,
    OKRCycle,
    OKRDepartmentRole,
    OKREvidence,
    OKRIdentityMap,
    OKRKeyResult,
    OKRMigrationBatch,
    OKRNotification,
    OKRObjective,
    OKRRole,
    OKRSettings,
    OKRTeam,
    OKRTeamMember,
)
from okr.models.choices import ApprovalStatus, BatchStatus
from okr.services import okrs
from okr.tests.test_api import APITestBase
from okr.tests.test_services import as_viewer

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 120
EXPORTER = Path(__file__).resolve().parents[1] / "legacy_import" / "joyokr_export.php"

LEGACY_TABLES = (OKRCycle, OKRTeam, OKRObjective, OKRKeyResult, OKRCheckin, OKRComment, OKREvidence,
                 OKRNotification, OKRActivity)


# Rows of build_export() that are deliberately broken (source ERRORs): kept out
# of the clean fixture, used by the validation tests.
BROKEN_ROWS = {"key_results": {"54"}, "checkins": {"63"}, "evidence": {"83", "84", "85"}}


def without_broken_rows(data):
    data = copy.deepcopy(data)
    for table, ids in BROKEN_ROWS.items():
        data["tables"][table] = [row for row in data["tables"][table] if row["id"] not in ids]
    data["attachments"] = [item for item in data["attachments"] if item["id"] == 501]
    return data


def legacy_count():
    return sum(model._base_manager.filter(legacy_wp_id__isnull=False).count() for model in LEGACY_TABLES)


class WordPressImportBase(APITestBase):
    def setUp(self):
        super().setUp()
        self.owner.badge_id = "NV-OWN"
        self.owner.save()
        self.tmp = tempfile.mkdtemp(prefix="okr_wp_import_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.uploads = Path(self.tmp) / "uploads"
        (self.uploads / "2026" / "10").mkdir(parents=True)
        (self.uploads / "2026" / "10" / "bao-cao.png").write_bytes(PNG)
        (self.uploads / "2026" / "10" / "sai.png").write_bytes(PNG + b"x")
        self.dirty = self.build_export()
        self.data = without_broken_rows(self.dirty)

    # -- fake JOYOKR export -------------------------------------------------------------

    def build_export(self):
        t = "2026-10-01 02:00:00"

        def objective(i, **kw):
            row = {"id": str(i), "cycle_id": "22", "parent_objective_id": None, "scope": "personal",
                   "department_id": None, "team_id": None, "owner_user_id": "11", "title": f"Muc tieu {i}",
                   "description": "", "weight": "100.00", "priority": "medium", "approval_status": "approved",
                   "progress": "0.00", "deleted_at": None, "created_at": t, "updated_at": t}
            row.update(kw)
            return row

        def key_result(i, objective_id, **kw):
            row = {"id": str(i), "objective_id": str(objective_id), "owner_user_id": "11", "title": f"KR {i}",
                   "description": "", "unit": "%", "start_value": "0.0000", "current_value": "0.0000",
                   "target_value": "100.0000", "weight": "50.00", "priority": "medium", "due_date": "2026-12-31",
                   "approval_status": "approved", "progress": "0.00", "deleted_at": None,
                   "created_at": t, "updated_at": t}
            row.update(kw)
            return row

        return {
            "format": "joyokr-export",
            "format_version": 1,
            "source": {"site_url": "https://okr.example.test/", "theme_version": "1.6.45",
                       "schema_version": "12", "table_prefix": "wp_", "exported_at": "2026-11-06T03:00:00Z"},
            "options": {"warning_threshold": "20", "reminder_frequency": "biweekly", "email_reminders": "0",
                        "rating_ranges": json.dumps([{"label": "Tot", "min": 70, "max": 100}])},
            "users": [
                {"id": 11, "login": "wp_owner", "email": self.owner.email, "display_name": "Owner",
                 "roles": ["joyokr_employee"], "employee_code": "NV-OWN"},
                {"id": 12, "login": "wp_manager", "email": self.manager.email.upper(), "display_name": "Manager",
                 "roles": ["joyokr_manager"], "employee_code": ""},
                {"id": 13, "login": "wp_admin", "email": self.admin.email, "display_name": "Admin",
                 "roles": ["administrator"], "employee_code": ""},
                {"id": 14, "login": "wp_stranger", "email": "khach.wp@example.com", "display_name": "Khach",
                 "roles": ["joyokr_employee"], "employee_code": "NV-NONE"},
                {"id": 15, "login": "wp_other_company", "email": self.outsider.email, "display_name": "Ngoai",
                 "roles": ["joyokr_employee"], "employee_code": ""},
            ],
            "tables": {
                "departments": [
                    {"id": "3", "code": "KT", "name": " phong  ky thuat ", "manager_user_id": "12", "status": "active",
                     "created_at": t, "updated_at": t},
                    {"id": "4", "code": "XX", "name": "Phong Khong Ton Tai", "manager_user_id": None,
                     "status": "active", "created_at": t, "updated_at": t},
                ],
                "teams": [{"id": "7", "department_id": "3", "name": "Team Bac", "description": "", "manager_user_id": "12",
                           "status": "active", "created_at": t, "updated_at": t}],
                "team_members": [{"team_id": "7", "user_id": "11", "joined_at": t},
                                 {"team_id": "7", "user_id": "14", "joined_at": t}],
                "cycles": [
                    {"id": "21", "parent_id": None, "name": "Nam 2026", "cycle_type": "year", "start_date": "2026-01-01",
                     "end_date": "2026-12-31", "status": "active", "created_at": t, "updated_at": t},
                    {"id": "22", "parent_id": "21", "name": "Q4 2026 WP", "cycle_type": "quarter",
                     "start_date": "2026-10-01", "end_date": "2026-12-31", "status": "active",
                     "created_at": t, "updated_at": t},
                ],
                "objectives": [
                    objective(31, scope="company", owner_user_id="13", progress="62.50", title="Cong ty WP"),
                    objective(32, scope="department", department_id="3", parent_objective_id="31",
                              owner_user_id="12", progress="62.50", title="Phong ban WP"),
                    objective(33, parent_objective_id="32", progress="62.50", title="Ca nhan WP",
                              created_at="2026-10-02 01:02:03"),
                    objective(34, approval_status="rejected", title="Can sua WP"),
                    objective(35, approval_status="approved", deleted_at="2026-10-05 00:00:00", title="Da xoa WP"),
                    objective(36, scope="team", team_id="7", owner_user_id="12", approval_status="submitted",
                              title="Team WP"),
                ],
                "key_results": [
                    key_result(51, 33, current_value="75.0000", progress="75.00"),
                    key_result(52, 33, start_value="10.0000", target_value="20.0000", current_value="15.0000",
                               progress="50.00"),
                    key_result(53, 34, approval_status="draft"),
                    key_result(54, 999),
                    key_result(55, 33, weight="0.00", deleted_at="2026-10-04 00:00:00"),
                    # JOYOKR only submits/approves an objective with 2-4 key results.
                    key_result(41, 31, current_value="62.5000", progress="62.50"),
                    key_result(42, 31, current_value="62.5000", progress="62.50"),
                    key_result(43, 32, owner_user_id="12", current_value="62.5000", progress="62.50"),
                    key_result(44, 32, owner_user_id="12", current_value="62.5000", progress="62.50"),
                    key_result(45, 36, owner_user_id="12", approval_status="submitted"),
                    key_result(46, 36, owner_user_id="12", approval_status="submitted"),
                ],
                "checkins": [
                    {"id": "61", "key_result_id": "51", "user_id": "11", "previous_value": "0.0000",
                     "current_value": "50.0000", "previous_progress": "0.00", "current_progress": "50.00",
                     "confidence": "4", "update_text": "Lan 1", "blockers": "", "next_steps": "",
                     "created_at": "2026-10-10 03:00:00"},
                    {"id": "62", "key_result_id": "51", "user_id": "11", "previous_value": "50.0000",
                     "current_value": "75.0000", "previous_progress": "50.00", "current_progress": "75.00",
                     "confidence": "3", "update_text": "Lan 2", "blockers": "Cham", "next_steps": "Tiep",
                     "created_at": "2026-10-20 03:00:00"},
                    {"id": "63", "key_result_id": "54", "user_id": "11", "previous_value": "0", "current_value": "1",
                     "previous_progress": "0", "current_progress": "1", "confidence": "3", "update_text": "x",
                     "created_at": t},
                ],
                "comments": [
                    {"id": "71", "object_type": "objective", "object_id": "33", "user_id": "12", "parent_id": None,
                     "content": "Xem lai KR 2", "created_at": t, "updated_at": t},
                    {"id": "72", "object_type": "objective", "object_id": "33", "user_id": "11", "parent_id": "71",
                     "content": "Da sua", "created_at": t, "updated_at": t},
                    {"id": "73", "object_type": "key_result", "object_id": "51", "user_id": "99", "parent_id": None,
                     "content": "Nguoi da nghi", "created_at": t, "updated_at": t},
                ],
                "evidence": [
                    {"id": "81", "key_result_id": "51", "checkin_id": "62", "user_id": "11", "evidence_type": "file",
                     "attachment_id": "501", "url": "https://okr.example.test/wp-content/uploads/2026/10/bao-cao.png",
                     "label": "Bao cao", "created_at": t},
                    {"id": "82", "key_result_id": "51", "checkin_id": None, "user_id": "11", "evidence_type": "url",
                     "attachment_id": None, "url": "https://docs.example.com/a", "label": "Lien ket",
                     "created_at": t},
                    {"id": "83", "key_result_id": "51", "checkin_id": None, "user_id": "11", "evidence_type": "file",
                     "attachment_id": "502", "url": "", "label": "Mat tep", "created_at": t},
                    {"id": "84", "key_result_id": "51", "checkin_id": None, "user_id": "11", "evidence_type": "file",
                     "attachment_id": "503", "url": "", "label": "Sai checksum", "created_at": t},
                    {"id": "85", "key_result_id": "51", "checkin_id": None, "user_id": "11", "evidence_type": "url",
                     "attachment_id": None, "url": "javascript:alert(1)", "label": "Xau", "created_at": t},
                ],
                "notifications": [
                    {"id": "91", "user_id": "11", "type": "approved", "title": "Ca nhan WP", "message": "",
                     "reference_type": "objective", "reference_id": "33", "dedupe_key": "approved:objective:33:user:11",
                     "is_read": "0", "created_at": t},
                    {"id": "92", "user_id": "14", "type": "comment", "title": "x", "message": "",
                     "reference_type": "objective", "reference_id": "33", "dedupe_key": None, "is_read": "0",
                     "created_at": t},
                    {"id": "93", "user_id": "12", "type": "approval_requested", "title": "Mat", "message": "",
                     "reference_type": "objective", "reference_id": "999", "dedupe_key": None, "is_read": "1",
                     "created_at": t},
                ],
                "activity_log": [
                    {"id": "101", "actor_user_id": "12", "action": "approved", "object_type": "objective",
                     "object_id": "33", "metadata": "{}", "created_at": t},
                    {"id": "102", "actor_user_id": "12", "action": "rejected", "object_type": "objective",
                     "object_id": "34", "metadata": json.dumps({"reason": "Bo sung chi so"}),
                     "created_at": "2026-10-03 00:00:00"},
                    {"id": "103", "actor_user_id": "11", "action": "checked_in", "object_type": "key_result",
                     "object_id": "51", "metadata": None, "created_at": t},
                    {"id": "104", "actor_user_id": "11", "action": "la_hanh_dong", "object_type": "objective",
                     "object_id": "33", "metadata": "khong phai json", "created_at": t},
                    {"id": "105", "actor_user_id": "13", "action": "cycle_status_changed", "object_type": "cycle",
                     "object_id": "22", "metadata": "{}", "created_at": t},
                ],
            },
            "attachments": [
                {"id": 501, "relative_path": "2026/10/bao-cao.png", "mime_type": "image/png",
                 "original_name": "bao-cao.png", "size": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()},
                {"id": 502, "relative_path": "2026/10/khong-co.png", "mime_type": "image/png",
                 "original_name": "khong-co.png", "size": 10, "sha256": ""},
                {"id": 503, "relative_path": "2026/10/sai.png", "mime_type": "image/png",
                 "original_name": "sai.png", "size": 1, "sha256": hashlib.sha256(PNG).hexdigest()},
            ],
        }

    def write_export(self, data=None, name="export.json"):
        path = Path(self.tmp) / name
        path.write_text(json.dumps(data or self.data, ensure_ascii=False), encoding="utf-8")
        return path

    def verified_mapping(self, export_path, *, reject=(14, 15)):
        mapping = propose(load_export(export_path), self.company.pk)
        for row in mapping["users"]:
            if row["wp_user_id"] in (11, 12, 13):
                row["status"] = "verified"
            elif row["wp_user_id"] in reject:
                row["status"] = "rejected"
        for row in mapping["departments"]:
            row["status"] = "verified" if row["wp_department_id"] == 3 else "rejected"
        path = Path(self.tmp) / "mapping.json"
        path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return path

    def run_import(self, *, apply=True, data=None, **kwargs):
        export_path = self.write_export(data)
        mapping_path = kwargs.pop("mapping_path", None) or self.verified_mapping(export_path)
        run = ImportRun(
            load_export(export_path),
            load_mapping(mapping_path, self.company.pk),
            self.company,
            operator=self.admin.employee_user_id if apply else None,
            uploads_dir=self.uploads,
            apply=apply,
            **kwargs,
        )
        return run.run()


class ProposalTests(WordPressImportBase):
    def test_proposals_never_verify_and_stay_inside_the_company(self):
        mapping = propose(load_export(self.write_export()), self.company.pk)
        users = {row["wp_user_id"]: row for row in mapping["users"]}
        self.assertEqual((users[11]["employee_id"], users[11]["matched_by"]), (self.owner.pk, "email_and_badge"))
        self.assertEqual((users[12]["employee_id"], users[12]["matched_by"]), (self.manager.pk, "email"))
        self.assertIn("thủ công", users[12]["notes"])
        self.assertEqual(users[14]["status"], "unmatched")
        self.assertEqual(users[15]["status"], "unmatched", "an employee of another company is never proposed")
        self.assertEqual({row["status"] for row in mapping["users"]}, {"proposed", "unmatched"})
        departments = {row["wp_department_id"]: row for row in mapping["departments"]}
        self.assertEqual((departments[3]["hr_department_id"], departments[3]["status"]),
                         (self.department.pk, "proposed"))
        self.assertEqual(departments[4]["status"], "unmatched")

    def test_badge_and_email_pointing_at_different_people_is_unmatched(self):
        self.colleague.badge_id = "NV-MIX"
        self.colleague.save()
        self.data["users"][0]["employee_code"] = "NV-MIX"
        users = {row["wp_user_id"]: row for row in propose(load_export(self.write_export()), self.company.pk)["users"]}
        self.assertEqual((users[11]["status"], users[11]["employee_id"]), ("unmatched", None))


class ExportFileTests(WordPressImportBase):
    def test_bad_files_are_refused(self):
        for broken in ({"format": "other"}, {**self.data, "format_version": 2}, {**self.data, "tables": []}):
            with self.assertRaises(ExportError):
                load_export(self.write_export(broken, "bad.json"))
        leaked = copy.deepcopy(self.data)
        leaked["users"][0]["user_pass"] = "$P$hash"
        with self.assertRaisesRegex(ExportError, "credential"):
            load_export(self.write_export(leaked, "leaked.json"))

    def test_exporter_never_selects_passwords_or_secrets(self):
        source = EXPORTER.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.strip().startswith(("*", "/**", "//")))
        for secret in ("user_pass", "user_activation_key", "session_tokens", "telegram_bot_token",
                       "telegram_chat_id", "joyokr_phone", "sso_secret"):
            self.assertNotIn(secret, code)
        self.assertIsNone(re.search(r"SELECT\s+\*\s+FROM\s+\{\$wpdb->users\}", code))
        self.assertIn("SELECT ID, user_login, user_email, display_name, user_registered", code)
        for verb in ("INSERT", "UPDATE ", "DELETE", "DROP", "ALTER", "update_option", "update_user_meta"):
            self.assertNotIn(verb, code)


class DryRunAndBlockingTests(WordPressImportBase):
    def test_dry_run_keeps_nothing_and_reports_what_would_happen(self):
        before = (legacy_count(), OKRMigrationBatch.objects.count(), OKRIdentityMap.objects.count())
        report = self.run_import(apply=False)
        self.assertEqual(report["mode"], "dry_run")
        self.assertIsNone(report["batch_id"])
        self.assertEqual(report["stats"]["objectives"]["created"], 6)
        self.assertEqual((legacy_count(), OKRMigrationBatch.objects.count(), OKRIdentityMap.objects.count()), before)
        self.assertEqual(list(Path(self.private_root).rglob("*.png")), [], "a dry run writes no file")

    def test_an_undecided_user_blocks_and_nothing_is_written(self):
        export_path = self.write_export()
        mapping_path = self.verified_mapping(export_path, reject=(15,))  # 14 stays unmatched
        with self.assertRaises(ImportBlocked) as caught:
            self.run_import(mapping_path=mapping_path)
        blocked = caught.exception.report["reconciliation"]["preflight"]["unverified_users"]
        self.assertEqual([row["wp_user_id"] for row in blocked], [14])
        self.assertEqual(legacy_count(), 0)
        report = self.run_import(mapping_path=mapping_path, allow_unmapped=True)
        self.assertEqual(report["stats"]["team_members"]["created"], 1)

    def test_mapping_for_another_company_is_refused(self):
        path = self.verified_mapping(self.write_export())
        with self.assertRaises(ExportError):
            load_mapping(path, self.other_company.pk)


class ApplyTests(WordPressImportBase):
    def test_full_import_keeps_tree_history_files_and_native_rows(self):
        native = self.draft(title="Tao tren Django")
        report = self.run_import()
        self.assertIsNotNone(report["batch_id"])
        get = lambda model, wp_id: model._base_manager.get(legacy_wp_id=wp_id)

        q4, year = get(OKRCycle, 22), get(OKRCycle, 21)
        self.assertEqual((q4.parent_id, q4.company_id, q4.cycle_type), (year.pk, self.company.pk, "quarter"))
        company_okr, dept_okr, personal = get(OKRObjective, 31), get(OKRObjective, 32), get(OKRObjective, 33)
        self.assertEqual((dept_okr.parent_id, personal.parent_id), (company_okr.pk, dept_okr.pk))
        self.assertEqual((dept_okr.department_id, dept_okr.owner_id), (self.department.pk, self.manager.pk))
        self.assertEqual((personal.owner_id, personal.progress), (self.owner.pk, Decimal("62.50")))
        self.assertEqual(personal.created_at.isoformat(), "2026-10-02T01:02:03+00:00", "UTC timestamps kept")
        deleted = get(OKRObjective, 35)
        self.assertEqual((deleted.approval_status, deleted.deleted_at is not None), (ApprovalStatus.DELETED, True))
        team_okr = get(OKRObjective, 36)
        self.assertEqual((team_okr.team_id, team_okr.approval_status), (get(OKRTeam, 7).pk, "submitted"))
        rejected = get(OKRObjective, 34)
        self.assertEqual(okrs.rejection_reason(rejected), "Bo sung chi so", "rejection reason comes from history")

        kr = get(OKRKeyResult, 51)
        self.assertEqual((kr.objective_id, kr.current_value, kr.progress), (personal.pk, Decimal("75"), Decimal("75")))
        checkins = list(OKRCheckin._base_manager.filter(key_result=kr).order_by("created_at"))
        self.assertEqual([c.legacy_wp_id for c in checkins], [61, 62])
        self.assertEqual((checkins[1].blockers, checkins[1].author_id), ("Cham", self.owner.pk))

        reply = get(OKRComment, 72)
        self.assertEqual(reply.parent_id, get(OKRComment, 71).pk)
        self.assertIsNone(get(OKRComment, 73).author_id, "a deleted WordPress user leaves an empty author")

        file_evidence = get(OKREvidence, 81)
        self.assertEqual((file_evidence.checkin_id, file_evidence.sha256), (checkins[1].pk,
                                                                             hashlib.sha256(PNG).hexdigest()))
        with file_evidence.file.open("rb") as handle:
            self.assertEqual(handle.read(), PNG)
        self.assertTrue(Path(file_evidence.file.path).is_relative_to(Path(self.private_root).resolve()))
        self.assertEqual(get(OKREvidence, 82).url, "https://docs.example.com/a")
        self.assertEqual(report["validation"]["counts"]["error"], 0)
        self.assertEqual([i for i in report["issues"] if i["severity"] == "error"], [])
        warned = {(f["code"], f["table"], f["wp_id"]) for f in report["validation"]["findings"]}
        self.assertIn(("user_deleted_in_wordpress", "comments", 73), warned)

        notification = get(OKRNotification, 91)
        self.assertEqual((notification.recipient_id, notification.reference_id, notification.dedupe_key),
                         (self.owner.pk, personal.pk, "wp:approved:objective:33:user:11"))
        self.assertFalse(OKRNotification._base_manager.filter(legacy_wp_id=92).exists())
        self.assertEqual(get(OKRNotification, 93).reference_id, None)
        odd = get(OKRActivity, 104)
        self.assertEqual((odd.action, odd.metadata["wp_action"], odd.metadata["raw"]),
                         ("updated", "la_hanh_dong", "khong phai json"))
        self.assertEqual(get(OKRActivity, 105).cycle_id, q4.pk)

        self.assertEqual(OKRTeamMember.objects.filter(team=get(OKRTeam, 7)).count(), 1)
        identity = OKRIdentityMap.objects.get(wp_user_id=11)
        self.assertEqual((identity.status, identity.employee_id, identity.verified_by_id),
                         ("verified", self.owner.pk, self.admin.employee_user_id.pk))
        native.refresh_from_db()
        self.assertIsNone(native.legacy_wp_id)
        self.assertEqual(report["reconciliation"]["native_rows_untouched"]["objectives"], 2)
        self.assertEqual(report["reconciliation"]["imported_legacy_rows"]["objectives"], 6)
        self.assertFalse(OKRSettings.objects.filter(company=self.company).exists(), "settings only on request")
        batch = OKRMigrationBatch.objects.get(pk=report["batch_id"])
        self.assertEqual((batch.status, batch.started_by_id), (BatchStatus.SUCCEEDED, self.admin.employee_user_id.pk))

    def test_progress_mismatches_are_reported_not_hidden(self):
        self.data["tables"]["key_results"][0]["progress"] = "70.00"  # WordPress says 70, values say 75
        report = self.run_import()
        mismatches = report["reconciliation"]["key_result_progress_mismatches"]
        self.assertEqual([(m["wp_id"], m["wp_progress"], m["computed"]) for m in mismatches], [(51, "70.00", "75.00")])
        self.assertEqual(OKRKeyResult._base_manager.get(legacy_wp_id=51).progress, Decimal("70.00"))

    def test_second_run_creates_nothing_and_a_delta_only_adds_what_changed(self):
        self.run_import()
        counts = legacy_count()
        again = self.run_import()
        created = {table: c.get("created", 0) for table, c in again["stats"].items() if table != "identity_map"}
        self.assertEqual(sum(created.values()), 0, created)
        self.assertEqual(sum(c.get("updated", 0) for t, c in again["stats"].items() if t != "identity_map"), 0)
        self.assertEqual(legacy_count(), counts)

        delta = copy.deepcopy(self.data)
        delta["tables"]["key_results"][0].update(current_value="90.0000", progress="90.00")
        delta["tables"]["checkins"].append(
            {"id": "64", "key_result_id": "51", "user_id": "11", "previous_value": "75", "current_value": "90",
             "previous_progress": "75", "current_progress": "90", "confidence": "5", "update_text": "Lan 3",
             "created_at": "2026-11-01 03:00:00"})
        delta["tables"]["notifications"][0]["is_read"] = "1"
        report = self.run_import(data=delta)
        self.assertEqual(report["stats"]["key_results"]["updated"], 1)
        self.assertEqual(report["stats"]["checkins"]["created"], 1)
        self.assertEqual(report["stats"]["notifications"]["updated"], 1)
        self.assertEqual(OKRKeyResult._base_manager.get(legacy_wp_id=51).current_value, Decimal("90"))
        self.assertTrue(OKRNotification._base_manager.get(legacy_wp_id=91).is_read)

    def test_roles_only_on_request_and_admin_never_granted(self):
        # The WordPress administrator is matched to a plain employee here.
        self.data["users"][2]["email"] = self.colleague.email
        report = self.run_import()
        self.assertFalse(OKRDepartmentRole.objects.filter(company=self.company).exists(), "roles only on request")
        self.assertEqual([(c["wp_user_id"], c["employee_id"]) for c in report["admin_candidates"]],
                         [(13, self.colleague.pk)])
        roles_before = set(OKRRole.objects.values_list("employee_id", "role"))
        report = self.run_import(import_roles=True, import_settings=True)
        self.assertEqual([(c["wp_user_id"], c["employee_id"]) for c in report["admin_candidates"]],
                         [(13, self.colleague.pk)])
        self.assertFalse(OKRRole.objects.filter(employee=self.colleague).exists(), "admin is never granted")
        self.assertEqual(set(OKRRole.objects.values_list("employee_id", "role")) - roles_before, set())
        department_role = OKRDepartmentRole.objects.get(company=self.company, department=self.department)
        self.assertEqual((department_role.manager_id, department_role.legacy_wp_code), (self.manager.pk, "KT"))
        settings = OKRSettings.objects.get(company=self.company)
        self.assertEqual((settings.warning_threshold, settings.reminder_frequency, settings.email_reminders),
                         (Decimal("20"), "biweekly", False))

    def test_new_wordpress_manager_role_is_granted_with_an_audit_event(self):
        OKRRole.objects.filter(employee=self.manager).delete()
        self.run_import(import_roles=True)
        role = OKRRole.objects.get(employee=self.manager, company=self.company)
        self.assertEqual((role.role, role.granted_by_id), ("manager", self.admin.employee_user_id.pk))
        events = set(role.employee.okr_role_events.values_list("role", "action"))
        self.assertEqual(events, {("manager", "granted"), ("department_manager", "granted")})


class ImportedDataThroughTheApiTests(WordPressImportBase):
    def test_owner_sees_imported_objectives_and_another_company_sees_none(self):
        self.run_import()
        personal = OKRObjective._base_manager.get(legacy_wp_id=33)
        mine = self.call(self.owner, "get", "objectives/?owner=me")
        self.assertIn(personal.pk, {row["id"] for row in mine["results"]})
        detail = self.call(self.owner, "get", f"objectives/{personal.pk}/")
        self.assertEqual(detail["progress"], "62.50")
        history = self.call(self.colleague, "get", f"checkins/?key_result={OKRKeyResult._base_manager.get(legacy_wp_id=51).pk}")
        self.assertEqual(history["count"], 2)
        self.call(self.outsider, "get", f"objectives/{personal.pk}/", expect=404)
        self.assertEqual(self.call(self.outsider, "get", "objectives/")["count"], 0)
        evidence = OKREvidence._base_manager.get(legacy_wp_id=81)
        response = self.client_for(self.colleague).get(f"/api/okr/evidence/{evidence.pk}/download/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client_for(self.outsider).get(f"/api/okr/evidence/{evidence.pk}/download/").status_code, 404)

    def test_imported_key_result_takes_a_new_checkin_in_django(self):
        self.run_import()
        kr = OKRKeyResult._base_manager.get(legacy_wp_id=51)
        result = okrs.checkin(as_viewer(self.owner), kr.pk, current_value="80", confidence=4, update_text="Moi")
        self.assertIsNone(result["checkin"].legacy_wp_id)
        self.assertEqual(OKRCheckin._base_manager.filter(key_result=kr).count(), 3)


class RollbackTests(WordPressImportBase):
    def test_rollback_removes_exactly_the_batch_and_its_files(self):
        native = self.draft(title="Giu nguyen")
        report = self.run_import()
        file_path = Path(OKREvidence._base_manager.get(legacy_wp_id=81).file.path)
        self.assertTrue(file_path.exists())
        dry = rollback_batch(report["batch_id"], apply=False)
        self.assertEqual(dry["mode"], "dry_run")
        self.assertGreater(legacy_count(), 0, "a dry rollback deletes nothing")
        done = rollback_batch(report["batch_id"], apply=True)
        self.assertGreater(done["deleted"]["objectives"], 0)
        self.assertEqual(legacy_count(), 0)
        self.assertFalse(file_path.exists())
        self.assertTrue(OKRObjective._base_manager.filter(pk=native.pk).exists())
        self.assertEqual(OKRMigrationBatch.objects.get(pk=report["batch_id"]).status, BatchStatus.ROLLED_BACK)
        with self.assertRaises(RollbackRefused):
            rollback_batch(report["batch_id"], apply=True)

    def test_rollback_is_refused_when_newer_data_depends_on_the_batch(self):
        report = self.run_import()
        kr = OKRKeyResult._base_manager.get(legacy_wp_id=51)
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="80", confidence=4, update_text="Moi")
        with self.assertRaises(RollbackRefused):
            rollback_batch(report["batch_id"], apply=True)
        self.assertTrue(OKRKeyResult._base_manager.filter(pk=kr.pk).exists(), "nothing half undone")


class CommandLineTests(WordPressImportBase):
    def test_propose_dry_run_apply_and_inventory(self):
        export_path = self.write_export()
        mapping_path = Path(self.tmp) / "proposed.json"
        out = io.StringIO()
        call_command("okr_import_wp", "propose", str(export_path), company=self.company.pk,
                     out=str(mapping_path), stdout=out)
        self.assertIn("Nothing is verified", out.getvalue())
        with self.assertRaises(CommandError):  # proposals are not verified: blocked
            call_command("okr_import_wp", "import", str(export_path), company=self.company.pk,
                         mapping=str(mapping_path), stdout=io.StringIO())
        verified = self.verified_mapping(export_path)
        report_path = Path(self.tmp) / "report.json"
        out = io.StringIO()
        call_command("okr_import_wp", "import", str(export_path), company=self.company.pk, mapping=str(verified),
                     uploads_dir=str(self.uploads), report=str(report_path), stdout=out)
        self.assertIn("Dry run: nothing was kept.", out.getvalue())
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["mode"], "dry_run")
        with self.assertRaisesRegex(CommandError, "operator"):
            call_command("okr_import_wp", "import", str(export_path), company=self.company.pk,
                         mapping=str(verified), apply=True, stdout=io.StringIO())
        call_command("okr_import_wp", "import", str(export_path), company=self.company.pk, mapping=str(verified),
                     uploads_dir=str(self.uploads), apply=True,
                     operator=self.admin.employee_user_id.username, stdout=io.StringIO())
        out = io.StringIO()
        call_command("okr_inventory", "--json", stdout=out)
        inventory = json.loads(out.getvalue())
        company = inventory["companies"][str(self.company.pk)]
        self.assertEqual(company["objectives"]["from_wordpress"], 6)
        self.assertEqual(company["objectives"]["created_in_django"], 1)
        self.assertEqual(inventory["identity_map"].get("verified"), 3)


class InspectAndReviewTableTests(WordPressImportBase):
    def test_inspect_reports_counts_links_history_and_files(self):
        from okr.legacy_import.inspect import inspect_export

        result = inspect_export(load_export(self.write_export(self.dirty)), self.uploads)
        self.assertEqual(result["counts"]["objectives"], 6)
        self.assertEqual(len(result["tables_present"]), 11)
        problems = result["problems"]
        self.assertEqual(problems["key_results.objective_id: objective missing"], ["54"])
        self.assertEqual(problems["comments.user_id: user missing from export"], ["73"])
        self.assertEqual(problems["evidence: link is not http(s)"], ["85"])
        files = result["evidence"]["files"]
        self.assertEqual((files["ok"], files["missing_on_disk"], files["checksum_mismatch"]), (1, [83], [84]))
        self.assertEqual(result["approval_history"]["objective:rejected"], 1)
        self.assertEqual(result["objectives"]["deleted"], 1)
        self.assertEqual(result["objectives"]["kr_count_not_2_to_4"], [])
        self.assertNotIn(33, result["objectives"]["weight_total_not_100"])
        self.assertEqual(result["timezone"]["unreadable_timestamps"], {})
        self.assertNotIn(self.owner.email, json.dumps(result), "the inspection report holds no e-mail")

    def test_csv_review_table_round_trip(self):
        import csv

        export_path = self.write_export()
        table = Path(self.tmp) / "review.csv"
        call_command("okr_import_wp", "propose", str(export_path), company=self.company.pk, out=str(table),
                     stdout=io.StringIO())
        with open(table, encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        decisions = {("user", "11"): "verified", ("user", "12"): "verified", ("user", "13"): "verified",
                     ("user", "14"): "rejected", ("department", "3"): "verified", ("department", "4"): "rejected"}
        for row in rows:
            row["status"] = decisions.get((row["kind"], row["wp_id"]), row["status"])
        with open(table, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        mapping = load_mapping(table, self.company.pk)
        self.assertEqual(set(mapping.employees), {11, 12, 13})
        self.assertEqual(set(mapping.departments), {3})
        out = io.StringIO()
        call_command("okr_import_wp", "import", str(export_path), company=self.company.pk, mapping=str(table),
                     uploads_dir=str(self.uploads), stdout=out)
        self.assertIn("Dry run", out.getvalue())
        out = io.StringIO()
        call_command("okr_import_wp", "inspect", str(export_path), uploads_dir=str(self.uploads), stdout=out)
        self.assertIn("objectives=6", out.getvalue())
