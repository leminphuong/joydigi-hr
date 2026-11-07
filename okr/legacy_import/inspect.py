"""Check a JOYOKR export before any import: tables, counts, links, history.

Pure file check — it needs no database and writes nothing. With an uploads
directory it also verifies every evidence file (presence, size, SHA-256).
The report holds ids and counts only, never names, e-mails or contents.
"""

from __future__ import annotations

import datetime
import hashlib
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from okr.legacy_import.export import TABLES, Export, ExportError, ref, text, to_date, to_datetime, to_decimal

WEIGHT_TOLERANCE = Decimal("0.01")


def _ids(rows):
    return {ref(row.get("id")) for row in rows if ref(row.get("id")) is not None}


def _alive(row):
    return to_datetime(row.get("deleted_at")) is None


def _progress(start, target, current):
    start, target, current = float(start), float(target), float(current)
    if start == target:
        return 100.0 if current == target else 0.0
    return round(max(0.0, min(100.0, (current - start) / (target - start) * 100.0)), 2)


def inspect_export(export: Export, uploads_dir=None) -> dict:
    t = export.tables
    users = set(export.users)
    problems = defaultdict(list)  # check name -> list of wp ids (capped in summary)
    bad_timestamps = Counter()

    def check_time(table, row, *columns):
        for column in columns:
            try:
                to_datetime(row.get(column))
            except ExportError:
                bad_timestamps[f"{table}.{column}"] += 1

    def user_ref(table, row, column):
        wp_user = ref(row.get(column))
        if wp_user is not None and wp_user not in users:
            problems[f"{table}.{column}: user missing from export"].append(ref(row.get("id")) or row.get("team_id"))

    departments, teams, cycles = _ids(t["departments"]), _ids(t["teams"]), _ids(t["cycles"])
    objectives = {ref(r.get("id")): r for r in t["objectives"]}
    key_results = {ref(r.get("id")): r for r in t["key_results"]}
    checkins = {ref(r.get("id")): r for r in t["checkins"]}
    comments = _ids(t["comments"])

    for row in t["departments"]:
        user_ref("departments", row, "manager_user_id")
    for row in t["teams"]:
        if ref(row.get("department_id")) and ref(row.get("department_id")) not in departments:
            problems["teams.department_id: department missing"].append(ref(row.get("id")))
        user_ref("teams", row, "manager_user_id")
    for row in t["team_members"]:
        if ref(row.get("team_id")) not in teams:
            problems["team_members.team_id: team missing"].append(row.get("team_id"))
        user_ref("team_members", row, "user_id")

    cycle_status = Counter()
    for row in t["cycles"]:
        cycle_status[text(row.get("status"))] += 1
        if ref(row.get("parent_id")) and ref(row.get("parent_id")) not in cycles:
            problems["cycles.parent_id: parent missing"].append(ref(row.get("id")))
        try:
            if to_date(row.get("start_date")) is None or to_date(row.get("end_date")) is None:
                problems["cycles: missing dates"].append(ref(row.get("id")))
        except ExportError:
            problems["cycles: unreadable dates"].append(ref(row.get("id")))
        check_time("cycles", row, "created_at", "updated_at")

    scope = Counter()
    approval = Counter()
    for wp_id, row in objectives.items():
        scope[text(row.get("scope"))] += 1
        approval[text(row.get("approval_status")) if _alive(row) else "deleted"] += 1
        if ref(row.get("cycle_id")) not in cycles:
            problems["objectives.cycle_id: cycle missing"].append(wp_id)
        parent = ref(row.get("parent_objective_id"))
        if parent and parent not in objectives:
            problems["objectives.parent_objective_id: parent missing"].append(wp_id)
        if text(row.get("scope")) == "department" and ref(row.get("department_id")) not in departments:
            problems["objectives: department scope without a known department"].append(wp_id)
        if text(row.get("scope")) == "team" and ref(row.get("team_id")) not in teams:
            problems["objectives: team scope without a known team"].append(wp_id)
        user_ref("objectives", row, "owner_user_id")
        check_time("objectives", row, "created_at", "updated_at", "deleted_at")
    for wp_id in objectives:  # parent loops
        seen, current = set(), wp_id
        while current is not None and current in objectives:
            if current in seen:
                problems["objectives: parent loop"].append(wp_id)
                break
            seen.add(current)
            current = ref(objectives[current].get("parent_objective_id"))

    weights = defaultdict(Decimal)
    kr_count = Counter()
    progress_mismatch = []
    for wp_id, row in key_results.items():
        objective_id = ref(row.get("objective_id"))
        if objective_id not in objectives:
            problems["key_results.objective_id: objective missing"].append(wp_id)
        user_ref("key_results", row, "owner_user_id")
        check_time("key_results", row, "created_at", "updated_at", "deleted_at")
        if _alive(row):
            weights[objective_id] += to_decimal(row.get("weight"), "100")
            kr_count[objective_id] += 1
            computed = _progress(to_decimal(row.get("start_value")), to_decimal(row.get("target_value"), "100"),
                                 to_decimal(row.get("current_value")))
            if abs(computed - float(to_decimal(row.get("progress")))) > 0.01:
                progress_mismatch.append(wp_id)
    weight_off, count_off = [], []
    for wp_id, row in objectives.items():
        if not _alive(row) or text(row.get("approval_status")) not in {"submitted", "approved"}:
            continue
        if abs(weights[wp_id] - Decimal("100")) > WEIGHT_TOLERANCE:
            weight_off.append(wp_id)
        if not 2 <= kr_count[wp_id] <= 4:
            count_off.append(wp_id)

    latest = {}
    for wp_id, row in checkins.items():
        kr = ref(row.get("key_result_id"))
        if kr not in key_results:
            problems["checkins.key_result_id: key result missing"].append(wp_id)
            continue
        user_ref("checkins", row, "user_id")
        check_time("checkins", row, "created_at")
        moment = to_datetime(row.get("created_at")) or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        if kr not in latest or (moment, wp_id) > latest[kr][0]:
            latest[kr] = ((moment, wp_id), row)
    checkin_mismatch = [
        kr for kr, (_, row) in latest.items()
        if to_decimal(row.get("current_value")) != to_decimal(key_results[kr].get("current_value"))
    ]

    comment_types = Counter()
    for row in t["comments"]:
        wp_id, kind, target = ref(row.get("id")), text(row.get("object_type")), ref(row.get("object_id"))
        comment_types[kind] += 1
        pool = objectives if kind == "objective" else key_results if kind == "key_result" else {}
        if target not in pool:
            problems["comments: target missing"].append(wp_id)
        if ref(row.get("parent_id")) and ref(row.get("parent_id")) not in comments:
            problems["comments.parent_id: parent missing"].append(wp_id)
        user_ref("comments", row, "user_id")
        check_time("comments", row, "created_at", "updated_at")

    evidence_types = Counter()
    files = {"checked": 0, "ok": 0, "missing_on_disk": [], "checksum_mismatch": [], "size_mismatch": [],
             "marked_missing_by_exporter": [], "not_in_export": [], "outside_uploads": []}
    root = Path(uploads_dir).resolve() if uploads_dir else None
    for row in t["evidence"]:
        wp_id = ref(row.get("id"))
        attachment_id = ref(row.get("attachment_id"))
        kind = "file" if text(row.get("evidence_type")) == "file" or attachment_id else "url"
        evidence_types[kind] += 1
        if ref(row.get("key_result_id")) not in key_results:
            problems["evidence.key_result_id: key result missing"].append(wp_id)
        if ref(row.get("checkin_id")) and ref(row.get("checkin_id")) not in checkins:
            problems["evidence.checkin_id: check-in missing"].append(wp_id)
        user_ref("evidence", row, "user_id")
        if kind == "url":
            if not text(row.get("url")).strip().startswith(("http://", "https://")):
                problems["evidence: link is not http(s)"].append(wp_id)
            continue
        attachment = export.attachments.get(attachment_id)
        if attachment is None:
            files["not_in_export"].append(wp_id)
            continue
        if attachment.get("missing"):
            files["marked_missing_by_exporter"].append(wp_id)
        if root is None:
            continue
        files["checked"] += 1
        relative = Path(text(attachment.get("relative_path")))
        path = (root / relative).resolve()
        if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(root):
            files["outside_uploads"].append(wp_id)
            continue
        if not path.is_file():
            files["missing_on_disk"].append(wp_id)
            continue
        content = path.read_bytes()
        if attachment.get("size") not in (None, "") and int(attachment["size"]) != len(content):
            files["size_mismatch"].append(wp_id)
        expected = text(attachment.get("sha256")).lower()
        if expected and hashlib.sha256(content).hexdigest() != expected:
            files["checksum_mismatch"].append(wp_id)
            continue
        files["ok"] += 1

    notification_refs = Counter()
    for row in t["notifications"]:
        wp_id = ref(row.get("id"))
        user_ref("notifications", row, "user_id")
        kind, target = text(row.get("reference_type")), ref(row.get("reference_id"))
        if kind in {"objective", "key_result"}:
            pool = objectives if kind == "objective" else key_results
            notification_refs["target_present" if target in pool else "target_missing"] += 1
        check_time("notifications", row, "created_at")

    history = Counter()
    for row in t["activity_log"]:
        history[f"{text(row.get('object_type'))}:{text(row.get('action'))}"] += 1
        user_ref("activity_log", row, "actor_user_id")
        check_time("activity_log", row, "created_at")

    roles = Counter(role for user in export.users.values() for role in (user.get("roles") or []))
    return {
        "export": {"sha256": export.checksum, "source": export.source, "warnings": export.warnings},
        "tables_present": [name for name in TABLES if name in export.tables and not any(
            name in warning for warning in export.warnings)],
        "counts": {**export.counts(), "users": len(export.users), "attachments": len(export.attachments)},
        "users": {"roles": dict(roles),
                  "with_employee_code": sum(1 for u in export.users.values() if text(u.get("employee_code")).strip())},
        "cycles": {"status": dict(cycle_status)},
        "objectives": {"scope": dict(scope), "approval_status": dict(approval),
                       "deleted": sum(1 for r in objectives.values() if not _alive(r)),
                       "weight_total_not_100": weight_off, "kr_count_not_2_to_4": count_off},
        "key_results": {"deleted": sum(1 for r in key_results.values() if not _alive(r)),
                        "progress_differs_from_formula": progress_mismatch,
                        "current_value_differs_from_latest_checkin": checkin_mismatch},
        "comments": {"object_type": dict(comment_types)},
        "evidence": {"type": dict(evidence_types), "files": files},
        "notifications": {"references": dict(notification_refs)},
        "approval_history": {key: value for key, value in history.items()
                             if key.split(":")[1] in {"submitted", "approved", "rejected", "deleted"}},
        "activity": dict(history),
        "timezone": {
            "tables": "UTC (JOYOKR writes current_time('mysql', true))",
            "site_timezone": text(export.source.get("timezone_string")) or text(export.source.get("gmt_offset")),
            "unreadable_timestamps": dict(bad_timestamps),
        },
        "problems": {name: sorted(set(map(str, ids))) for name, ids in sorted(problems.items())},
    }
