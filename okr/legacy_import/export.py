"""Reading a JOYOKR export file (``joyokr_export.php``, format version 1).

The file is JSON::

    {"format": "joyokr-export", "format_version": 1,
     "source": {"site_url", "theme_version", "schema_version", "table_prefix",
                "exported_at", "timezone_string", "gmt_offset", "uploads_basedir"},
     "options": {"warning_threshold", "reminder_frequency", "email_reminders",
                 "rating_ranges"},
     "users": [{"id", "login", "email", "display_name", "roles": [],
                "employee_code", "department_id", "manager_id",
                "employment_status"}],
     "tables": {"departments": [...], "teams": [...], ... one list per
                ``joyokr_*`` table, rows exactly as stored},
     "attachments": [{"id", "relative_path", "mime_type", "original_name",
                      "size", "sha256"}]}

No password, session, Telegram or SSO value is ever part of it. JOYOKR
writes its table timestamps in UTC (``current_time('mysql', true)``), so
they are read as UTC here.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

FORMAT = "joyokr-export"
FORMAT_VERSION = 1
TABLES = (
    "departments",
    "teams",
    "team_members",
    "cycles",
    "objectives",
    "key_results",
    "checkins",
    "comments",
    "evidence",
    "notifications",
    "activity_log",
)
# Keys an export must never carry; refusing them keeps a mistaken export
# (e.g. a raw ``wp_users`` dump) out of the database and the reports.
FORBIDDEN_USER_KEYS = {"user_pass", "pass", "password", "user_activation_key", "session_tokens"}


class ExportError(ValueError):
    """The file is not a usable JOYOKR export."""


@dataclass
class Export:
    path: Path
    checksum: str
    source: dict
    options: dict
    users: dict  # wp user id -> user dict
    tables: dict  # table name -> list of rows
    attachments: dict  # attachment id -> dict
    warnings: list = field(default_factory=list)

    def rows(self, table: str) -> list:
        return self.tables.get(table, [])

    def counts(self) -> dict:
        return {name: len(self.rows(name)) for name in TABLES}


def load_export(path) -> Export:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExportError(f"Cannot read export file: {exc}") from exc
    checksum = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"Export is not UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ExportError("Not a JOYOKR export (format must be 'joyokr-export').")
    if data.get("format_version") != FORMAT_VERSION:
        raise ExportError(f"Unsupported export format_version {data.get('format_version')!r}.")
    tables = data.get("tables")
    if not isinstance(tables, dict):
        raise ExportError("Export has no 'tables' object.")
    warnings = []
    clean_tables = {}
    for name in TABLES:
        rows = tables.get(name)
        if rows is None:
            warnings.append(f"table {name} missing from export (treated as empty)")
            rows = []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ExportError(f"Table {name} must be a list of objects.")
        clean_tables[name] = rows
    users = {}
    for user in data.get("users") or []:
        if not isinstance(user, dict) or "id" not in user:
            raise ExportError("Every user needs an 'id'.")
        leaked = FORBIDDEN_USER_KEYS & set(user)
        if leaked:
            raise ExportError(f"Export contains credential fields ({', '.join(sorted(leaked))}); refusing it.")
        users[to_int(user["id"])] = user
    attachments = {}
    for item in data.get("attachments") or []:
        if not isinstance(item, dict) or "id" not in item:
            raise ExportError("Every attachment needs an 'id'.")
        attachments[to_int(item["id"])] = item
    return Export(
        path=path,
        checksum=checksum,
        source=data.get("source") or {},
        options=data.get("options") or {},
        users=users,
        tables=clean_tables,
        attachments=attachments,
        warnings=warnings,
    )


# -- value helpers -------------------------------------------------------------------


def to_int(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ExportError(f"Not an integer: {value!r}") from exc


def ref(value):
    """A foreign key value: ``None`` for empty or 0 (JOYOKR's 'no link')."""
    number = to_int(value)
    return number or None


def to_decimal(value, default="0"):
    if value in (None, ""):
        value = default
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ExportError(f"Not a number: {value!r}") from exc


def to_datetime(value):
    """MySQL ``DATETIME`` (UTC) or ISO text → aware UTC datetime; zero dates → None."""
    if value in (None, "") or str(value).startswith("0000-00-00"):
        return None
    text = str(value).strip().replace("T", " ")
    if text.endswith("Z"):
        text = text[:-1]
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ExportError(f"Not a date-time: {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(datetime.timezone.utc)


def to_date(value):
    if value in (None, "") or str(value).startswith("0000-00-00"):
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ExportError(f"Not a date: {value!r}") from exc


def text(value) -> str:
    return "" if value is None else str(value)
