"""M3: source checks before the first write (okr/legacy_import/validate.py).

Every ERROR refuses the dry run and the apply alike and writes nothing (all
OKR rows, every column, the batch table and the evidence files are compared
before/after). WARNINGs import and are reported; INFO changes nothing.
"""

import copy
import io
from pathlib import Path

from django.core.management import call_command

from okr.legacy_import.export import load_export
from okr.legacy_import.validate import validate_source
from okr.models import OKRObjective
from okr.tests.factories import okr_cycle
from okr.tests.test_wp_import import PNG
from okr.tests.test_wp_import_guard import ImportGuardBase, state


def _find(tables, table, wp_id):
    return next(row for row in tables[table] if row["id"] == str(wp_id))


class SourceValidationTests(ImportGuardBase):
    def findings(self, report, severity=None):
        return {(f["code"], f["table"], f["wp_id"]) for f in report["validation"]["findings"]
                if severity is None or f["severity"] == severity}

    def assert_source_error(self, data, expected, **kwargs):
        """Dry run and apply both refused, nothing written, the ERROR named."""
        for apply in (False, True):
            report = self.assert_blocked(set(), data=data, apply=apply, **kwargs)
            self.assertEqual(report["conflicts"], [], "refused by the source check, before any write")
            self.assertLessEqual(expected, self.findings(report, "error"), report["validation"]["codes"])
        return report

    def test_clean_export_has_no_error_and_its_warnings_and_infos_are_listed(self):
        report = self.run_import()
        self.assertEqual(report["validation"]["counts"]["error"], 0, report["validation"]["codes"])
        self.assertLessEqual({
            ("key_result_value_without_checkin", "key_results", 52),
            ("key_result_value_without_checkin", "key_results", 41),
            ("weight_total_draft", "objectives", 34),
            ("user_deleted_in_wordpress", "comments", 73),
            ("user_rejected_in_mapping", "team_members", "7:14"),
        }, self.findings(report, "warning"))
        self.assertLessEqual({
            ("notification_not_migrated", "notifications", 92),
            ("notification_reference_missing", "notifications", 93),
            ("activity_action_unknown", "activity_log", 104),
            ("admin_not_granted", "users", 13),
            ("deleted_in_wordpress", "objectives", 35),
        }, self.findings(report, "info"))
        self.assertEqual(report["validation"]["files"], {"checked": 1, "ok": 1})
        # ...and the same key results are listed for review in the reconciliation.
        listed = {row["wp_id"] for row in report["reconciliation"]["key_results_without_checkin_history"]}
        self.assertEqual(listed, {41, 42, 43, 44, 52})

    def test_every_error_refuses_the_import_and_writes_nothing(self):
        def changed(edit):
            data = copy.deepcopy(self.data)
            edit(data["tables"], data)
            return data

        def loop(t, d):
            _find(t, "objectives", 31)["parent_objective_id"] = "33"

        def broken_rows(t, d):
            t["key_results"] = copy.deepcopy(self.dirty["tables"]["key_results"])  # KR 54 -> objective 999
            t["checkins"].append(dict(t["checkins"][0], id="63", key_result_id="999"))

        def files(t, d):
            t["evidence"] = copy.deepcopy(self.dirty["tables"]["evidence"])
            d["attachments"] = copy.deepcopy(self.dirty["attachments"])

        cases = {
            "parent loop": (loop, {("objective_parent_loop", "objectives", 31)}),
            "own parent": (lambda t, d: _find(t, "objectives", 34).update(parent_objective_id="34"),
                           {("objective_parent_loop", "objectives", 34)}),
            "parent missing": (lambda t, d: _find(t, "objectives", 34).update(parent_objective_id="999"),
                               {("objective_parent_missing", "objectives", 34)}),
            "cycle missing": (lambda t, d: _find(t, "objectives", 34).update(cycle_id="999"),
                              {("objective_cycle_missing", "objectives", 34)}),
            "KR and check-in references": (broken_rows, {("key_result_objective_missing", "key_results", 54),
                                                        ("checkin_key_result_missing", "checkins", 63)}),
            "KR value vs newest check-in": (lambda t, d: _find(t, "key_results", 51).update(
                current_value="80.0000", progress="80.00"), {("key_result_vs_latest_checkin", "key_results", 51)}),
            "weights": (lambda t, d: _find(t, "key_results", 52).update(weight="30.00"),
                        {("weight_total_invalid", "objectives", 33)}),
            "approval status": (lambda t, d: _find(t, "objectives", 34).update(approval_status="cho_duyet"),
                                {("approval_status_invalid", "objectives", 34)}),
            "owner deleted in WordPress": (lambda t, d: _find(t, "objectives", 34).update(owner_user_id="99"),
                                           {("user_deleted_in_wordpress", "objectives", 34)}),
            "owner empty": (lambda t, d: _find(t, "key_results", 53).update(owner_user_id=None),
                            {("owner_missing", "key_results", 53)}),
            "owner rejected in the mapping": (lambda t, d: _find(t, "objectives", 34).update(owner_user_id="14"),
                                              {("owner_not_verified", "objectives", 34)}),
            "approved objective without key results": (
                lambda t, d: t.update(key_results=[kr for kr in t["key_results"] if kr["objective_id"] != "31"]),
                {("objective_without_key_results", "objectives", 31)}),
            "submitted objective without key results": (
                lambda t, d: t.update(key_results=[kr for kr in t["key_results"] if kr["objective_id"] != "36"]),
                {("objective_without_key_results", "objectives", 36)}),
            "evidence files and link": (files, {("evidence_file_missing", "evidence", 83),
                                                ("evidence_checksum_mismatch", "evidence", 84),
                                                ("evidence_link_invalid", "evidence", 85)}),
            "unknown value": (lambda t, d: _find(t, "cycles", 22).update(status="archived"),
                              {("value_unknown", "cycles", 22)}),
            "cycle dates": (lambda t, d: _find(t, "cycles", 22).update(end_date="2026-09-01"),
                            {("cycle_dates_invalid", "cycles", 22)}),
            "team objective without team": (lambda t, d: _find(t, "objectives", 36).update(team_id="999"),
                                            {("scope_target_missing", "objectives", 36)}),
            "comment target": (lambda t, d: _find(t, "comments", 71).update(object_id="999"),
                               {("comment_target_missing", "comments", 71)}),
            "timestamp": (lambda t, d: _find(t, "objectives", 34).update(created_at="hom qua"),
                          {("timestamp_unreadable", "objectives", 34)}),
        }
        for name, (edit, expected) in cases.items():
            with self.subTest(name):
                self.assert_source_error(changed(edit), expected)
        self.assertFalse(OKRObjective._base_manager.filter(legacy_wp_id__isnull=False).exists())

    def test_missing_uploads_copy_is_an_error(self):
        export_path = self.write_export()
        before, files = state(), self.files()
        from okr.legacy_import.importer import ImportBlocked, ImportRun
        from okr.legacy_import.mapping import load_mapping

        run = ImportRun(load_export(export_path), load_mapping(self.verified_mapping(export_path), self.company.pk),
                        self.company, operator=self.admin.employee_user_id, uploads_dir=None, apply=True)
        with self.assertRaises(ImportBlocked) as caught:
            run.run()
        self.assertIn(("evidence_file_missing", "evidence", 81), self.findings(caught.exception.report, "error"))
        self.assertEqual((state(), self.files()), (before, files))

    def test_error_in_a_later_delta_leaves_the_imported_data_untouched(self):
        self.run_import()
        before = state()
        delta = self.newer()
        _find(delta["tables"], "key_results", 51).update(current_value="99.0000", progress="99.00")
        self.assert_source_error(delta, {("key_result_vs_latest_checkin", "key_results", 51)})
        self.assertEqual(state(), before)

    def test_warnings_only_import_and_stay_in_the_report(self):
        data = copy.deepcopy(self.data)
        _find(data["tables"], "comments", 72)["user_id"] = "14"  # author rejected in the mapping (not an owner)
        _find(data["tables"], "key_results", 51)["progress"] = "70.00"  # formula says 75
        report = self.run_import(data=data)
        self.assertIn(("user_rejected_in_mapping", "comments", 72), self.findings(report, "warning"))
        self.assertIn(("key_result_progress_vs_formula", "key_results", 51), self.findings(report, "warning"))
        from okr.models import OKRComment
        self.assertIsNone(OKRComment._base_manager.get(legacy_wp_id=72).author_id)

    def test_a_row_the_importer_cannot_write_refuses_the_whole_run(self):
        # Not visible in the file: the legacy id already belongs to another company.
        cycle = okr_cycle(self.other_company, name="Khac")
        OKRObjective._base_manager.create(company=self.other_company, cycle=cycle, owner=self.outsider,
                                          title="Cong ty khac", legacy_wp_id=34)
        report = self.assert_blocked(set())
        self.assertIn(("objectives", 34), {(i["table"], i["wp_id"]) for i in report["issues"]
                                          if i["severity"] == "error"})
        self.assertEqual(OKRObjective._base_manager.filter(legacy_wp_id__isnull=False).count(), 1)

    def test_inspect_and_the_command_print_the_source_check(self):
        export_path = self.write_export(self.dirty)
        validation = validate_source(load_export(export_path), None, self.uploads)
        self.assertGreaterEqual(validation["counts"]["error"], 5)
        out, err = io.StringIO(), io.StringIO()
        report = Path(self.tmp) / "inspect.json"
        call_command("okr_import_wp", "inspect", str(export_path), uploads_dir=str(self.uploads),
                     report=str(report), stdout=out, stderr=err)
        self.assertIn("Source check: ERROR=", out.getvalue())
        self.assertIn("ERROR evidence_checksum_mismatch evidence wp#84", err.getvalue())
        self.assertIn('"validation"', report.read_text(encoding="utf-8"))
        self.assertNotIn(self.owner.email, report.read_text(encoding="utf-8"), "no e-mail in the report")
        self.assertTrue(PNG)
