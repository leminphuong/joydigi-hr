"""Source checks run before any import writes a row (M3).

Every finding names its table and WordPress id and has a severity:

* ``error``   — the import is refused (dry run and ``--apply`` alike), nothing
  is written. The source is never corrected and no record is silently
  dropped: a person fixes the source (and exports again) or decides.
* ``warning`` — imported, but an administrator must review it (an empty
  owner decided in the mapping, progress that differs from the formula...).
* ``info``    — no effect on the imported data (an unsupported history type,
  a notification for someone who is not migrated...).

Without a mapping (``inspect``) the identity checks that need one are skipped.
"""

from __future__ import annotations

import datetime
import hashlib
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from okr.legacy_import.export import TABLES, Export, ExportError, ref, text, to_date, to_datetime, to_decimal
from okr.models.choices import (
    ActivityAction,
    ApprovalStatus,
    CycleStatus,
    CycleType,
    EvidenceType,
    IdentityStatus,
    Priority,
    RecordStatus,
    Scope,
)
from okr.services import calculator

ERROR, WARNING, INFO = "error", "warning", "info"
SOURCE_APPROVAL = {ApprovalStatus.DRAFT, ApprovalStatus.SUBMITTED, ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
LIVE_REVIEW = {ApprovalStatus.SUBMITTED, ApprovalStatus.APPROVED}
ACTIVITY_OBJECTS = {"cycle", "objective", "key_result"}
TOLERANCE = 0.01


def _epoch():
    return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def validate_source(export: Export, mapping=None, uploads_dir=None, *, allow_unmapped=False) -> dict:
    findings = []
    t = export.tables

    def add(severity, code, table, wp_id, detail):
        findings.append({"severity": severity, "code": code, "table": table, "wp_id": wp_id, "detail": detail})

    for warning in export.warnings:
        if "missing from export" in warning:
            add(ERROR, "table_missing", warning.split()[1], None, warning)

    def when(table, row, column):
        try:
            return to_datetime(row.get(column))
        except ExportError:
            add(ERROR, "timestamp_unreadable", table, ref(row.get("id")), f"{column} {row.get(column)!r} is not a date")
            return None

    def enum(table, row, column, choices):
        raw = text(row.get(column)).strip()
        if not raw:
            add(WARNING, "value_empty", table, ref(row.get("id")), f"{column} is empty; the Django default is used")
        elif raw not in choices:
            add(ERROR, "value_unknown", table, ref(row.get("id")), f"{column} {raw!r} is not a known value")

    users = set(export.users)

    def user(table, row, column, *, required=False, label=None, key=None):
        """WordPress user -> mapping decision. Required owners must end up mapped."""
        wp_id, wp_user = (key if key is not None else ref(row.get("id"))), ref(row.get(column))
        label = label or column
        if wp_user is None:
            if required:
                add(ERROR, "owner_missing", table, wp_id, f"{label} is empty")
            return
        if wp_user not in users:
            add(ERROR if required else WARNING, "user_deleted_in_wordpress", table, wp_id,
                f"{label}: WordPress user {wp_user} no longer exists; the link would stay empty")
            return
        if mapping is None or wp_user in mapping.employees:
            return
        status = mapping.status_of(wp_user)
        if status == IdentityStatus.REJECTED and required:
            add(ERROR, "owner_not_verified", table, wp_id,
                f"{label}: WordPress user {wp_user} has no verified HR employee; an owner is required")
        elif status == IdentityStatus.REJECTED:
            add(WARNING, "user_rejected_in_mapping", table, wp_id,
                f"{label}: WordPress user {wp_user} has no HR employee (decided); the link stays empty")
        else:
            # --allow-unmapped (never for the cut-over) only relaxes links that are not required.
            add(WARNING if allow_unmapped and not required else ERROR, "user_not_verified", table, wp_id,
                f"{label}: WordPress user {wp_user} is {status} in the mapping")

    departments = {ref(r.get("id")) for r in t["departments"]}
    teams = {ref(r.get("id")): r for r in t["teams"]}
    cycles = {ref(r.get("id")): r for r in t["cycles"]}
    objectives = {ref(r.get("id")): r for r in t["objectives"]}
    key_results = {ref(r.get("id")): r for r in t["key_results"]}
    checkins = {ref(r.get("id")): r for r in t["checkins"]}
    comments = {ref(r.get("id")): r for r in t["comments"]}

    # -- organisation ----------------------------------------------------------------
    for row in t["departments"]:
        user("departments", row, "manager_user_id", label="manager")
    for wp_id, row in teams.items():
        if ref(row.get("department_id")) and ref(row.get("department_id")) not in departments:
            add(WARNING, "team_department_missing", "teams", wp_id, "department not in export; left empty")
        enum("teams", row, "status", RecordStatus.values)
        user("teams", row, "manager_user_id", label="manager")
        when("teams", row, "created_at")
    for row in t["team_members"]:
        key = f"{row.get('team_id')}:{row.get('user_id')}"
        if ref(row.get("team_id")) not in teams:
            add(ERROR, "team_missing", "team_members", key, "membership of a team that is not in the export")
        user("team_members", row, "user_id", label="member", key=key)

    # -- cycles ----------------------------------------------------------------------
    for wp_id, row in cycles.items():
        enum("cycles", row, "cycle_type", CycleType.values)
        enum("cycles", row, "status", CycleStatus.values)
        try:
            start, end = to_date(row.get("start_date")), to_date(row.get("end_date"))
        except ExportError:
            start = end = None
        if start is None or end is None or end < start:
            add(ERROR, "cycle_dates_invalid", "cycles", wp_id, "start/end date missing, unreadable or reversed")
        parent = ref(row.get("parent_id"))
        if parent and parent not in cycles:
            add(ERROR, "cycle_parent_missing", "cycles", wp_id, f"parent cycle {parent} not in export")
        when("cycles", row, "created_at")
    for wp_id in cycles:
        seen, current = set(), wp_id
        while current is not None and current in cycles:
            if current in seen:
                add(ERROR, "cycle_parent_loop", "cycles", wp_id, "cycle parents form a loop")
                break
            seen.add(current)
            current = ref(cycles[current].get("parent_id"))

    def cycle_ancestors(cycle_id):
        out, current = set(), cycle_id
        while current is not None and current in cycles and current not in out:
            out.add(current)
            current = ref(cycles[current].get("parent_id"))
        return out

    # -- objectives ------------------------------------------------------------------
    for wp_id, row in objectives.items():
        alive = when("objectives", row, "deleted_at") is None and not text(row.get("deleted_at")).strip()
        when("objectives", row, "created_at")
        cycle = ref(row.get("cycle_id"))
        if cycle not in cycles:
            add(ERROR, "objective_cycle_missing", "objectives", wp_id, f"cycle {row.get('cycle_id')} not in export")
        enum("objectives", row, "scope", Scope.values)
        enum("objectives", row, "priority", Priority.values)
        status = text(row.get("approval_status")).strip()
        if status not in SOURCE_APPROVAL:
            add(ERROR, "approval_status_invalid", "objectives", wp_id, f"approval status {status!r}")
        scope = text(row.get("scope")).strip()
        department = ref(row.get("department_id"))
        if scope == Scope.DEPARTMENT:
            if department not in departments:
                add(ERROR, "scope_target_missing", "objectives", wp_id, "department objective without its department")
            elif mapping is not None and department not in mapping.departments:
                add(ERROR, "department_not_mapped", "objectives", wp_id,
                    f"department objective: department {department} has no verified HR department")
        elif department and mapping is not None and department not in mapping.departments:
            add(WARNING, "department_not_mapped", "objectives", wp_id, f"department {department} left empty")
        if scope == Scope.TEAM and ref(row.get("team_id")) not in teams:
            add(ERROR, "scope_target_missing", "objectives", wp_id, "team objective without its team")
        user("objectives", row, "owner_user_id", required=True, label="owner")
        parent = ref(row.get("parent_objective_id"))
        if parent:
            if parent == wp_id:
                add(ERROR, "objective_parent_loop", "objectives", wp_id, "objective is its own parent")
            elif parent not in objectives:
                add(ERROR, "objective_parent_missing", "objectives", wp_id, f"parent objective {parent} not in export")
            elif alive and ref(objectives[parent].get("cycle_id")) not in cycle_ancestors(cycle):
                add(WARNING, "objective_parent_other_cycle", "objectives", wp_id,
                    f"parent objective {parent} is in a cycle that does not contain this one")
        if not alive:
            add(INFO, "deleted_in_wordpress", "objectives", wp_id, "imported as deleted (kept for history)")
    for wp_id in objectives:
        seen, current = set(), wp_id
        while current is not None and current in objectives:
            if current in seen:
                if current != wp_id or len(seen) > 1:
                    add(ERROR, "objective_parent_loop", "objectives", wp_id, "objective parents form a loop")
                break
            seen.add(current)
            current = ref(objectives[current].get("parent_objective_id"))

    # -- key results, weights, progress ---------------------------------------------
    live_krs = defaultdict(list)
    for wp_id, row in key_results.items():
        alive = when("key_results", row, "deleted_at") is None and not text(row.get("deleted_at")).strip()
        when("key_results", row, "created_at")
        objective = ref(row.get("objective_id"))
        if objective not in objectives:
            add(ERROR, "key_result_objective_missing", "key_results", wp_id, f"objective {row.get('objective_id')} "
                "not in export")
        status = text(row.get("approval_status")).strip()
        if status not in SOURCE_APPROVAL:
            add(ERROR, "approval_status_invalid", "key_results", wp_id, f"approval status {status!r}")
        enum("key_results", row, "priority", Priority.values)
        user("key_results", row, "owner_user_id", required=True, label="owner")
        try:
            to_date(row.get("due_date"))
        except ExportError:
            add(ERROR, "value_unreadable", "key_results", wp_id, "due date is not a date")
        start, target = to_decimal(row.get("start_value")), to_decimal(row.get("target_value"), "100")
        current, progress = to_decimal(row.get("current_value")), to_decimal(row.get("progress"))
        if alive:
            live_krs[objective].append(row)
            computed = calculator.progress(start, target, current)
            if abs(float(computed) - float(progress)) > TOLERANCE:
                add(WARNING, "key_result_progress_vs_formula", "key_results", wp_id,
                    f"progress {progress} but the formula gives {computed}; WordPress value kept")
            if objective in objectives and status == ApprovalStatus.APPROVED and text(
                    objectives[objective].get("approval_status")) != ApprovalStatus.APPROVED:
                add(WARNING, "approval_mismatch", "key_results", wp_id, "approved key result under a "
                    "non-approved objective")
        else:
            add(INFO, "deleted_in_wordpress", "key_results", wp_id, "imported as deleted (kept for history)")

    latest = {}
    for wp_id, row in checkins.items():
        kr = ref(row.get("key_result_id"))
        moment = when("checkins", row, "created_at")
        if kr not in key_results:
            add(ERROR, "checkin_key_result_missing", "checkins", wp_id, f"key result {row.get('key_result_id')} "
                "not in export")
            continue
        user("checkins", row, "user_id", label="author")
        stamp = (moment or _epoch(), wp_id)
        if kr not in latest or stamp > latest[kr][0]:
            latest[kr] = (stamp, row)
    for wp_id, row in key_results.items():
        alive = not text(row.get("deleted_at")).strip()
        current = to_decimal(row.get("current_value"))
        if wp_id in latest:
            last = to_decimal(latest[wp_id][1].get("current_value"))
            if last != current:
                add(ERROR if alive else WARNING, "key_result_vs_latest_checkin", "key_results", wp_id,
                    f"current value {current} but the newest check-in says {last}")
        elif alive and current != to_decimal(row.get("start_value")):
            add(WARNING, "key_result_value_without_checkin", "key_results", wp_id,
                f"current value {current} differs from start without any check-in")

    for wp_id, row in objectives.items():
        if text(row.get("deleted_at")).strip():
            continue
        status = text(row.get("approval_status")).strip()
        krs = live_krs.get(wp_id, [])
        total = sum((to_decimal(kr.get("weight"), "100") for kr in krs), Decimal("0"))
        if status in LIVE_REVIEW:
            if not krs:
                # JOYOKR 1.6.45 only submits/approves an objective with 2-4 key results
                # (JoyOKR_OKR_Service submit/approve); no rule allows one without any.
                add(ERROR, "objective_without_key_results", "objectives", wp_id,
                    f"{status} objective without any key result (JOYOKR requires 2-4)")
            elif not calculator.weight_total_ok(total):
                add(ERROR, "weight_total_invalid", "objectives", wp_id,
                    f"{status} objective: key result weights total {total}, JOYOKR requires 100")
            if krs and not 2 <= len(krs) <= 4:
                add(WARNING, "key_result_count", "objectives", wp_id, f"{len(krs)} key results (JOYOKR asks 2-4)")
            if status == ApprovalStatus.APPROVED and any(
                    text(kr.get("approval_status")) != ApprovalStatus.APPROVED for kr in krs):
                add(WARNING, "approval_mismatch", "objectives", wp_id, "approved objective with key results "
                    "that are not approved")
        elif krs and not calculator.weight_total_ok(total):
            add(WARNING, "weight_total_draft", "objectives", wp_id, f"{status or 'draft'} objective: weights total "
                f"{total} (allowed until it is submitted)")
        if krs:
            pool = krs if status != ApprovalStatus.APPROVED else [
                kr for kr in krs if text(kr.get("approval_status")) == ApprovalStatus.APPROVED]
            weighted = calculator.weighted_progress(
                (to_decimal(kr.get("progress")), to_decimal(kr.get("weight"), "100")) for kr in pool)
            if abs(float(weighted) - float(to_decimal(row.get("progress")))) > TOLERANCE:
                add(WARNING, "objective_progress_vs_key_results", "objectives", wp_id,
                    f"progress {row.get('progress')} but the weighted key results give {weighted}")

    # -- comments ------------------------------------------------------------------------
    for wp_id, row in comments.items():
        kind, target = text(row.get("object_type")), ref(row.get("object_id"))
        pool = objectives if kind == "objective" else key_results if kind == "key_result" else {}
        when("comments", row, "created_at")
        if target not in pool:
            add(ERROR, "comment_target_missing", "comments", wp_id, f"{kind or '?'} {row.get('object_id')} not in export")
        parent = ref(row.get("parent_id"))
        if parent and parent not in comments:
            add(WARNING, "comment_parent_missing", "comments", wp_id, f"parent comment {parent} not in export; "
                "imported as a top-level comment")
        user("comments", row, "user_id", label="author")

    # -- evidence ----------------------------------------------------------------------
    root = Path(uploads_dir).resolve() if uploads_dir else None
    files = {"checked": 0, "ok": 0}
    for row in t["evidence"]:
        wp_id = ref(row.get("id"))
        when("evidence", row, "created_at")
        if ref(row.get("key_result_id")) not in key_results:
            add(ERROR, "evidence_key_result_missing", "evidence", wp_id, f"key result {row.get('key_result_id')} "
                "not in export")
        checkin = ref(row.get("checkin_id"))
        if checkin and checkin not in checkins:
            add(WARNING, "evidence_checkin_missing", "evidence", wp_id, f"check-in {checkin} not in export; "
                "link left empty")
        user("evidence", row, "user_id", label="author")
        attachment_id = ref(row.get("attachment_id"))
        kind = text(row.get("evidence_type")).strip()
        if kind and kind not in EvidenceType.values:
            add(ERROR, "value_unknown", "evidence", wp_id, f"evidence_type {kind!r} is not a known value")
            continue
        if kind != EvidenceType.FILE and not attachment_id:
            if not text(row.get("url")).strip().startswith(("http://", "https://")):
                add(ERROR, "evidence_link_invalid", "evidence", wp_id, "link is not http(s)")
            continue
        _check_file(add, files, export, root, wp_id, attachment_id)

    # -- notifications and history (never block) -------------------------------------
    for row in t["notifications"]:
        wp_id = ref(row.get("id"))
        recipient = ref(row.get("user_id"))
        if recipient not in users or (mapping is not None and recipient not in mapping.employees):
            add(INFO, "notification_not_migrated", "notifications", wp_id, "recipient is not a migrated employee")
        kind, target = text(row.get("reference_type")), ref(row.get("reference_id"))
        if kind in {"objective", "key_result"}:
            pool = objectives if kind == "objective" else key_results
            if target not in pool:
                add(INFO, "notification_reference_missing", "notifications", wp_id, "kept without its link")
        when("notifications", row, "created_at")
    for row in t["activity_log"]:
        wp_id = ref(row.get("id"))
        kind, target = text(row.get("object_type")), ref(row.get("object_id"))
        when("activity_log", row, "created_at")
        if kind not in ACTIVITY_OBJECTS:
            add(INFO, "activity_object_unsupported", "activity_log", wp_id, f"history about {kind or '?'} is not kept")
            continue
        pool = {"cycle": cycles, "objective": objectives, "key_result": key_results}[kind]
        if target not in pool:
            add(WARNING, "activity_target_missing", "activity_log", wp_id, f"{kind} {target} not in export; "
                "entry not imported")
        if text(row.get("action")) not in ActivityAction.values:
            add(INFO, "activity_action_unknown", "activity_log", wp_id,
                f"action {text(row.get('action'))!r} kept as 'updated' with the original name")
        user("activity_log", row, "actor_user_id", label="actor")

    for wp_id, row in export.users.items():
        if "administrator" in (row.get("roles") or []):
            add(INFO, "admin_not_granted", "users", wp_id, "WordPress administrator: OKR admin is never granted "
                "by the import")

    counts = Counter(f["severity"] for f in findings)
    return {
        "counts": {ERROR: counts[ERROR], WARNING: counts[WARNING], INFO: counts[INFO]},
        "codes": dict(sorted(Counter(f"{f['severity']}:{f['code']}" for f in findings).items())),
        "files": files,
        "tables": list(TABLES),
        "findings": findings,
    }


def _check_file(add, files, export, root, wp_id, attachment_id):
    """Same checks the importer applies to a file (presence, size, SHA-256, type)."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from okr.services.engagement import _check_upload

    attachment = export.attachments.get(attachment_id)
    if attachment is None:
        add(ERROR, "evidence_file_missing", "evidence", wp_id, f"attachment {attachment_id} not in export")
        return
    if attachment.get("missing"):
        add(ERROR, "evidence_file_missing", "evidence", wp_id, "the exporter could not find the file in WordPress")
        return
    if root is None:
        add(ERROR, "evidence_file_missing", "evidence", wp_id, "no uploads copy given (--uploads-dir)")
        return
    files["checked"] += 1
    relative = Path(text(attachment.get("relative_path")))
    path = (root / relative).resolve()
    if not text(attachment.get("relative_path")) or relative.is_absolute() or ".." in relative.parts \
            or not path.is_relative_to(root):
        add(ERROR, "evidence_file_missing", "evidence", wp_id, "file path is not inside the uploads copy")
        return
    if not path.is_file():
        add(ERROR, "evidence_file_missing", "evidence", wp_id, f"{relative.as_posix()} not in the uploads copy")
        return
    content = path.read_bytes()
    expected = text(attachment.get("sha256")).lower()
    if not expected:
        add(ERROR, "evidence_checksum_missing", "evidence", wp_id, "the export gives no SHA-256 for this file")
        return
    if hashlib.sha256(content).hexdigest() != expected or (
            attachment.get("size") not in (None, "") and int(attachment["size"]) != len(content)):
        add(ERROR, "evidence_checksum_mismatch", "evidence", wp_id, f"{relative.as_posix()}: size or SHA-256 "
            "differs from the export")
        return
    name = text(attachment.get("original_name")) or path.name
    try:
        _check_upload(SimpleUploadedFile(name, content, content_type=text(attachment.get("mime_type"))))
    except Exception as exc:  # okr.services.errors.OKRError
        fields = getattr(exc, "fields", None) or {}
        add(ERROR, "evidence_file_refused", "evidence", wp_id, f"file refused: {fields.get('file') or exc}")
        return
    files["ok"] += 1
