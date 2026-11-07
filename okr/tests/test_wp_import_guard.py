"""M3 hotfix: the JOYOKR import never overwrites what Django changed.

Reproduces the rehearsal bug (a key result checked in on Django, then the
old WordPress file imported again put the old value back) and covers the 14
required cases plus rollback. A blocked import is checked against the whole
database field by field (not only row counts), the batch table and the
private evidence files.
"""

import copy
import io
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError

from okr.legacy_import.importer import ImportBlocked, ImportRun
from okr.legacy_import.provenance import canon
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
    OKRRoleEvent,
    OKRSettings,
    OKRTeam,
    OKRTeamMember,
)
from okr.services import engagement, okrs, organization
from okr.tests.factories import okr_cycle, okr_objective
from okr.tests.test_services import as_viewer
from okr.tests.test_wp_import import PNG, WordPressImportBase

ALL_MODELS = (
    OKRCycle, OKRTeam, OKRTeamMember, OKRObjective, OKRKeyResult, OKRCheckin, OKRComment, OKREvidence,
    OKRNotification, OKRActivity, OKRRole, OKRRoleEvent, OKRDepartmentRole, OKRSettings, OKRIdentityMap,
    OKRMigrationBatch,
)


def rows(model, **filters):
    return [
        {field.attname: canon(getattr(obj, field.attname)) for field in obj._meta.concrete_fields}
        for obj in model._base_manager.filter(**filters).order_by("pk")
    ]


def state(**filters):
    """Every OKR row, every column (updated_at included)."""
    return {model.__name__: rows(model, **filters) for model in ALL_MODELS}


def stats_total(report, outcome, skip=()):
    return sum(c.get(outcome, 0) for table, c in report["stats"].items() if table not in skip)


class ImportGuardBase(WordPressImportBase):
    def kr(self, wp_id):
        return OKRKeyResult._base_manager.get(legacy_wp_id=wp_id)

    def objective(self, wp_id):
        return OKRObjective._base_manager.get(legacy_wp_id=wp_id)

    def newer(self, data=None):
        """A later WordPress export (the final delta)."""
        delta = copy.deepcopy(data or self.data)
        delta["source"]["exported_at"] = "2026-11-07T03:00:00Z"
        return delta

    def files(self):
        root = Path(self.private_root)
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())

    def conflicts(self, report):
        return {(c["table"], c["wp_id"], c["kind"]) for c in report["conflicts"]}

    def assert_blocked(self, expected, **kwargs):
        """The import is refused and NOTHING changed: rows, batches, files."""
        before, files = state(), self.files()
        with self.assertRaises(ImportBlocked) as caught:
            self.run_import(**kwargs)
        self.assertEqual(state(), before, "a blocked import must not write anything")
        self.assertEqual(self.files(), files, "no evidence file may be left behind")
        report = caught.exception.report
        self.assertLessEqual(set(expected), self.conflicts(report), report["conflicts"])
        return report

    def checkin(self, wp_id, value, employee=None):
        return okrs.checkin(as_viewer(employee or self.owner), self.kr(wp_id).pk, current_value=value,
                            confidence=4, update_text="Cap nhat tren Django")


class ReproductionTests(ImportGuardBase):
    def test_bug_reproduced_old_file_no_longer_puts_the_old_value_back(self):
        # M3 rehearsal: KR imported at 21, checked in on Django to 26, old file again -> 21.
        self.data["tables"]["key_results"][0].update(current_value="21.0000", progress="21.00")
        for checkin in self.data["tables"]["checkins"]:
            if checkin["key_result_id"] == "51":
                checkin.update(current_value="21.0000", current_progress="21.00")
        self.run_import()
        self.assertEqual(self.kr(51).current_value, Decimal("21"))
        self.checkin(51, "26")
        report = self.assert_blocked({("key_results", 51, "changed_in_django")})
        self.assertEqual(self.kr(51).current_value, Decimal("26"), "the Django value stays")
        conflict = next(c for c in report["conflicts"] if c["table"] == "key_results")
        self.assertEqual(conflict["fields"]["current_value"], {"imported": "21", "django": "26", "wordpress": "21"})
        self.assertEqual(conflict["django_data"].get("checkins"), 1)
        latest = OKRCheckin._base_manager.filter(key_result=self.kr(51)).order_by("-created_at", "-id").first()
        self.assertEqual(latest.current_value, self.kr(51).current_value, "KR agrees with its newest check-in")


class RequiredCaseTests(ImportGuardBase):
    def test_01_first_import_passes(self):
        report = self.run_import()
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["stats"]["objectives"]["created"], 6)
        batch = OKRMigrationBatch.objects.get(pk=report["batch_id"])
        baseline = batch.stats["baseline"]
        self.assertEqual(baseline["key_results"]["51"]["current_value"], "75")
        self.assertEqual(baseline["objectives"]["33"]["parent"], self.objective(32).pk)
        self.assertEqual(batch.stats["company_id"], self.company.pk)

    def test_02_identical_file_again_changes_nothing(self):
        self.run_import(import_roles=True, import_settings=True)
        before = state()
        again = self.run_import(import_roles=True, import_settings=True)
        self.assertEqual(again["conflicts"], [])
        self.assertEqual(stats_total(again, "created"), 0, again["stats"])
        self.assertEqual(stats_total(again, "updated"), 0, again["stats"])
        self.assertEqual(again["stats"]["identity_map"], {"unchanged": 5})
        after = state()
        self.assertEqual(len(after["OKRMigrationBatch"]), len(before["OKRMigrationBatch"]) + 1)
        after.pop("OKRMigrationBatch"), before.pop("OKRMigrationBatch")
        self.assertEqual(after, before, "not one column changed")

    def test_03_wordpress_delta_without_django_changes_passes(self):
        self.run_import()
        delta = self.newer()
        delta["tables"]["key_results"][0].update(current_value="90.0000", progress="90.00")
        delta["tables"]["checkins"].append(
            {"id": "64", "key_result_id": "51", "user_id": "11", "previous_value": "75", "current_value": "90",
             "previous_progress": "75", "current_progress": "90", "confidence": "5", "update_text": "Lan 3",
             "created_at": "2026-11-01 03:00:00"})
        delta["tables"]["objectives"][2].update(title="Ca nhan WP (sua tren WordPress)", progress="70.00")
        delta["tables"]["notifications"][0]["is_read"] = "1"
        report = self.run_import(data=delta)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["stats"]["key_results"]["updated"], 1)
        self.assertEqual(report["stats"]["objectives"]["updated"], 1)
        self.assertEqual(report["stats"]["checkins"]["created"], 1)
        self.assertEqual(self.kr(51).current_value, Decimal("90"))
        self.assertEqual(self.objective(33).title, "Ca nhan WP (sua tren WordPress)")
        self.assertTrue(OKRNotification._base_manager.get(legacy_wp_id=91).is_read)
        self.assertEqual(report["reconciliation"]["key_result_vs_latest_checkin"], [])

    def test_04_django_checkin_and_old_wordpress_file_blocks(self):
        self.run_import()
        self.checkin(51, "80")
        report = self.assert_blocked({("key_results", 51, "changed_in_django"),
                                      ("objectives", 33, "changed_in_django")})
        kr_conflict = next(c for c in report["conflicts"] if c["table"] == "key_results")
        self.assertEqual(kr_conflict["fields"]["current_value"], {"imported": "75", "django": "80", "wordpress": "75"})
        self.assertEqual(self.kr(51).current_value, Decimal("80"))

    def test_04b_django_checkin_with_the_same_value_still_blocks(self):
        self.run_import()
        self.checkin(51, "75")  # value unchanged, but Django now owns this KR's history
        self.assert_blocked({("key_results", 51, "django_checkins")})

    def test_05_django_edit_of_an_objective_and_old_file_blocks(self):
        self.run_import()
        item = self.objective(33)
        okrs.update_objective(as_viewer(self.admin), item.pk, {
            "title": "Sua tren Django", "description": item.description, "cycle_id": item.cycle_id,
            "scope": item.scope, "department_id": item.department_id, "team_id": item.team_id,
            "parent_id": item.parent_id, "owner_id": item.owner_id, "weight": str(item.weight),
            "priority": item.priority,
        })
        report = self.assert_blocked({("objectives", 33, "changed_in_django")})
        fields = next(c for c in report["conflicts"] if c["wp_id"] == 33)["fields"]
        self.assertEqual(fields["title"]["django"], "Sua tren Django")
        self.assertEqual(self.objective(33).title, "Sua tren Django")

    def test_06_django_approval_and_old_file_blocks(self):
        tables = self.data["tables"]
        tables["objectives"][3].update(approval_status="submitted", parent_objective_id="32")
        tables["key_results"][2].update(approval_status="submitted", weight="50.00")
        tables["key_results"].append(dict(tables["key_results"][2], id="56", title="KR 56"))
        self.run_import()
        self.assertEqual(self.objective(34).approval_status, "submitted")
        okrs.approve_objective(as_viewer(self.admin), self.objective(34).pk)
        self.assert_blocked({("objectives", 34, "changed_in_django"), ("key_results", 53, "changed_in_django"),
                             ("key_results", 56, "changed_in_django")})
        self.assertEqual(self.objective(34).approval_status, "approved")

    def test_07_django_evidence_is_never_removed(self):
        self.run_import()
        native = engagement.create_evidence(as_viewer(self.owner), self.kr(51).pk,
                                            url="https://docs.example.com/moi", label="Them tren Django")
        kept = rows(OKREvidence, pk=native.pk)
        report = self.run_import()  # same (old) file: nothing to overwrite, nothing removed
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(rows(OKREvidence, pk=native.pk), kept)
        changed = self.newer()
        changed["tables"]["key_results"][0]["title"] = "KR doi ten tren WordPress"
        self.assert_blocked({("key_results", 51, "wordpress_change_on_django_data")}, data=changed)
        self.assertEqual(rows(OKREvidence, pk=native.pk), kept)

    def test_08_django_comment_is_never_removed_or_overwritten(self):
        self.run_import()
        top = OKRComment._base_manager.get(legacy_wp_id=71)
        mine = engagement.create_comment(as_viewer(self.owner), objective_id=self.objective(33).pk,
                                         content="Binh luan moi tren Django")
        reply = engagement.create_comment(as_viewer(self.owner), objective_id=self.objective(33).pk,
                                          content="Tra loi tren Django", parent_id=top.pk)
        kept = rows(OKRComment, legacy_wp_id__isnull=True)
        self.assertEqual({row["id"] for row in kept}, {mine.pk, reply.pk})
        self.assertEqual(self.run_import()["conflicts"], [])
        self.assertEqual(rows(OKRComment, legacy_wp_id__isnull=True), kept)
        changed = self.newer()
        changed["tables"]["comments"][0]["content"] = "Sua tren WordPress"
        self.assert_blocked({("comments", 71, "wordpress_change_on_django_data")}, data=changed)
        self.assertEqual(OKRComment._base_manager.get(legacy_wp_id=71).content, "Xem lai KR 2")

    def test_09_both_sides_changed_the_same_key_result_blocks(self):
        self.run_import()
        self.checkin(52, "18")
        delta = self.newer()
        delta["tables"]["key_results"][1].update(current_value="17.0000", progress="70.00")
        report = self.assert_blocked({("key_results", 52, "changed_in_django")}, data=delta)
        fields = next(c for c in report["conflicts"] if c["wp_id"] == 52)["fields"]
        self.assertEqual(fields["current_value"], {"imported": "15", "django": "18", "wordpress": "17"})

    def test_10_rows_created_in_django_are_not_touched(self):
        self.run_import()
        native = self.draft(title="Tao tren Django", cycle_id=OKRCycle._base_manager.get(legacy_wp_id=22).pk,
                            parent_id=self.objective(32).pk)
        native_kr = native.key_results.first()
        mine = {model.__name__: rows(model, legacy_wp_id__isnull=True)
                for model in (OKRObjective, OKRKeyResult, OKRCheckin, OKRComment, OKREvidence)}
        report = self.run_import()
        self.assertEqual(report["conflicts"], [])
        self.assertEqual({model.__name__: rows(model, legacy_wp_id__isnull=True)
                          for model in (OKRObjective, OKRKeyResult, OKRCheckin, OKRComment, OKREvidence)}, mine)
        self.assertTrue(OKRKeyResult._base_manager.filter(pk=native_kr.pk, legacy_wp_id__isnull=True).exists())
        changed = self.newer()
        changed["tables"]["objectives"][1]["title"] = "Phong ban doi ten tren WordPress"
        self.assert_blocked({("objectives", 32, "wordpress_change_on_django_data")}, data=changed)

    def _delta_with_new_rows(self):
        delta = self.newer()
        tables = delta["tables"]
        tables["objectives"].append(dict(tables["objectives"][2], id="37", title="Moi tren WordPress"))
        tables["key_results"].append(dict(tables["key_results"][0], id="57", objective_id="37", weight="100.00"))
        tables["checkins"].append(dict(tables["checkins"][1], id="65", key_result_id="57"))
        tables["comments"].append(dict(tables["comments"][0], id="74", object_id="37"))
        tables["evidence"].append(dict(tables["evidence"][0], id="86", key_result_id="57", checkin_id=None,
                                       attachment_id="504"))
        delta["attachments"].append(dict(delta["attachments"][0], id=504, relative_path="2026/10/moi.png",
                                         original_name="moi.png"))
        (self.uploads / "2026" / "10" / "moi.png").write_bytes(PNG)
        return delta

    def test_11_conflict_found_at_the_end_writes_nothing_at_all(self):
        self.run_import(import_roles=True)
        role = OKRRole.objects.get(company=self.company, employee=self.manager, role="manager")
        organization.revoke_role(as_viewer(self.admin), role.pk)  # roles run last
        delta = self._delta_with_new_rows()
        report = self.assert_blocked({("roles", 12, "deleted_in_django")}, data=delta, import_roles=True)
        self.assertEqual(report["phase"], "prescan")
        self.assertFalse(OKRObjective._base_manager.filter(legacy_wp_id=37).exists())
        # Even with the fail-closed dry pass switched off, the writing pass
        # itself refuses at the very end and rolls back everything it wrote
        # (rows AND the evidence file it had already stored).
        with mock.patch.object(ImportRun, "_prescan", lambda run: None):
            report = self.assert_blocked({("roles", 12, "deleted_in_django")}, data=delta, import_roles=True)
        self.assertEqual(report["phase"], "apply")
        self.assertEqual(report["stats"]["objectives"].get("created"), 1, "the new rows were written, then undone")
        self.assertFalse(OKRObjective._base_manager.filter(legacy_wp_id=37).exists())
        self.assertFalse(OKRRole.objects.filter(employee=self.manager, role="manager").exists(), "never re-granted")

    def test_12_dry_run_and_apply_apply_the_same_rules(self):
        self.run_import()
        self.checkin(51, "80")
        dry = self.assert_blocked(set(), apply=False)
        applied = self.assert_blocked(set(), apply=True)
        self.assertTrue(dry["conflicts"])
        self.assertEqual(self.conflicts(dry), self.conflicts(applied))
        self.assertEqual((dry["phase"], applied["phase"]), ("dry_run", "prescan"))

    def test_12b_clean_delta_dry_run_predicts_the_apply(self):
        self.run_import()
        delta = self._delta_with_new_rows()
        dry = self.run_import(data=delta, apply=False)
        applied = self.run_import(data=delta)
        self.assertEqual(dry["conflicts"], applied["conflicts"])
        without_identity = lambda r: {t: c for t, c in r["stats"].items() if t != "identity_map"}  # noqa: E731
        self.assertEqual(without_identity(dry), without_identity(applied))

    def test_13_rerun_after_a_valid_delta_is_idempotent_and_older_files_are_refused(self):
        self.run_import()
        delta = self._delta_with_new_rows()
        delta["tables"]["key_results"][0].update(current_value="90.0000", progress="90.00")
        delta["tables"]["checkins"].append(dict(delta["tables"]["checkins"][1], id="66", current_value="90.0000",
                                                current_progress="90.00", created_at="2026-11-02 03:00:00"))
        self.run_import(data=delta)
        before = state()
        again = self.run_import(data=delta)
        self.assertEqual((again["conflicts"], stats_total(again, "created"), stats_total(again, "updated")), ([], 0, 0))
        after = state()
        after.pop("OKRMigrationBatch"), before.pop("OKRMigrationBatch")
        self.assertEqual(after, before)
        # The first (older) export again would put old WordPress values back.
        self.assert_blocked({("export", None, "stale_export")})

    def test_14_another_company_is_not_affected(self):
        cycle = okr_cycle(self.other_company, name="Chu ky cong ty khac")
        okr_objective(cycle, self.outsider, title="Muc tieu cong ty khac")
        theirs = {model.__name__: rows(model, company_id=self.other_company.pk)
                  for model in (OKRCycle, OKRObjective, OKRKeyResult, OKRCheckin, OKRComment, OKRRole)}
        self.assertTrue(theirs["OKRObjective"])
        self.run_import()
        self.checkin(51, "80")
        self.assert_blocked({("key_results", 51, "changed_in_django")})
        self.assertEqual({model.__name__: rows(model, company_id=self.other_company.pk)
                          for model in (OKRCycle, OKRObjective, OKRKeyResult, OKRCheckin, OKRComment, OKRRole)},
                         theirs)


class MoreDjangoChangeTests(ImportGuardBase):
    def test_imported_evidence_deleted_in_django_is_not_brought_back(self):
        self.run_import()
        OKREvidence._base_manager.filter(legacy_wp_id=82).delete()
        self.assert_blocked({("evidence", 82, "deleted_in_django")})

    def test_read_in_django_stays_read(self):
        self.run_import()
        OKRNotification._base_manager.filter(legacy_wp_id=91).update(is_read=True)
        report = self.run_import()
        self.assertEqual(report["conflicts"], [])
        self.assertTrue(OKRNotification._base_manager.get(legacy_wp_id=91).is_read)

    def test_settings_changed_in_django_block(self):
        self.run_import(import_settings=True)
        OKRSettings.objects.filter(company=self.company).update(warning_threshold=Decimal("35"))
        self.assert_blocked({("settings", "company", "changed_in_django")}, import_settings=True)

    def test_rows_without_an_import_snapshot_are_only_kept_when_identical(self):
        self.run_import()
        OKRMigrationBatch.objects.all().delete()  # e.g. imported by an older tool: no snapshot
        self.assertEqual(self.run_import()["conflicts"], [])
        OKRMigrationBatch.objects.all().delete()
        changed = self.newer()
        changed["tables"]["key_results"][0]["title"] = "Khac"
        self.assert_blocked({("key_results", 51, "no_provenance")}, data=changed)

    def test_wordpress_update_that_disagrees_with_its_latest_checkin_blocks(self):
        self.run_import()
        delta = self.newer()
        delta["tables"]["key_results"][0].update(current_value="95.0000", progress="95.00")  # no check-in for it
        report = self.assert_blocked(set(), data=delta)
        self.assertIn("error:key_result_vs_latest_checkin", report["validation"]["codes"])
        # Second line of defence: even without the source check the writing pass refuses it.
        quiet = {"counts": {"error": 0, "warning": 0, "info": 0}, "codes": {}, "files": {}, "findings": []}
        with mock.patch("okr.legacy_import.importer.validate_source", return_value=quiet):
            self.assert_blocked({("key_results", 51, "value_vs_latest_checkin")}, data=delta)

    def test_command_line_prints_each_conflict_and_writes_the_report(self):
        self.run_import()
        self.checkin(51, "80")
        export = self.write_export()
        report_path = Path(self.tmp) / "blocked.json"
        err = io.StringIO()
        with self.assertRaises(CommandError) as caught:
            call_command("okr_import_wp", "import", str(export), "--company", str(self.company.pk),
                         "--mapping", str(self.verified_mapping(export)), "--uploads-dir", str(self.uploads),
                         "--report", str(report_path), "--apply", "--operator",
                         self.admin.employee_user_id.username, stdout=io.StringIO(), stderr=err)
        self.assertIn("nothing was written", str(caught.exception))
        self.assertIn("CONFLICT changed_in_django key_results wp#51", err.getvalue())
        self.assertIn('"changed_in_django"', report_path.read_text(encoding="utf-8"))


class RollbackWithGuardTests(ImportGuardBase):
    def test_rollback_of_a_delta_puts_the_old_values_back_and_it_can_be_applied_again(self):
        self.run_import()
        delta = self.newer()
        delta["tables"]["key_results"][0].update(current_value="90.0000", progress="90.00")
        delta["tables"]["checkins"].append(dict(delta["tables"]["checkins"][1], id="64", current_value="90.0000",
                                                current_progress="90.00", created_at="2026-11-01 03:00:00"))
        before = rows(OKRKeyResult, legacy_wp_id=51)
        report = self.run_import(data=delta)
        self.assertEqual(self.kr(51).current_value, Decimal("90"))
        result = rollback_batch(report["batch_id"], apply=True)
        self.assertEqual(result["reverted_updates"], {"key_results": 1})
        after = rows(OKRKeyResult, legacy_wp_id=51)
        for row in (before[0], after[0]):
            row.pop("updated_at")
        self.assertEqual(after, before)
        self.assertFalse(OKRCheckin._base_manager.filter(legacy_wp_id=64).exists())
        again = self.run_import(data=delta)
        self.assertEqual(again["conflicts"], [], "no false conflict after a rollback")

    def test_rollback_refused_when_django_changed_a_row_of_the_batch(self):
        report = self.run_import()
        OKRObjective._base_manager.filter(legacy_wp_id=34).update(title="Sua tren Django")
        before = state()
        with self.assertRaises(RollbackRefused) as caught:
            rollback_batch(report["batch_id"], apply=True)
        self.assertIn("objectives:34 (title)", str(caught.exception))
        self.assertEqual(state(), before)

    def test_rollback_newest_batch_first(self):
        first = self.run_import()
        delta = self.newer()
        delta["tables"]["objectives"][2]["title"] = "Doi tren WordPress"
        self.run_import(data=delta)
        with self.assertRaises(RollbackRefused) as caught:
            rollback_batch(first["batch_id"], apply=True)
        self.assertIn("must be rolled back first", str(caught.exception))
