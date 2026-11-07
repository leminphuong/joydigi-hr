"""M3 fallback for hosts without SSH / WP-CLI: read-only SELECTs run in a
database tool, saved as CSV, assembled into the same export the WP-CLI
script writes (okr/legacy_import/assemble.py, joyokr_export_queries.sql).
"""

import csv
import io
import json
import re
from pathlib import Path

from django.core.management import call_command

from okr.legacy_import.assemble import assemble
from okr.legacy_import.export import TABLES, ExportError, load_export
from okr.legacy_import.validate import validate_source
from okr.tests.test_wp_import import WordPressImportBase

QUERIES = Path(__file__).resolve().parents[1] / "legacy_import" / "joyokr_export_queries.sql"
EXPORTER = Path(__file__).resolve().parents[1] / "legacy_import" / "joyokr_export.php"


def _cell(value):
    return "NULL" if value is None else str(value)


class AssembleFromDatabaseToolTests(WordPressImportBase):
    def write_csv(self, folder, name, columns, rows):
        with open(folder / f"{name}.csv", "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([_cell(row.get(column)) for column in columns])

    def csv_folder(self, data=None):
        """What phpMyAdmin saves for each SELECT of joyokr_export_queries.sql."""
        data = data or self.data
        folder = Path(self.tmp) / "csv"
        folder.mkdir(exist_ok=True)
        for name in TABLES:
            rows = data["tables"][name]
            columns = list(dict.fromkeys(key for row in rows for key in row)) or ["id"]
            self.write_csv(folder, name, columns, rows)
        self.write_csv(folder, "users", ["ID", "user_login", "user_email", "display_name", "user_registered"], [
            {"ID": u["id"], "user_login": u["login"], "user_email": u["email"], "display_name": u["display_name"],
             "user_registered": "2026-01-01 00:00:00"} for u in data["users"]])
        meta = []
        for user in data["users"]:
            roles = "".join(f's:{len(role)}:"{role}";b:1;' for role in user["roles"])
            meta.append({"user_id": user["id"], "meta_key": "wp_capabilities",
                         "meta_value": f"a:{len(user['roles'])}:{{{roles}}}"})
            if user.get("employee_code"):
                meta.append({"user_id": user["id"], "meta_key": "joyokr_employee_code",
                             "meta_value": user["employee_code"]})
        self.write_csv(folder, "usermeta", ["user_id", "meta_key", "meta_value"], meta)
        self.write_csv(folder, "attachments", ["ID", "post_mime_type", "attached_file"], [
            {"ID": a["id"], "post_mime_type": a["mime_type"], "attached_file": a["relative_path"]}
            for a in data["attachments"]])
        options = [{"option_name": "home", "option_value": "https://okr.example.test"},
                   {"option_name": "joyokr_schema_version", "option_value": "12"}]
        options += [{"option_name": f"joyokr_{k}", "option_value": v} for k, v in data["options"].items()]
        self.write_csv(folder, "options", ["option_name", "option_value"], options)
        return folder

    def test_sql_file_only_reads_and_never_touches_credentials(self):
        source = QUERIES.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("--"))
        statements = [s.strip() for s in code.split(";") if s.strip()]
        self.assertEqual(len(statements), 16)
        for statement in statements:
            self.assertTrue(statement.upper().startswith("SELECT"), statement[:40])
        for word in ("user_pass", "user_activation_key", "session_tokens", "telegram", "sso", "INSERT", "UPDATE",
                     "DELETE", "DROP", "ALTER", "CREATE", "REPLACE", "INTO OUTFILE"):
            self.assertNotIn(word.lower(), code.lower())
        self.assertIsNone(re.search(r"SELECT\s+\*\s+FROM\s+wp_(users|usermeta|options|posts|postmeta)\b", code))
        self.assertEqual(len(set(re.findall(r"wp_joyokr_(\w+)", code))), 11)
        # The WP-CLI script prints its own self-check and never creates folders.
        php = EXPORTER.read_text(encoding="utf-8")
        self.assertIn("wp_upload_dir( null, false )", php)
        self.assertNotIn("wp_upload_dir()", php)
        self.assertIn("'broken_links'", php)

    def test_csv_files_give_the_same_export_as_the_wp_cli_script(self):
        out = Path(self.tmp) / "assembled.json"
        stdout = io.StringIO()
        call_command("okr_import_wp", "assemble", from_dir=str(self.csv_folder()), uploads_dir=str(self.uploads),
                     exported_at="2026-11-06T03:00:00Z", out=str(out), stdout=stdout)
        self.assertIn("evidence files=1 (missing 0)", stdout.getvalue())
        built = load_export(out)
        expected = {name: [{k: (None if v is None else str(v)) for k, v in row.items()} for row in rows]
                    for name, rows in self.data["tables"].items()}
        self.assertEqual(built.tables, expected)
        self.assertEqual({u: (row["roles"], row["employee_code"]) for u, row in built.users.items()},
                         {u["id"]: (u["roles"], u.get("employee_code", "")) for u in self.data["users"]})
        self.assertEqual(built.attachments[501]["sha256"], self.data["attachments"][0]["sha256"])
        self.assertEqual(built.source["export_method"], "database_tool_csv")
        self.assertEqual(validate_source(built, None, self.uploads)["counts"]["error"], 0)
        # The importer reads it like the WP-CLI file.
        same = self.run_import(apply=False, data=json.loads(out.read_text(encoding="utf-8")))
        reference = self.run_import(apply=False)
        self.assertEqual(same["stats"], reference["stats"])

    def test_missing_files_credentials_and_bad_inputs_are_refused_or_reported(self):
        folder = self.csv_folder()
        (self.uploads / "2026" / "10" / "bao-cao.png").unlink()
        data = assemble(folder, self.uploads, exported_at="2026-11-06T03:00:00Z")
        self.assertTrue(data["attachments"][0]["missing"])
        self.assertEqual(data["summary"]["missing_files"], 1)
        bad = Path(self.tmp) / "bad"
        bad.mkdir()
        for path in folder.iterdir():
            (bad / path.name).write_bytes(path.read_bytes())
        self.write_csv(bad, "users", ["ID", "user_login", "user_pass"], [{"ID": 11, "user_login": "a", "user_pass": "x"}])
        with self.assertRaisesRegex(ExportError, "credential"):
            assemble(bad, self.uploads, exported_at="2026-11-06T03:00:00Z")
        (bad / "options.csv").unlink()
        with self.assertRaisesRegex(ExportError, "options.csv"):
            assemble(bad, self.uploads, exported_at="2026-11-06T03:00:00Z")
        with self.assertRaisesRegex(ExportError, "time zone"):
            assemble(folder, self.uploads, exported_at="2026-11-06 03:00:00")
