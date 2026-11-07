"""Build a JOYOKR export (format 1) from CSV files saved with a database tool.

For hosts without SSH / WP-CLI: the administrator runs the read-only
SELECTs of ``joyokr_export_queries.sql`` in phpMyAdmin or Adminer, saves each
result as CSV and copies the listed evidence files out of
``wp-content/uploads``. This turns those files into exactly what
``joyokr_export.php`` would have written, computing each file's size and
SHA-256 from the copy. Nothing is fixed or dropped: a value is copied as
read; broken links are left for ``inspect`` / the source check to report.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from okr.legacy_import.export import TABLES, ExportError

FORMAT = "joyokr-export"
EXTRA_FILES = ("users", "usermeta", "attachments", "options")
FORBIDDEN_COLUMNS = {"user_pass", "user_activation_key", "session_tokens"}
SAFE_OPTIONS = ("warning_threshold", "reminder_frequency", "email_reminders", "rating_ranges")
PROFILE_KEYS = ("employee_code", "department_id", "manager_id", "employment_status")
_ROLE = re.compile(r's:\d+:"([^"]+)";b:1;')


def _read(path: Path, null: str, delimiter: str) -> list:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        columns = set(reader.fieldnames or [])
        leaked = columns & FORBIDDEN_COLUMNS
        if leaked:
            raise ExportError(f"{path.name} holds credential columns {sorted(leaked)}; export only the given SELECT.")
        return [{key: (None if value == null else value) for key, value in row.items()} for row in reader]


def _when(raw: str) -> str:
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ExportError("--exported-at must be an ISO date-time, e.g. 2026-11-07T08:00:00Z") from None
    if moment.tzinfo is None:
        raise ExportError("--exported-at needs a time zone (use Z for UTC)")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assemble(src_dir, uploads_dir, *, exported_at: str, table_prefix="wp_", null="NULL", delimiter=",",
             theme_version="") -> dict:
    src = Path(src_dir)
    wanted = [*TABLES, *EXTRA_FILES]
    missing = [f"{name}.csv" for name in wanted if not (src / f"{name}.csv").is_file()]
    if missing:
        raise ExportError("missing CSV file(s): " + ", ".join(missing))
    tables = {name: _read(src / f"{name}.csv", null, delimiter) for name in TABLES}

    meta = defaultdict(dict)
    for row in _read(src / "usermeta.csv", null, delimiter):
        meta[str(row.get("user_id"))][row.get("meta_key")] = row.get("meta_value") or ""
    users = []
    for row in _read(src / "users.csv", null, delimiter):
        user_meta = meta.get(str(row.get("ID")), {})
        users.append({
            "id": int(row["ID"]),
            "login": row.get("user_login") or "",
            "email": row.get("user_email") or "",
            "display_name": row.get("display_name") or "",
            "registered": row.get("user_registered") or "",
            "roles": _ROLE.findall(user_meta.get(f"{table_prefix}capabilities", "")),
            **{key: user_meta.get(f"joyokr_{key}", "") for key in PROFILE_KEYS},
        })

    options = {row.get("option_name"): row.get("option_value") or "" for row in _read(src / "options.csv", null,
                                                                                         delimiter)}
    root = Path(uploads_dir).resolve() if uploads_dir else None
    attachments = []
    for row in _read(src / "attachments.csv", null, delimiter):
        relative = (row.get("attached_file") or "").replace("\\", "/")
        item = {"id": int(row["ID"]), "relative_path": relative, "mime_type": row.get("post_mime_type") or "",
                "original_name": PurePosixPath(relative).name if relative else "", "size": None, "sha256": "",
                "missing": True}
        parts = PurePosixPath(relative).parts
        if root is not None and relative and not relative.startswith("/") and ".." not in parts:
            path = (root / relative).resolve()
            if path.is_relative_to(root) and path.is_file():
                content = path.read_bytes()
                item.update(size=len(content), sha256=hashlib.sha256(content).hexdigest(), missing=False)
        attachments.append(item)

    return {
        "format": FORMAT,
        "format_version": 1,
        "source": {
            "site_url": (options.get("home") or options.get("siteurl") or "").rstrip("/") + "/",
            "theme_version": theme_version,
            "schema_version": options.get("joyokr_schema_version", ""),
            "table_prefix": table_prefix,
            "exported_at": _when(exported_at),
            "timezone_string": options.get("timezone_string", ""),
            "gmt_offset": options.get("gmt_offset", ""),
            "uploads_basedir": "",
            "export_method": "database_tool_csv",
        },
        "options": {name: options.get(f"joyokr_{name}", "") for name in SAFE_OPTIONS},
        "users": users,
        "tables": tables,
        "attachments": attachments,
        "summary": {
            "tables_found": len(TABLES),
            "counts": {name: len(rows) for name, rows in tables.items()},
            "users": len(users),
            "attachments": len(attachments),
            "missing_files": sum(1 for item in attachments if item["missing"]),
        },
    }
