"""Provenance of imported rows: what the importer itself last wrote.

Every succeeded ``--apply`` batch keeps, in ``stats["baseline"]``, a
snapshot of each row it created, updated or confirmed (the protected fields
below, in a canonical form, plus the row's ``pk``). The next import compares
every existing row three ways:

* **imported**  — that snapshot (the value WordPress put there last time);
* **django**    — the row as it is now;
* **wordpress** — what this export would write.

``django != imported`` means the row was changed in Django after the import
(Web or App edit, approval, check-in, deletion...). Whatever the export says,
the importer cannot know which value is right, so it reports a conflict and
the whole run is refused. The decision never rests on timestamps: it rests on
the stored snapshot, on the rows Django created around the imported row
(``legacy_wp_id`` empty) and on the batch history.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

from django.db import models

from okr.models import (
    OKRActivity,
    OKRCheckin,
    OKRComment,
    OKRCycle,
    OKRDepartmentRole,
    OKREvidence,
    OKRKeyResult,
    OKRMigrationBatch,
    OKRNotification,
    OKRObjective,
    OKRRole,
    OKRSettings,
    OKRTeam,
    OKRTeamMember,
)
from okr.models.choices import BatchMode, BatchStatus

# Fields the importer writes and must never silently overwrite. Rows of the
# other tables are only ever created by the importer (history, files,
# memberships, roles): for them the snapshot only proves the row existed.
FIELDS = {
    "cycles": ("name", "cycle_type", "start_date", "end_date", "status", "parent"),
    "teams": ("name", "description", "department", "manager", "status"),
    "objectives": ("cycle", "parent", "scope", "department", "team", "owner", "title", "description", "weight",
                   "priority", "approval_status", "progress", "deleted_at"),
    "key_results": ("objective", "owner", "title", "description", "unit", "start_value", "current_value",
                    "target_value", "weight", "priority", "due_date", "approval_status", "progress", "deleted_at"),
    "comments": ("objective", "key_result", "parent", "author", "content", "deleted_at"),
    "notifications": ("is_read",),
    "department_roles": ("manager",),
    "settings": ("warning_threshold", "reminder_frequency", "email_reminders", "rating_ranges"),
}

MODELS = {
    "cycles": OKRCycle,
    "teams": OKRTeam,
    "team_members": OKRTeamMember,
    "objectives": OKRObjective,
    "key_results": OKRKeyResult,
    "checkins": OKRCheckin,
    "comments": OKRComment,
    "evidence": OKREvidence,
    "notifications": OKRNotification,
    "activity_log": OKRActivity,
    "roles": OKRRole,
    "department_roles": OKRDepartmentRole,
    "settings": OKRSettings,
}

# Rows Django created (no legacy id) that hang off an imported row.
_DEPENDENTS = {
    "cycles": ((OKRObjective, "cycle", "objectives"), (OKRCycle, "parent", "cycles")),
    "teams": ((OKRObjective, "team", "objectives"),),
    "objectives": ((OKRKeyResult, "objective", "key_results"), (OKRObjective, "parent", "objectives"),
                   (OKRComment, "objective", "comments"), (OKRActivity, "objective", "activity")),
    "key_results": ((OKRCheckin, "key_result", "checkins"), (OKREvidence, "key_result", "evidence"),
                    (OKRComment, "key_result", "comments"), (OKRActivity, "key_result", "activity")),
    "comments": ((OKRComment, "parent", "comments"),),
}


def canon(value):
    """One comparable, JSON-safe form per value (Decimal 75 == 75.0000)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, models.Model):
        return value.pk
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value.is_finite() else str(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt_timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def _attname(obj, name):
    return obj._meta.get_field(name).attname


def snapshot(obj, table) -> dict:
    """The protected fields of a saved row, plus its pk."""
    data = {"pk": obj.pk}
    for name in FIELDS.get(table, ()):
        data[name] = canon(getattr(obj, _attname(obj, name)))
    return data


def planned(values: dict, table) -> dict:
    """The protected fields the importer is about to write."""
    return {name: canon(values[name]) for name in FIELDS.get(table, ()) if name in values}


def differs(a: dict, b: dict, names) -> list:
    return [name for name in names if name in a and name in b and a[name] != b[name]]


def restore_values(model, data: dict) -> dict:
    """Snapshot back to model values (used by rollback to undo an update)."""
    out = {}
    for name, value in data.items():
        if name == "pk":
            continue
        field = model._meta.get_field(name)
        if isinstance(field, models.JSONField):
            value = json.loads(value) if isinstance(value, str) else value
        else:
            value = field.to_python(value)
        out[field.attname] = value
    return out


def native_dependents(table, obj) -> dict:
    """``{"checkins": 2, ...}``: rows created in Django that point at ``obj``."""
    found = {}
    for model, field, label in _DEPENDENTS.get(table, ()):
        count = model._base_manager.filter(**{field: obj, "legacy_wp_id__isnull": True}).count()
        if count:
            found[label] = found.get(label, 0) + count
    return found


def apply_batches(company_id):
    """Succeeded, not rolled back ``--apply`` batches of this company, oldest first."""
    batches = OKRMigrationBatch.objects.filter(mode=BatchMode.APPLY, status=BatchStatus.SUCCEEDED).order_by("id")
    return [batch for batch in batches if (batch.stats or {}).get("company_id") == company_id]


def load_baselines(company_id):
    """Merged snapshots (later batches win) and the newest export time applied."""
    baselines, latest_export, ids = {}, None, []
    for batch in apply_batches(company_id):
        stats = batch.stats or {}
        ids.append(batch.pk)
        for table, rows in (stats.get("baseline") or {}).items():
            baselines.setdefault(table, {}).update(rows)
        exported = parse_time(stats.get("exported_at"))
        if exported is not None and (latest_export is None or exported > latest_export):
            latest_export = exported
    return baselines, latest_export, ids


def parse_time(raw):
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)


def short(value, limit=80):
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1] + "…"
    return value
