"""JOYOKR (WordPress) → Django OKR migration.

    # (No SSH / WP-CLI on the host: build the export from the CSV results of
    #  joyokr_export_queries.sql run in phpMyAdmin/Adminer.)
    manage.py okr_import_wp assemble --from-dir CSV_DIR --uploads-dir FILES --exported-at 2026-11-07T08:00:00Z \n        --out EXPORT.json

    # 0. Check the export file (no database needed, nothing written):
    manage.py okr_import_wp inspect EXPORT.json --uploads-dir /path/to/uploads-copy --report inspect.json

    # 1. Suggest matches for a person to verify (no database write); --out
    #    ending in .csv gives the review table (Excel), .json the raw file:
    manage.py okr_import_wp propose EXPORT.json --company 1 --out mapping.csv

    # 2. Dry run (default): everything is checked, nothing is kept.
    manage.py okr_import_wp import EXPORT.json --company 1 --mapping mapping.json \
        --uploads-dir /path/to/uploads-copy --report report.json

    # 3. Apply (only after the dry run is clean and approved):
    manage.py okr_import_wp import ... --apply --operator <hr-username>

    # Undo one applied batch (dry run unless --apply):
    manage.py okr_import_wp rollback --batch 12 --apply --operator <hr-username>

Source check: before anything is written (dry run and --apply alike) the
export is checked record by record (okr/legacy_import/validate.py). Any
ERROR (broken parent/cycle/KR link, KR value against its newest check-in,
weights, approval status, required owner not mapped, evidence file missing
or with a wrong checksum...) refuses the import; WARNINGs are for review.

Conflict guard: a row changed in Django after it was imported (edit,
approval, check-in, deletion, revoked role...) is never overwritten. Any
conflict refuses the whole import and nothing is written; the report lists
each conflict with the imported / Django / WordPress values. There is no
option to force an import over a conflict.

See okr/docs/wp_migration.md. Never run against production without the
approved runbook (backup verified, WordPress frozen for the final delta).
"""

from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from okr.legacy_import.export import ExportError, load_export
from okr.legacy_import.importer import ImportBlocked, ImportRun
from okr.legacy_import.inspect import inspect_export
from okr.legacy_import.mapping import load_mapping, propose, write_mapping_csv
from okr.legacy_import.rollback import RollbackRefused, rollback_batch
from okr.legacy_import.validate import validate_source


class Command(BaseCommand):
    help = "Migrate JOYOKR WordPress data into Django OKR (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["assemble", "inspect", "propose", "import", "rollback"])
        parser.add_argument("export", nargs="?", help="JOYOKR export JSON (joyokr_export.php).")
        parser.add_argument("--company", type=int, help="HR company id that receives the data.")
        parser.add_argument("--out", help="propose: where to write the mapping file.")
        parser.add_argument("--mapping", help="import: the reviewed mapping file.")
        parser.add_argument("--uploads-dir", help="import: copy of wp-content/uploads for evidence files.")
        parser.add_argument("--report", help="Write the full JSON report here.")
        parser.add_argument("--batch", type=int, help="rollback: batch id.")
        parser.add_argument("--apply", action="store_true", help="Keep the changes (default: dry run).")
        parser.add_argument("--operator", help="HR username running an --apply (recorded on the batch).")
        parser.add_argument("--import-roles", action="store_true",
                            help="Also import OKR manager and department manager roles (never admin).")
        parser.add_argument("--import-settings", action="store_true",
                            help="Also import warning threshold, reminder frequency, e-mail flag, rating ranges.")
        parser.add_argument("--from-dir", help="assemble: folder of CSV files saved from joyokr_export_queries.sql.")
        parser.add_argument("--exported-at", help="assemble: UTC time the CSV files were exported (ISO 8601).")
        parser.add_argument("--table-prefix", default="wp_", help="assemble: WordPress table prefix.")
        parser.add_argument("--null-string", default="NULL", help="assemble: how the CSV files write NULL.")
        parser.add_argument("--csv-delimiter", default=",", help="assemble: CSV column separator.")
        parser.add_argument("--theme-version", default="", help="assemble: JOYOKR theme version, if known.")
        parser.add_argument("--allow-unmapped", action="store_true",
                            help="Import even when some referenced users/departments are not verified "
                                 "(their links stay empty). Not for the production cut-over.")

    def handle(self, *args, **options):
        action = options["action"]
        operator = self._operator(options)
        if action == "rollback":
            return self._rollback(options, operator)
        if action == "assemble":
            return self._assemble(options)
        if not options.get("export"):
            raise CommandError("An export file is required.")
        try:
            export = load_export(options["export"])
        except ExportError as exc:
            raise CommandError(str(exc)) from exc
        if action == "inspect":
            return self._inspect(export, options)
        company = self._company(options)
        if action == "propose":
            return self._propose(export, company, options)
        return self._import(export, company, operator, options)

    # -- helpers ---------------------------------------------------------------------

    def _company(self, options):
        from base.models import Company

        if not options.get("company"):
            raise CommandError("--company is required.")
        company = Company.objects.filter(pk=options["company"]).first()
        if company is None:
            raise CommandError(f"Company {options['company']} does not exist.")
        return company

    def _operator(self, options):
        if not options.get("apply"):
            return None
        username = options.get("operator")
        if not username:
            raise CommandError("--apply needs --operator <hr-username> for the audit trail.")
        user = get_user_model().objects.filter(username=username, is_active=True).first()
        if user is None:
            raise CommandError(f"Active user {username!r} not found.")
        return user

    def _write(self, path, data):
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # -- actions ---------------------------------------------------------------------

    def _assemble(self, options):
        from okr.legacy_import.assemble import assemble

        for name in ("from_dir", "exported_at", "out"):
            if not options.get(name):
                raise CommandError(f"--{name.replace('_', '-')} is required for assemble.")
        try:
            data = assemble(options["from_dir"], options.get("uploads_dir"), exported_at=options["exported_at"],
                            table_prefix=options["table_prefix"], null=options["null_string"],
                            delimiter=options["csv_delimiter"], theme_version=options["theme_version"])
        except ExportError as exc:
            raise CommandError(str(exc)) from exc
        self._write(options["out"], data)
        summary = data["summary"]
        self.stdout.write("Export written: " + ", ".join(f"{k}={v}" for k, v in summary["counts"].items())
                          + f"; users={summary['users']}; evidence files={summary['attachments']} "
                          f"(missing {summary['missing_files']}). Next: inspect.")

    def _validation_lines(self, validation, limit=40):
        if not validation:
            return
        counts = validation["counts"]
        self.stdout.write(f"Source check: ERROR={counts['error']} WARNING={counts['warning']} INFO={counts['info']}")
        shown = [f for f in validation["findings"] if f["severity"] == "error"][:limit]
        for item in shown:
            self.stderr.write(f"  ERROR {item['code']} {item['table']} wp#{item['wp_id']}: {item['detail']}")

    def _inspect(self, export, options):
        result = inspect_export(export, options.get("uploads_dir"))
        # Without a mapping the identity checks that need one are skipped.
        result["validation"] = validate_source(export, None, options.get("uploads_dir"))
        self._validation_lines(result["validation"])
        if options.get("report"):
            self._write(options["report"], result)
        counts = result["counts"]
        self.stdout.write("Tables: " + ", ".join(f"{name}={counts[name]}" for name in counts))
        files = result["evidence"]["files"]
        self.stdout.write(
            f"Evidence files: checked={files['checked']} ok={files['ok']} "
            f"missing={len(files['missing_on_disk'])} checksum_mismatch={len(files['checksum_mismatch'])} "
            f"not_in_export={len(files['not_in_export'])}"
        )
        self.stdout.write(f"Problems: {sum(len(v) for v in result['problems'].values())} "
                          f"in {len(result['problems'])} checks; export sha256 {export.checksum}")

    def _propose(self, export, company, options):
        if not options.get("out"):
            raise CommandError("--out is required for propose.")
        mapping = propose(export, company.pk)
        if str(options["out"]).lower().endswith(".csv"):
            write_mapping_csv(mapping, options["out"])
        else:
            self._write(options["out"], mapping)
        users = mapping["users"]
        departments = mapping["departments"]
        self.stdout.write(
            f"Mapping written: {len(users)} users "
            f"({sum(u['status'] == 'proposed' for u in users)} proposed, "
            f"{sum(u['status'] == 'unmatched' for u in users)} unmatched), "
            f"{len(departments)} departments "
            f"({sum(d['status'] == 'proposed' for d in departments)} proposed). "
            "Nothing is verified: a person must set status to 'verified' row by row."
        )

    def _import(self, export, company, operator, options):
        if not options.get("mapping"):
            raise CommandError("--mapping is required for import.")
        try:
            mapping = load_mapping(options["mapping"], company.pk)
        except ExportError as exc:
            raise CommandError(str(exc)) from exc
        run = ImportRun(
            export,
            mapping,
            company,
            operator=operator,
            uploads_dir=options.get("uploads_dir"),
            apply=options.get("apply", False),
            import_roles=options.get("import_roles", False),
            import_settings=options.get("import_settings", False),
            allow_unmapped=options.get("allow_unmapped", False),
        )
        try:
            report = run.run()
        except ImportBlocked as blocked:
            if options.get("report"):
                self._write(options["report"], blocked.report)
            self._validation_lines(blocked.report.get("validation"))
            for item in (blocked.report.get("conflicts") or [])[:30]:
                fields = ", ".join(
                    f"{name}: " + " / ".join(f"{k}={v}" for k, v in values.items())
                    for name, values in item.get("fields", {}).items()
                )
                extra = f" [django data: {item['django_data']}]" if item.get("django_data") else ""
                self.stderr.write(f"  CONFLICT {item['kind']} {item['table']} wp#{item['wp_id']}: {fields}{extra}")
            raise CommandError(f"Import blocked, nothing written: {blocked}") from blocked
        if options.get("report"):
            self._write(options["report"], report)
        self._summary(report)
        return None

    def _summary(self, report):
        self.stdout.write(f"Mode: {report['mode']}  batch: {report['batch_id']}")
        self._validation_lines(report.get("validation"))
        for table, counts in report["stats"].items():
            self.stdout.write(f"  {table:<16} " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        self.stdout.write(f"Issues: {report['issue_counts'] or 'none'}")
        rec = report["reconciliation"]
        self.stdout.write(
            "Reconciliation: "
            f"KR progress mismatches={len(rec.get('key_result_progress_mismatches', []))}, "
            f"objective progress mismatches={len(rec.get('objective_progress_mismatches', []))}, "
            f"admin candidates (not granted)={len(report['admin_candidates'])}"
        )
        if report["mode"] == "dry_run":
            self.stdout.write("Dry run: nothing was kept.")

    def _rollback(self, options, operator):
        if not options.get("batch"):
            raise CommandError("--batch is required for rollback.")
        try:
            result = rollback_batch(options["batch"], apply=options.get("apply", False), operator=operator)
        except RollbackRefused as exc:
            raise CommandError(str(exc)) from exc
        if options.get("report"):
            self._write(options["report"], result)
        self.stdout.write(json.dumps(result, ensure_ascii=False))
