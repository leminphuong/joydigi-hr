"""Undo one applied import batch: delete the rows it created and put back
the values of the rows it updated (a delta import).

Refused, with nothing changed, when:

* a newer applied batch of the same company exists (undo the newest first);
* a row the batch created or updated was changed in Django since (its
  snapshot in ``stats["baseline"]`` no longer matches) — undoing would throw
  that Django change away;
* a created row is still referenced by data added in Django afterwards
  (a new check-in on an imported key result, say): Django's PROTECT stops
  the delete and the whole rollback is rolled back.

In those cases only a database (+ evidence files) restore can go back.
Batches written before the conflict guard have no snapshots: their updates
are not reverted and the result says so.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from okr.legacy_import.provenance import FIELDS, MODELS, differs, restore_values, snapshot
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
from okr.models.choices import BatchMode, BatchStatus

# Children before parents.
ORDER = (
    ("notifications", OKRNotification),
    ("activity_log", OKRActivity),
    ("evidence", OKREvidence),
    ("comments", OKRComment),
    ("checkins", OKRCheckin),
    ("key_results", OKRKeyResult),
    ("objectives", OKRObjective),
    ("team_members", OKRTeamMember),
    ("teams", OKRTeam),
    ("department_roles", OKRDepartmentRole),
    ("role_events", OKRRoleEvent),
    ("roles", OKRRole),
    ("settings", OKRSettings),
    ("identity_map", OKRIdentityMap),
    ("cycles", OKRCycle),
)
SELF_PARENTS = {"comments": OKRComment, "objectives": OKRObjective, "cycles": OKRCycle}
# A notification read in Django after the import loses nothing worth keeping when it is undone.
NOT_GUARDED = {"notifications"}


class RollbackRefused(Exception):
    pass


def _changed_since(batch) -> list:
    """Rows this batch created or updated that no longer look as it left them."""
    stats = batch.stats or {}
    created = {table: set(ids) for table, ids in (stats.get("created") or {}).items()}
    before = stats.get("before") or {}
    changed = []
    for table, rows in (stats.get("baseline") or {}).items():
        model = MODELS.get(table)
        if model is None or table in NOT_GUARDED:
            continue
        for key, snap in rows.items():
            if snap.get("pk") not in created.get(table, set()) and key not in before.get(table, {}):
                continue  # only confirmed by this batch, not written by it
            obj = model._base_manager.filter(pk=snap.get("pk")).first()
            if obj is None:
                changed.append(f"{table}:{key} (deleted)")
                continue
            fields = differs(snapshot(obj, table), snap, FIELDS.get(table, ()))
            if fields:
                changed.append(f"{table}:{key} ({', '.join(fields)})")
    return changed


def rollback_batch(batch_id: int, *, apply=False, operator=None) -> dict:
    batch = OKRMigrationBatch.objects.filter(pk=batch_id).first()
    if batch is None:
        raise RollbackRefused(f"Batch {batch_id} does not exist.")
    if batch.mode != BatchMode.APPLY or batch.status != BatchStatus.SUCCEEDED:
        raise RollbackRefused(f"Batch {batch_id} is {batch.mode}/{batch.status}; only a succeeded apply can be undone.")
    stats = batch.stats or {}
    company_id = stats.get("company_id")
    newer = [
        other.pk for other in OKRMigrationBatch.objects.filter(
            mode=BatchMode.APPLY, status=BatchStatus.SUCCEEDED, pk__gt=batch.pk
        ).order_by("pk")
        if (other.stats or {}).get("company_id") == company_id
    ]
    if newer:
        raise RollbackRefused(f"Newer import batch(es) {newer} must be rolled back first (newest first).")
    changed = _changed_since(batch)
    if changed:
        raise RollbackRefused(
            "Rows of this batch were changed in Django since the import: " + ", ".join(changed[:20])
            + (" …" if len(changed) > 20 else "") + ". Undoing would discard those changes; restore the "
            "database and evidence-file backup instead."
        )
    created = stats.get("created", {})
    before = stats.get("before") or {}
    counts = stats.get("stats", {})
    # Updates without a snapshot to go back to (batches older than the guard; the identity map).
    not_reverted = {
        table: c.get("updated", 0) for table, c in counts.items()
        if c.get("updated") and table not in before
    }
    files = []
    deleted = {}
    reverted = {}
    try:
        with transaction.atomic():
            for table, rows in before.items():
                model = MODELS.get(table)
                if model is None:
                    continue
                for snap in rows.values():
                    model._base_manager.filter(pk=snap["pk"]).update(**restore_values(model, snap))
                reverted[table] = len(rows)
            for table, model in SELF_PARENTS.items():
                ids = created.get(table) or []
                if ids:
                    model._base_manager.filter(pk__in=ids).update(parent=None)
            for table, model in ORDER:
                ids = created.get(table) or []
                if not ids:
                    continue
                if table == "evidence":
                    files += [
                        name for name in model._base_manager.filter(pk__in=ids).values_list("file", flat=True) if name
                    ]
                if table == "identity_map":
                    OKRIdentityMap.objects.filter(pk__in=ids).update(batch=None)
                deleted[table], _ = model._base_manager.filter(pk__in=ids).delete()
            batch.status = BatchStatus.ROLLED_BACK
            batch.notes = (batch.notes + "\n" if batch.notes else "") + f"rolled back {timezone.now().isoformat()}"
            batch.save(update_fields=["status", "notes", "updated_at"])
            OKRMigrationBatch.objects.create(
                source=batch.source,
                source_version=batch.source_version,
                source_checksum=batch.source_checksum,
                mode=BatchMode.ROLLBACK,
                status=BatchStatus.SUCCEEDED,
                started_at=timezone.now(),
                finished_at=timezone.now(),
                started_by=operator,
                stats={"rolled_back_batch": batch.pk, "company_id": company_id, "deleted": deleted,
                       "reverted_updates": reverted, "not_reverted_updates": not_reverted},
            )
            if not apply:
                transaction.set_rollback(True)
    except ProtectedError as exc:
        blockers = sorted({f"{obj._meta.label}#{obj.pk}" for obj in list(exc.protected_objects)[:20]})
        raise RollbackRefused(
            "Rows created by this batch are referenced by newer data: " + ", ".join(blockers)
            + ". Restore the database backup instead."
        ) from exc
    if apply:
        from okr.storage import evidence_storage

        for name in files:
            try:
                evidence_storage().delete(name)
            except OSError:
                pass
    return {
        "mode": "apply" if apply else "dry_run",
        "batch_id": batch.pk,
        "deleted": deleted,
        "reverted_updates": reverted,
        "files": len(files),
        "not_reverted_updates": not_reverted,
    }
