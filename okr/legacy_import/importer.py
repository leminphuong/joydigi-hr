"""Import a JOYOKR export into the Django OKR tables.

Rules (``okr/docs/wp_migration.md``):

* Dry run unless ``apply=True``: everything runs inside one transaction
  that is rolled back, files are only checked, never written.
* Idempotent: a row is found by ``legacy_wp_id``. A second run creates
  nothing; a later export (the final delta) updates changed definitions
  and adds new history rows. Check-ins and activity are history and are
  never changed once imported.
* Rows created in Django (no ``legacy_wp_id``) are never touched.
* Only *verified* identities and departments are used. A record of a
  WordPress user who exists but is not verified blocks the run (unless
  ``allow_unmapped``); a record of a user that no longer exists in
  WordPress keeps an empty owner and is reported.
* No HR employee, department or account is created; no password is read;
  OKR admin rights are never granted — WordPress administrators are only
  listed for a person to grant with ``okr_grant_role``.
* Source check (``validate.py``) before any write, dry run and apply alike:
  any ERROR refuses the run; nothing in the source is corrected or dropped.
  A row the importer still has to leave out while writing (a model refusal,
  a legacy id owned by another company) also refuses the whole run.
* Conflict guard (``provenance.py``): an imported row that was changed in
  Django afterwards (edit, approval, check-in, deletion, revoked role...) is
  never overwritten or brought back. Any such conflict refuses the WHOLE run:
  an ``--apply`` first makes a full dry pass with the same rules and writes
  nothing if it finds one; the writing pass checks again and rolls the whole
  transaction back on any conflict. There is no option to force it.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone

from okr.legacy_import.export import (
    Export,
    ExportError,
    ref,
    text,
    to_date,
    to_datetime,
    to_decimal,
)
from okr.legacy_import.mapping import Mapping
from okr.legacy_import.validate import validate_source
from okr.legacy_import.provenance import (
    FIELDS,
    MODELS,
    canon,
    differs,
    load_baselines,
    native_dependents,
    parse_time,
    planned,
    short,
    snapshot,
)
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
from okr.models.choices import (
    ActivityAction,
    ApprovalStatus,
    BatchMode,
    BatchStatus,
    CycleStatus,
    CycleType,
    EvidenceType,
    IdentityStatus,
    OKRRoleType,
    Priority,
    RecordStatus,
    ReferenceType,
    ReminderFrequency,
    RoleEventAction,
    RoleEventRole,
    Scope,
)
from okr.services import calculator

PROGRESS_TOLERANCE = 0.01


class ImportBlocked(Exception):
    """Preflight found something only a person can resolve; nothing was written."""

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


class _DryRun(Exception):
    """Raised at the end of a dry run to roll the transaction back."""


class _Conflicts(Exception):
    """Raised at the end of a pass that found conflicts: roll everything back."""


def _choice(value, choices, default):
    value = text(value).strip()
    return value if value in choices.values else default


class ImportRun:
    def __init__(
        self,
        export: Export,
        mapping: Mapping,
        company,
        *,
        operator=None,
        uploads_dir=None,
        apply=False,
        import_roles=False,
        import_settings=False,
        allow_unmapped=False,
    ):
        self.export = export
        self.mapping = mapping
        self.company = company
        self.operator = operator
        self.uploads_dir = Path(uploads_dir) if uploads_dir else None
        self.apply = apply
        self.import_roles = import_roles
        self.import_settings = import_settings
        self.allow_unmapped = allow_unmapped
        self.stats = defaultdict(Counter)
        self.issues = []
        self.created = defaultdict(list)
        self.maps = defaultdict(dict)  # table -> wp id -> django object
        self.written_files = []
        self.admin_candidates = []
        self.reconciliation = {}
        # Conflict guard state.
        self.phase = "apply" if apply else "dry_run"
        self.conflicts = []
        self.conflicted = defaultdict(set)  # table -> wp ids already reported
        self.fresh = defaultdict(set)  # table -> wp ids created by this run
        self.updated_keys = defaultdict(set)  # table -> keys updated by this run
        self.before = defaultdict(dict)  # table -> key -> snapshot before this run changed it
        self.touched = defaultdict(dict)  # existence-only rows: table -> key -> object
        self.baselines = {}
        self.baseline_batches = []
        self.validation = None

    # -- reporting -------------------------------------------------------------------

    def issue(self, severity, table, wp_id, detail):
        """``error``: the row was not imported; ``warning``: imported with a
        gap (e.g. empty owner); ``info``: deliberately not imported."""
        self.issues.append({"severity": severity, "table": table, "wp_id": wp_id, "detail": detail})

    def _count(self, table, outcome, obj=None):
        self.stats[table][outcome] += 1
        if outcome == "created" and obj is not None:
            self.created[table].append(obj.pk)

    def report(self, batch=None) -> dict:
        return {
            "mode": "apply" if self.apply else "dry_run",
            "batch_id": batch.pk if batch is not None and self.apply else None,
            "company_id": self.company.pk,
            "export": {
                "file": self.export.path.name,
                "sha256": self.export.checksum,
                "source": self.export.source,
                "counts": self.export.counts(),
                "users": len(self.export.users),
                "warnings": self.export.warnings,
            },
            "stats": {table: dict(counter) for table, counter in sorted(self.stats.items())},
            "issues": self.issues,
            "issue_counts": dict(Counter(item["severity"] for item in self.issues)),
            "phase": self.phase,
            "validation": self.validation,
            "conflicts": self.conflicts,
            "conflict_counts": dict(Counter(item["kind"] for item in self.conflicts)),
            "provenance": {"baseline_batches": self.baseline_batches,
                           "baseline_rows": sum(len(rows) for rows in self.baselines.values())},
            "admin_candidates": self.admin_candidates,
            "reconciliation": self.reconciliation,
        }

    def conflict(self, table, wp_id, kind, detail, *, fields=None, django_data=None):
        """Something only a person can settle: the whole run will be refused."""
        self.conflicted[table].add(wp_id)
        self.conflicts.append({
            "table": table,
            "wp_id": wp_id,
            "kind": kind,
            "detail": detail,
            "fields": {name: {k: short(v) for k, v in values.items()} for name, values in (fields or {}).items()},
            "django_data": django_data or {},
        })
        self._count(table, "conflict")

    # -- identities ------------------------------------------------------------------

    def employee(self, wp_user_id, table, wp_id, role="owner"):
        """The verified employee of a WordPress user, or ``None``."""
        wp_user_id = ref(wp_user_id)
        if wp_user_id is None:
            return None
        employee = self.mapping.employees.get(wp_user_id)
        if employee is not None:
            return employee
        if wp_user_id not in self.export.users:
            self.issue("warning", table, wp_id, f"{role}: WordPress user {wp_user_id} no longer exists; left empty")
        else:
            self.issue(
                "warning",
                table,
                wp_id,
                f"{role}: WordPress user {wp_user_id} is {self.mapping.status_of(wp_user_id)}; left empty",
            )
        return None

    def preflight(self):
        """Users and departments the records need, and whether they are verified."""
        referenced = defaultdict(set)
        user_columns = {
            "teams": ["manager_user_id"],
            "team_members": ["user_id"],
            "objectives": ["owner_user_id"],
            "key_results": ["owner_user_id"],
            "checkins": ["user_id"],
            "comments": ["user_id"],
            "evidence": ["user_id"],
            "notifications": ["user_id"],
            "activity_log": ["actor_user_id"],
            "departments": ["manager_user_id"],
        }
        for table, columns in user_columns.items():
            for row in self.export.rows(table):
                for column in columns:
                    wp_user = ref(row.get(column))
                    if wp_user is not None:
                        referenced[wp_user].add(table)
        # "rejected" is a decision (no HR employee / department for it): the
        # link stays empty. Anything not yet decided blocks the run.
        blocking, gone, declined = [], [], []
        for wp_user, tables in sorted(referenced.items()):
            if wp_user in self.mapping.employees:
                continue
            if wp_user not in self.export.users:
                gone.append({"wp_user_id": wp_user, "tables": sorted(tables)})
            elif self.mapping.status_of(wp_user) == IdentityStatus.REJECTED:
                declined.append({"wp_user_id": wp_user, "tables": sorted(tables)})
            else:
                blocking.append(
                    {"wp_user_id": wp_user, "status": self.mapping.status_of(wp_user), "tables": sorted(tables)}
                )
        departments_needed = set()
        for table in ("teams", "objectives"):
            for row in self.export.rows(table):
                department = ref(row.get("department_id"))
                if department is not None:
                    departments_needed.add(department)
        decided = set(self.mapping.departments) | {
            wp_id for wp_id, row in self.mapping.department_rows.items() if row.get("status") == "rejected"
        }
        unmapped_departments = sorted(departments_needed - decided)
        result = {
            "referenced_users": len(referenced),
            "unverified_users": blocking,
            "rejected_users": declined,
            "deleted_users": gone,
            "unmapped_departments": unmapped_departments,
            "mapping_problems": self.mapping.problems,
        }
        self.reconciliation["preflight"] = result
        self.validation = validate_source(
            self.export, self.mapping, self.uploads_dir, allow_unmapped=self.allow_unmapped
        )
        problems = []
        if self.validation["counts"]["error"]:
            problems.append(f"{self.validation['counts']['error']} source error(s) found before import "
                            "(see validation in the report)")
        if self.mapping.problems:
            problems.append("mapping file has invalid verified rows")
        if (blocking or unmapped_departments) and not self.allow_unmapped:
            problems.append(
                f"{len(blocking)} WordPress user(s) and {len(unmapped_departments)} department(s) "
                "used by OKR records are not verified in the mapping"
            )
        if problems:
            raise ImportBlocked("; ".join(problems), self.report())

    # -- run -------------------------------------------------------------------------

    def run(self) -> dict:
        self.preflight()
        if self.apply:
            self._prescan()
        return self._execute()

    def _prescan(self):
        """Fail closed before writing: a full dry pass with exactly the same
        rules. Any conflict raises ``ImportBlocked`` and nothing is written
        (not even an evidence file)."""
        scan = ImportRun(
            self.export,
            self.mapping,
            self.company,
            uploads_dir=self.uploads_dir,
            apply=False,
            import_roles=self.import_roles,
            import_settings=self.import_settings,
            allow_unmapped=self.allow_unmapped,
        )
        scan.phase = "prescan"
        scan.run()

    def _load_provenance(self):
        self.baselines, latest_export, self.baseline_batches = load_baselines(self.company.pk)
        exported = parse_time(self.export.source.get("exported_at"))
        if latest_export is not None and exported is not None and exported < latest_export:
            self.conflict(
                "export", None, "stale_export",
                f"this export ({exported.isoformat()}) is older than one already applied "
                f"({latest_export.isoformat()}); it would put old WordPress values back",
            )

    def _execute(self) -> dict:
        batch = None
        try:
            with transaction.atomic():
                self._load_provenance()
                batch = OKRMigrationBatch.objects.create(
                    source="joyokr",
                    source_version=text(self.export.source.get("theme_version"))[:40],
                    source_checksum=self.export.checksum,
                    mode=BatchMode.APPLY if self.apply else BatchMode.DRY_RUN,
                    status=BatchStatus.RUNNING,
                    started_at=timezone.now(),
                    started_by=self.operator,
                )
                self._identities(batch)
                self._cycles()
                self._teams()
                self._objectives()
                self._key_results()
                self._checkins()
                self._comments()
                self._evidence()
                self._notifications()
                self._activity()
                if self.import_settings:
                    self._settings()
                self._roles()
                self._reconcile()
                if self.conflicts or self._row_errors():
                    raise _Conflicts()
                batch.status = BatchStatus.SUCCEEDED
                batch.finished_at = timezone.now()
                batch.stats = {
                    "company_id": self.company.pk,
                    "exported_at": text(self.export.source.get("exported_at")),
                    "stats": {table: dict(counter) for table, counter in self.stats.items()},
                    "created": {table: ids for table, ids in self.created.items()},
                    "files": self.written_files,
                    "baseline": self._baseline(),
                    "before": {table: rows for table, rows in self.before.items()},
                }
                batch.reconciliation = json.loads(json.dumps(self.reconciliation, default=str))
                batch.save()
                if not self.apply:
                    raise _DryRun()
        except _DryRun:
            pass
        except _Conflicts:
            self._remove_written_files()
            reasons = []
            if self.conflicts:
                counts = ", ".join(f"{kind}={n}" for kind, n in sorted(Counter(c["kind"] for c in self.conflicts).items()))
                reasons.append(f"{len(self.conflicts)} conflict(s) between Django and this export ({counts})")
            if self._row_errors():
                reasons.append(f"{len(self._row_errors())} row(s) could not be imported (see issues)")
            raise ImportBlocked("; ".join(reasons) + "; nothing was written", self.report()) from None
        except Exception:
            self._remove_written_files()
            raise
        return self.report(batch)

    def _row_errors(self):
        """A row the importer had to leave out (model refusal, id owned by another
        company...). Never a silent drop: the whole run is refused."""
        return [item for item in self.issues if item["severity"] == "error"]

    def _baseline(self) -> dict:
        """Snapshot, read back from the database, of every row this run
        created, updated or confirmed: the provenance the next run checks."""
        out = defaultdict(dict)
        for table, model in MODELS.items():
            rows = {}
            for key, obj in self.maps.get(table, {}).items():
                if obj is not None and obj.pk:
                    rows[obj.pk] = str(key)
            for key, obj in self.touched.get(table, {}).items():
                if obj is not None and obj.pk:
                    rows[obj.pk] = str(key)
            if not rows:
                continue
            for obj in model._base_manager.filter(pk__in=list(rows)):
                out[table][rows[obj.pk]] = snapshot(obj, table)
        return dict(out)

    # -- conflict guard ----------------------------------------------------------------

    def _base(self, table, key):
        return self.baselines.get(table, {}).get(str(key))

    def _remember_before(self, table, key, obj):
        self.before[table].setdefault(str(key), snapshot(obj, table))

    def _guard(self, table, wp_id, existing, values):
        """Fields of ``existing`` this export may change, or ``None`` after a
        conflict was recorded. Three-way: imported snapshot / Django now /
        this export."""
        names = FIELDS.get(table, ())
        current = snapshot(existing, table)
        wanted = planned(values, table)
        base = self._base(table, wp_id)
        dependents = native_dependents(table, existing)
        if base is None:
            # No record of what an import wrote here (older importer, or the
            # row got its legacy id another way): only an exact match is safe.
            diff = differs(wanted, current, wanted)
            if diff:
                self.conflict(
                    table, wp_id, "no_provenance",
                    "row exists in Django without an import snapshot and the export differs",
                    fields={n: {"django": current[n], "wordpress": wanted[n]} for n in diff},
                    django_data=dependents,
                )
                return None
            return []
        changed = differs(current, base, names)
        if changed:
            self.conflict(
                table, wp_id, "changed_in_django",
                "changed in Django after it was imported; the export would overwrite it",
                fields={n: {"imported": base[n], "django": current[n], "wordpress": wanted.get(n, base[n])}
                        for n in changed},
                django_data=dependents,
            )
            return None
        if table == "key_results" and dependents.get("checkins"):
            self.conflict(
                table, wp_id, "django_checkins",
                "Django holds newer check-ins for this key result; its value now comes from that history",
                django_data=dependents,
            )
            return None
        wp_changes = [n for n in wanted if wanted[n] != base.get(n)]
        if wp_changes and dependents:
            self.conflict(
                table, wp_id, "wordpress_change_on_django_data",
                "WordPress changed a row that already has data created in Django",
                fields={n: {"imported": base.get(n), "django": current[n], "wordpress": wanted[n]}
                        for n in wp_changes},
                django_data=dependents,
            )
            return None
        return [n for n in wp_changes if wanted[n] != current[n]]

    def _gone(self, table, key, wp_id=None):
        """An earlier import created this row and Django has since removed it:
        importing it again would undo that removal."""
        if self._base(table, key) is None:
            return False
        self.conflict(
            table, wp_id if wp_id is not None else key, "deleted_in_django",
            "imported earlier, removed in Django since; importing again would bring it back",
        )
        return True

    def _link_parent(self, table, model, obj, wp_id, parent):
        """Second pass for self-parents (cycles, objectives, comments)."""
        wanted = parent.pk if parent else None
        if obj.parent_id == wanted or wp_id in self.conflicted[table]:
            return
        if wp_id not in self.fresh[table]:
            base = self._base(table, wp_id)
            dependents = native_dependents(table, obj)
            if base is None:
                self.conflict(
                    table, wp_id, "no_provenance",
                    "row exists in Django without an import snapshot and the export moves it",
                    fields={"parent": {"django": obj.parent_id, "wordpress": wanted}}, django_data=dependents,
                )
                return
            if dependents:
                self.conflict(
                    table, wp_id, "wordpress_change_on_django_data",
                    "WordPress moved a row that already has data created in Django",
                    fields={"parent": {"imported": base.get("parent"), "django": obj.parent_id, "wordpress": wanted}},
                    django_data=dependents,
                )
                return
            self._remember_before(table, wp_id, obj)
            if str(wp_id) not in self.updated_keys[table]:
                self.updated_keys[table].add(str(wp_id))
                self.stats[table]["updated"] += 1
                if self.stats[table]["unchanged"]:
                    self.stats[table]["unchanged"] -= 1
        model._base_manager.filter(pk=obj.pk).update(parent=parent)
        obj.parent_id = wanted

    def _remove_written_files(self):
        from okr.storage import evidence_storage

        for name in self.written_files:
            try:
                evidence_storage().delete(name)
            except OSError:
                pass

    def _save(self, table, wp_id, obj):
        """Full save with the model's company checks; a refusal skips the row."""
        try:
            with transaction.atomic():
                obj.save()
            return True
        except ValidationError as exc:
            detail = "; ".join(f"{k}: {', '.join(map(str, v))}" for k, v in exc.message_dict.items())
            self.issue("error", table, wp_id, f"refused by model checks: {detail}")
            self._count(table, "skipped")
            return False

    def _upsert(self, model, table, wp_id, values, *, created_at=None, updated_at=None):
        """Create or update the row with ``legacy_wp_id=wp_id`` (definitions only)."""
        existing = model._base_manager.filter(legacy_wp_id=wp_id).first()
        if existing is not None and existing.company_id != self.company.pk:
            self.issue("error", table, wp_id, f"legacy id already used by company {existing.company_id}")
            self._count(table, "skipped")
            return None
        if existing is None:
            if self._gone(table, wp_id):
                return None
            obj = model(company=self.company, legacy_wp_id=wp_id, **values)
            if created_at is not None:
                obj.created_at = created_at
            if not self._save(table, wp_id, obj):
                return None
            self.fresh[table].add(wp_id)
            self._count(table, "created", obj)
        else:
            guarded = self._guard(table, wp_id, existing, values)
            if guarded is None:
                # Keep it mapped so the children are still checked: every
                # conflict of the export is reported in one go.
                self.maps[table][wp_id] = existing
                return existing
            # Fields outside the protected list (none today) follow the export.
            protected = set(FIELDS.get(table, ()))
            changed = guarded + [
                name for name, value in values.items()
                if name not in protected and canon(getattr(existing, name)) != canon(value)
            ]
            if not changed:
                self._count(table, "unchanged")
                self.maps[table][wp_id] = existing
                return existing
            self._remember_before(table, wp_id, existing)
            for name in changed:
                setattr(existing, name, values[name])
            if not self._save(table, wp_id, existing):
                return None
            obj = existing
            self.updated_keys[table].add(str(wp_id))
            self._count(table, "updated")
        if updated_at is not None and hasattr(model, "updated_at"):
            model._base_manager.filter(pk=obj.pk).update(updated_at=updated_at)
        self.maps[table][wp_id] = obj
        return obj

    def _append(self, model, table, wp_id, values, created_at):
        """History rows: created once, never updated."""
        existing = model._base_manager.filter(legacy_wp_id=wp_id).first()
        if existing is not None:
            if existing.company_id != self.company.pk:
                self.issue("error", table, wp_id, f"legacy id already used by company {existing.company_id}")
                self._count(table, "skipped")
                return None
            self._count(table, "unchanged")
            self.maps[table][wp_id] = existing
            return existing
        if self._gone(table, wp_id):
            return None
        obj = model(company=self.company, legacy_wp_id=wp_id, **values)
        if created_at is not None:
            obj.created_at = created_at
        if not self._save(table, wp_id, obj):
            return None
        self.fresh[table].add(wp_id)
        self._count(table, "created", obj)
        self.maps[table][wp_id] = obj
        return obj

    # -- stages ----------------------------------------------------------------------

    def _identities(self, batch):
        """Record verified matches in ``okr_identity_map`` for the audit trail."""
        if not self.apply:
            return
        for wp_id, row in self.mapping.user_rows.items():
            employee = self.mapping.employees.get(wp_id)
            values = {
                "wp_email": text(row.get("wp_email"))[:254],
                "wp_employee_code": text(row.get("wp_employee_code"))[:50],
                "employee": employee,
                "company": self.company,
                "matched_by": text(row.get("matched_by"))[:20],
                "status": row.get("status") if row.get("status") in IdentityStatus.values else IdentityStatus.UNMATCHED,
                "notes": text(row.get("notes")),
                "batch": batch,
            }
            if values["status"] == IdentityStatus.VERIFIED and employee is None:
                values["status"] = IdentityStatus.UNMATCHED
            if values["status"] == IdentityStatus.VERIFIED:
                values["verified_by"] = self.operator
                values["verified_at"] = timezone.now()
            existing = OKRIdentityMap.objects.filter(wp_user_id=wp_id).first()
            if existing is None:
                obj = OKRIdentityMap.objects.create(wp_user_id=wp_id, **values)
                self._count("identity_map", "created", obj)
                continue
            # Same decision as before: leave the row (and who verified it, when) alone.
            content = ("wp_email", "wp_employee_code", "employee", "company", "matched_by", "status", "notes")
            if all(canon(getattr(existing, name)) == canon(values[name]) for name in content):
                self._count("identity_map", "unchanged")
                continue
            for name, value in values.items():
                setattr(existing, name, value)
            existing.save()
            self._count("identity_map", "updated")

    def _cycles(self):
        rows = sorted(self.export.rows("cycles"), key=lambda r: ref(r.get("id")) or 0)
        for row in rows:
            wp_id = ref(row.get("id"))
            status = text(row.get("status"))
            if status not in CycleStatus.values:
                self.issue("warning", "cycles", wp_id, f"status {status!r} imported as closed")
                status = CycleStatus.CLOSED
            values = {
                "name": text(row.get("name"))[:190] or f"Chu kỳ {wp_id}",
                "cycle_type": _choice(row.get("cycle_type"), CycleType, CycleType.CUSTOM),
                "start_date": to_date(row.get("start_date")),
                "end_date": to_date(row.get("end_date")),
                "status": status,
            }
            if values["start_date"] is None or values["end_date"] is None:
                self.issue("error", "cycles", wp_id, "missing start or end date")
                self._count("cycles", "skipped")
                continue
            self._upsert(OKRCycle, "cycles", wp_id, values, created_at=to_datetime(row.get("created_at")),
                         updated_at=to_datetime(row.get("updated_at")))
        for row in rows:
            wp_id, parent_id = ref(row.get("id")), ref(row.get("parent_id"))
            cycle = self.maps["cycles"].get(wp_id)
            if cycle is None:
                continue
            parent = self.maps["cycles"].get(parent_id) if parent_id else None
            if parent_id and parent is None:
                self.issue("warning", "cycles", wp_id, f"parent cycle {parent_id} not found; left empty")
            self._link_parent("cycles", OKRCycle, cycle, wp_id, parent)

    def _teams(self):
        for row in sorted(self.export.rows("teams"), key=lambda r: ref(r.get("id")) or 0):
            wp_id = ref(row.get("id"))
            department_id = ref(row.get("department_id"))
            department = self.mapping.departments.get(department_id) if department_id else None
            if department_id and department is None:
                self.issue("warning", "teams", wp_id, f"department {department_id} not verified; left empty")
            status = text(row.get("status"))
            values = {
                "name": text(row.get("name"))[:190] or f"Team {wp_id}",
                "description": text(row.get("description")),
                "department": department,
                "manager": self.employee(row.get("manager_user_id"), "teams", wp_id, "manager"),
                "status": status if status in RecordStatus.values else RecordStatus.INACTIVE,
            }
            self._upsert(OKRTeam, "teams", wp_id, values, created_at=to_datetime(row.get("created_at")),
                         updated_at=to_datetime(row.get("updated_at")))
        for row in self.export.rows("team_members"):
            team_wp, user_wp = ref(row.get("team_id")), ref(row.get("user_id"))
            key = f"{team_wp}:{user_wp}"
            team = self.maps["teams"].get(team_wp)
            employee = self.employee(user_wp, "team_members", key, "member")
            if team is None or employee is None:
                if team is None:
                    self.issue("warning", "team_members", key, f"team {team_wp} not imported")
                self._count("team_members", "skipped")
                continue
            existing = OKRTeamMember.objects.filter(team=team, employee=employee).first()
            if existing is not None:
                self.touched["team_members"][key] = existing
                self._count("team_members", "unchanged")
                continue
            if self._gone("team_members", key):
                continue
            member = OKRTeamMember(
                company=self.company, team=team, employee=employee,
                joined_at=to_datetime(row.get("joined_at")) or timezone.now(),
            )
            if self._save("team_members", key, member):
                self.touched["team_members"][key] = member
                self._count("team_members", "created", member)

    def _objectives(self):
        rows = sorted(self.export.rows("objectives"), key=lambda r: ref(r.get("id")) or 0)
        for row in rows:
            wp_id = ref(row.get("id"))
            cycle = self.maps["cycles"].get(ref(row.get("cycle_id")))
            if cycle is None:
                self.issue("error", "objectives", wp_id, f"cycle {row.get('cycle_id')} not imported (orphan)")
                self._count("objectives", "skipped")
                continue
            scope = _choice(row.get("scope"), Scope, Scope.PERSONAL)
            department_id = ref(row.get("department_id"))
            department = self.mapping.departments.get(department_id) if department_id else None
            if department_id and department is None:
                if scope == Scope.DEPARTMENT:
                    self.issue("error", "objectives", wp_id, f"department {department_id} not verified")
                    self._count("objectives", "skipped")
                    continue
                self.issue("warning", "objectives", wp_id, f"department {department_id} not verified; left empty")
            team_id = ref(row.get("team_id"))
            team = self.maps["teams"].get(team_id) if team_id else None
            if team_id and team is None:
                if scope == Scope.TEAM:
                    self.issue("error", "objectives", wp_id, f"team {team_id} not imported")
                    self._count("objectives", "skipped")
                    continue
                self.issue("warning", "objectives", wp_id, f"team {team_id} not imported; left empty")
            deleted_at = to_datetime(row.get("deleted_at"))
            status = _choice(row.get("approval_status"), ApprovalStatus, ApprovalStatus.DRAFT)
            if deleted_at is not None:
                status = ApprovalStatus.DELETED
            values = {
                "cycle": cycle,
                "scope": scope,
                "department": department,
                "team": team,
                "owner": self.employee(row.get("owner_user_id"), "objectives", wp_id),
                "title": text(row.get("title"))[:255] or f"Objective {wp_id}",
                "description": text(row.get("description")),
                "weight": to_decimal(row.get("weight"), "100"),
                "priority": _choice(row.get("priority"), Priority, Priority.MEDIUM),
                "approval_status": status,
                "progress": to_decimal(row.get("progress")),
                "deleted_at": deleted_at,
            }
            self._upsert(OKRObjective, "objectives", wp_id, values, created_at=to_datetime(row.get("created_at")),
                         updated_at=to_datetime(row.get("updated_at")))
        for row in rows:
            wp_id, parent_id = ref(row.get("id")), ref(row.get("parent_objective_id"))
            objective = self.maps["objectives"].get(wp_id)
            if objective is None:
                continue
            parent = self.maps["objectives"].get(parent_id) if parent_id else None
            if parent_id and parent is None:
                self.issue("warning", "objectives", wp_id, f"parent objective {parent_id} not imported; left empty")
            self._link_parent("objectives", OKRObjective, objective, wp_id, parent)

    def _key_results(self):
        for row in sorted(self.export.rows("key_results"), key=lambda r: ref(r.get("id")) or 0):
            wp_id = ref(row.get("id"))
            objective = self.maps["objectives"].get(ref(row.get("objective_id")))
            if objective is None:
                self.issue("error", "key_results", wp_id, f"objective {row.get('objective_id')} not imported (orphan)")
                self._count("key_results", "skipped")
                continue
            deleted_at = to_datetime(row.get("deleted_at"))
            status = _choice(row.get("approval_status"), ApprovalStatus, ApprovalStatus.DRAFT)
            if deleted_at is not None:
                status = ApprovalStatus.DELETED
            values = {
                "objective": objective,
                "owner": self.employee(row.get("owner_user_id"), "key_results", wp_id),
                "title": text(row.get("title"))[:255] or f"Key result {wp_id}",
                "description": text(row.get("description")),
                "unit": text(row.get("unit"))[:40] or "%",
                "start_value": to_decimal(row.get("start_value")),
                "current_value": to_decimal(row.get("current_value")),
                "target_value": to_decimal(row.get("target_value"), "100"),
                "weight": to_decimal(row.get("weight"), "100"),
                "priority": _choice(row.get("priority"), Priority, Priority.MEDIUM),
                "due_date": to_date(row.get("due_date")),
                "approval_status": status,
                "progress": to_decimal(row.get("progress")),
                "deleted_at": deleted_at,
            }
            self._upsert(OKRKeyResult, "key_results", wp_id, values, created_at=to_datetime(row.get("created_at")),
                         updated_at=to_datetime(row.get("updated_at")))

    def _checkins(self):
        for row in sorted(self.export.rows("checkins"), key=lambda r: ref(r.get("id")) or 0):
            wp_id = ref(row.get("id"))
            key_result = self.maps["key_results"].get(ref(row.get("key_result_id")))
            if key_result is None:
                self.issue("error", "checkins", wp_id, f"key result {row.get('key_result_id')} not imported (orphan)")
                self._count("checkins", "skipped")
                continue
            confidence = ref(row.get("confidence")) or 3
            values = {
                "key_result": key_result,
                "author": self.employee(row.get("user_id"), "checkins", wp_id, "author"),
                "previous_value": to_decimal(row.get("previous_value")),
                "current_value": to_decimal(row.get("current_value")),
                "previous_progress": to_decimal(row.get("previous_progress")),
                "current_progress": to_decimal(row.get("current_progress")),
                "confidence": min(5, max(1, confidence)),
                "update_text": text(row.get("update_text")),
                "blockers": text(row.get("blockers")),
                "next_steps": text(row.get("next_steps")),
            }
            self._append(OKRCheckin, "checkins", wp_id, values, to_datetime(row.get("created_at")))

    def _comment_target(self, row):
        kind = text(row.get("object_type"))
        object_id = ref(row.get("object_id"))
        if kind == "objective":
            target = self.maps["objectives"].get(object_id)
            return ({"objective": target, "key_result": None} if target else None), kind
        if kind == "key_result":
            target = self.maps["key_results"].get(object_id)
            return ({"objective": None, "key_result": target} if target else None), kind
        return None, kind

    def _comments(self):
        rows = sorted(self.export.rows("comments"), key=lambda r: ref(r.get("id")) or 0)
        for row in rows:
            wp_id = ref(row.get("id"))
            target, kind = self._comment_target(row)
            if target is None:
                self.issue("error", "comments", wp_id, f"{kind} {row.get('object_id')} not imported (orphan)")
                self._count("comments", "skipped")
                continue
            values = {
                **target,
                "author": self.employee(row.get("user_id"), "comments", wp_id, "author"),
                "content": text(row.get("content")),
            }
            self._upsert(OKRComment, "comments", wp_id, values, created_at=to_datetime(row.get("created_at")),
                         updated_at=to_datetime(row.get("updated_at")))
        for row in rows:
            wp_id, parent_id = ref(row.get("id")), ref(row.get("parent_id"))
            comment = self.maps["comments"].get(wp_id)
            if comment is None:
                continue
            parent = self.maps["comments"].get(parent_id) if parent_id else None
            if parent_id and parent is None:
                self.issue("warning", "comments", wp_id, f"parent comment {parent_id} not imported; left empty")
            self._link_parent("comments", OKRComment, comment, wp_id, parent)

    def _evidence_file(self, wp_id, attachment_id):
        """Checked bytes of an evidence file, or ``None`` (reported)."""
        from okr.services.engagement import _check_upload

        attachment = self.export.attachments.get(attachment_id)
        if attachment is None:
            self.issue("error", "evidence", wp_id, f"attachment {attachment_id} missing from export")
            return None
        if self.uploads_dir is None:
            self.issue("error", "evidence", wp_id, "no --uploads-dir given; file not imported")
            return None
        relative = Path(text(attachment.get("relative_path")))
        if relative.is_absolute() or ".." in relative.parts:
            self.issue("error", "evidence", wp_id, "attachment path is not inside uploads; refused")
            return None
        path = (self.uploads_dir / relative).resolve()
        if not path.is_relative_to(self.uploads_dir.resolve()) or not path.is_file():
            self.issue("error", "evidence", wp_id, f"file {relative.as_posix()} not found in uploads")
            return None
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        expected = text(attachment.get("sha256")).lower()
        if expected and expected != digest:
            self.issue("error", "evidence", wp_id, f"file {relative.as_posix()} checksum differs from export")
            return None
        name = text(attachment.get("original_name")) or path.name
        upload = SimpleUploadedFile(name, content, content_type=text(attachment.get("mime_type")))
        try:
            checked_name, content_type, sha256 = _check_upload(upload)
        except Exception as exc:  # okr.services.errors.OKRError
            fields = getattr(exc, "fields", None) or {}
            self.issue("error", "evidence", wp_id, f"file refused: {fields.get('file') or exc}")
            return None
        return upload, checked_name, content_type, sha256

    def _evidence(self):
        for row in sorted(self.export.rows("evidence"), key=lambda r: ref(r.get("id")) or 0):
            wp_id = ref(row.get("id"))
            key_result = self.maps["key_results"].get(ref(row.get("key_result_id")))
            if key_result is None:
                self.issue("error", "evidence", wp_id, f"key result {row.get('key_result_id')} not imported (orphan)")
                self._count("evidence", "skipped")
                continue
            existing = OKREvidence._base_manager.filter(legacy_wp_id=wp_id).first()
            if existing is not None:
                if existing.company_id != self.company.pk:
                    self.issue("error", "evidence", wp_id, f"legacy id already used by company {existing.company_id}")
                    self._count("evidence", "skipped")
                    continue
                self.maps["evidence"][wp_id] = existing
                self._count("evidence", "unchanged")
                continue
            if self._gone("evidence", wp_id):
                continue
            checkin_wp = ref(row.get("checkin_id"))
            checkin = self.maps["checkins"].get(checkin_wp) if checkin_wp else None
            if checkin_wp and checkin is None:
                self.issue("warning", "evidence", wp_id, f"check-in {checkin_wp} not imported; link left empty")
            label = text(row.get("label"))[:255]
            evidence = OKREvidence(
                company=self.company,
                legacy_wp_id=wp_id,
                key_result=key_result,
                checkin=checkin,
                author=self.employee(row.get("user_id"), "evidence", wp_id, "author"),
                label=label,
            )
            attachment_id = ref(row.get("attachment_id"))
            if text(row.get("evidence_type")) == EvidenceType.FILE or attachment_id:
                checked = self._evidence_file(wp_id, attachment_id)
                if checked is None:
                    self._count("evidence", "skipped")
                    continue
                upload, name, content_type, sha256 = checked
                evidence.evidence_type = EvidenceType.FILE
                evidence.legacy_attachment_id = attachment_id
                evidence.original_name, evidence.content_type = name, content_type
                evidence.size, evidence.sha256 = upload.size, sha256
                evidence.label = label or name[:255]
                if not self.apply:
                    # A dry run checks the file but writes nothing; the row
                    # cannot exist without its file (okr_evidence_has_payload).
                    self._count("evidence", "created")
                    continue
                evidence.file.save(name, upload, save=False)
                self.written_files.append(evidence.file.name)
            else:
                url = text(row.get("url")).strip()
                if not url.startswith(("http://", "https://")):
                    self.issue("error", "evidence", wp_id, "link is not http(s); refused")
                    self._count("evidence", "skipped")
                    continue
                evidence.evidence_type, evidence.url = EvidenceType.URL, url[:2000]
                evidence.label = label or url[:255]
            created_at = to_datetime(row.get("created_at"))
            if created_at is not None:
                evidence.created_at = created_at
            if self._save("evidence", wp_id, evidence):
                self._count("evidence", "created", evidence)
                self.maps["evidence"][wp_id] = evidence

    def _notifications(self):
        for row in sorted(self.export.rows("notifications"), key=lambda r: ref(r.get("id")) or 0):
            wp_id = ref(row.get("id"))
            recipient = self.mapping.employees.get(ref(row.get("user_id")))
            if recipient is None:
                self.issue("info", "notifications", wp_id, "recipient not verified; notification not imported")
                self._count("notifications", "skipped")
                continue
            reference_type = text(row.get("reference_type"))
            reference_id = None
            if reference_type == ReferenceType.OBJECTIVE:
                target = self.maps["objectives"].get(ref(row.get("reference_id")))
                reference_id = target.pk if target else None
            elif reference_type == ReferenceType.KEY_RESULT:
                target = self.maps["key_results"].get(ref(row.get("reference_id")))
                reference_id = target.pk if target else None
            else:
                reference_type = ""
            dedupe = text(row.get("dedupe_key"))
            existing = OKRNotification._base_manager.filter(legacy_wp_id=wp_id).first()
            is_read = str(row.get("is_read")) in {"1", "true", "True"}
            if existing is not None:
                if existing.company_id != self.company.pk:
                    self.issue("error", "notifications", wp_id,
                               f"legacy id already used by company {existing.company_id}")
                    self._count("notifications", "skipped")
                    continue
                self.maps["notifications"][wp_id] = existing
                # "Read" only ever moves forward: read in either system stays read.
                if is_read and not existing.is_read:
                    self._remember_before("notifications", wp_id, existing)
                    OKRNotification._base_manager.filter(pk=existing.pk).update(is_read=True)
                    existing.is_read = True
                    self.updated_keys["notifications"].add(str(wp_id))
                    self._count("notifications", "updated")
                elif existing.is_read and not is_read:
                    self._count("notifications", "kept_read_in_django")
                else:
                    self._count("notifications", "unchanged")
                continue
            if self._gone("notifications", wp_id):
                continue
            notification = OKRNotification(
                company=self.company,
                legacy_wp_id=wp_id,
                recipient=recipient,
                notification_type=text(row.get("type"))[:40] or "info",
                title=text(row.get("title"))[:255] or "Thông báo OKR",
                message=text(row.get("message")),
                reference_type=reference_type if reference_id else "",
                reference_id=reference_id,
                dedupe_key=f"wp:{dedupe}"[:190] if dedupe else None,
                is_read=is_read,
                created_at=to_datetime(row.get("created_at")) or timezone.now(),
            )
            if self._save("notifications", wp_id, notification):
                self.fresh["notifications"].add(wp_id)
                self.maps["notifications"][wp_id] = notification
                self._count("notifications", "created", notification)

    def _activity(self):
        for row in sorted(self.export.rows("activity_log"), key=lambda r: ref(r.get("id")) or 0):
            wp_id = ref(row.get("id"))
            kind, object_id = text(row.get("object_type")), ref(row.get("object_id"))
            target = {"cycle": None, "objective": None, "key_result": None}
            table = {"cycle": "cycles", "objective": "objectives", "key_result": "key_results"}.get(kind)
            if table is None or self.maps[table].get(object_id) is None:
                self.issue("info", "activity_log", wp_id, f"{kind} {object_id} not imported; entry skipped")
                self._count("activity_log", "skipped")
                continue
            target[kind] = self.maps[table][object_id]
            raw = row.get("metadata")
            try:
                metadata = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
            except json.JSONDecodeError:
                metadata = {"raw": raw}
            if not isinstance(metadata, dict):
                metadata = {"value": metadata}
            action = text(row.get("action"))
            if action not in ActivityAction.values:
                metadata["wp_action"] = action
                action = ActivityAction.UPDATED
            values = {
                **target,
                "actor": self.employee(row.get("actor_user_id"), "activity_log", wp_id, "actor"),
                "action": action,
                "metadata": metadata,
            }
            self._append(OKRActivity, "activity_log", wp_id, values, to_datetime(row.get("created_at")))

    def _settings(self):
        options = self.export.options
        values = {}
        if options.get("warning_threshold") not in (None, ""):
            values["warning_threshold"] = to_decimal(options["warning_threshold"])
        frequency = text(options.get("reminder_frequency"))
        if frequency in ReminderFrequency.values:
            values["reminder_frequency"] = frequency
        if options.get("email_reminders") not in (None, ""):
            values["email_reminders"] = str(options["email_reminders"]) in {"1", "true", "True", "yes"}
        ranges = options.get("rating_ranges")
        if isinstance(ranges, str) and ranges.strip():
            try:
                ranges = json.loads(ranges)
            except json.JSONDecodeError:
                ranges = None
        if isinstance(ranges, list) and ranges:
            values["rating_ranges"] = ranges
        settings = OKRSettings.objects.filter(company=self.company).first()
        if settings is None:
            if self._gone("settings", "company"):
                return
            settings = OKRSettings(company=self.company, **values)
            settings.save()
            self.touched["settings"]["company"] = settings
            self._count("settings", "created", settings)
            return
        self.touched["settings"]["company"] = settings
        current, wanted = snapshot(settings, "settings"), planned(values, "settings")
        base = self._base("settings", "company")
        if base is None:
            # Settings saved in Django before any import: only untouched defaults may be replaced.
            defaults = snapshot(OKRSettings(company=self.company), "settings")
            edited = differs(current, defaults, FIELDS["settings"])
            diff = differs(wanted, current, wanted)
            if edited and diff:
                self.conflict("settings", "company", "no_provenance",
                              "OKR settings were set in Django; the export would replace them",
                              fields={n: {"django": current[n], "wordpress": wanted[n]} for n in diff})
                return
        else:
            changed = differs(current, base, FIELDS["settings"])
            if changed:
                self.conflict("settings", "company", "changed_in_django",
                              "OKR settings changed in Django after the import",
                              fields={n: {"imported": base[n], "django": current[n],
                                          "wordpress": wanted.get(n, base[n])} for n in changed})
                return
        diff = differs(wanted, current, wanted)
        if not diff:
            self._count("settings", "unchanged")
            return
        self._remember_before("settings", "company", settings)
        for name in diff:
            setattr(settings, name, values[name])
        settings.save()
        self.updated_keys["settings"].add("company")
        self._count("settings", "updated")

    def _roles(self):
        """Managers and department managers only with ``import_roles``;
        WordPress administrators are only listed, never granted."""
        for wp_id, user in sorted(self.export.users.items()):
            roles = set(user.get("roles") or [])
            if "administrator" in roles:
                employee = self.mapping.employees.get(wp_id)
                self.admin_candidates.append(
                    {"wp_user_id": wp_id, "employee_id": employee.pk if employee else None,
                     "note": "chỉ cấp sau khi được phê duyệt: manage.py okr_grant_role --employee <id> "
                             "--role admin --granted-by <username> --yes"}
                )
            if not self.import_roles or "joyokr_manager" not in roles:
                continue
            employee = self.mapping.employees.get(wp_id)
            if employee is None:
                self.issue("warning", "roles", wp_id, "joyokr_manager not imported: user not verified")
                continue
            key = f"{employee.pk}:{OKRRoleType.MANAGER}"
            existing = OKRRole.objects.filter(company=self.company, employee=employee, role=OKRRoleType.MANAGER).first()
            if existing is not None:
                self.touched["roles"][key] = existing
                self._count("roles", "unchanged")
                continue
            if self._gone("roles", key, wp_id):  # revoked in Django: never granted again by an import
                continue
            role = OKRRole(company=self.company, employee=employee, role=OKRRoleType.MANAGER, granted_by=self.operator)
            if self._save("roles", wp_id, role):
                self.touched["roles"][key] = role
                self._count("roles", "created", role)
                event = OKRRoleEvent.objects.create(
                    company=self.company, employee=employee, role=RoleEventRole.MANAGER,
                    action=RoleEventAction.GRANTED, actor=self.operator,
                )
                self.created["role_events"].append(event.pk)
        if not self.import_roles:
            return
        for row in self.export.rows("departments"):
            wp_dept = ref(row.get("id"))
            department = self.mapping.departments.get(wp_dept)
            manager_wp = ref(row.get("manager_user_id"))
            if department is None or manager_wp is None:
                continue
            employee = self.mapping.employees.get(manager_wp)
            if employee is None:
                self.issue("warning", "department_roles", wp_dept, "department manager not verified")
                continue
            key = str(department.pk)
            existing = OKRDepartmentRole.objects.filter(company=self.company, department=department).first()
            if existing is not None:
                # Never replaced: the Django assignment wins (reported, not overwritten).
                self.touched["department_roles"][key] = existing
                self._count("department_roles", "unchanged" if existing.manager_id == employee.pk else "kept_existing")
                continue
            if self._gone("department_roles", key, wp_dept):
                continue
            role = OKRDepartmentRole(
                company=self.company, department=department, manager=employee,
                legacy_wp_code=text(row.get("code"))[:40],
            )
            if self._save("department_roles", wp_dept, role):
                self.touched["department_roles"][key] = role
                self._count("department_roles", "created", role)
                event = OKRRoleEvent.objects.create(
                    company=self.company, employee=employee, role=RoleEventRole.DEPARTMENT_MANAGER,
                    action=RoleEventAction.GRANTED, department=department, actor=self.operator,
                )
                self.created["role_events"].append(event.pk)

    # -- reconciliation --------------------------------------------------------------

    def _reconcile(self):
        source = self.export.counts()
        imported = {}
        for table, model in (
            ("cycles", OKRCycle), ("teams", OKRTeam), ("objectives", OKRObjective),
            ("key_results", OKRKeyResult), ("checkins", OKRCheckin), ("comments", OKRComment),
            ("evidence", OKREvidence), ("notifications", OKRNotification), ("activity_log", OKRActivity),
        ):
            imported[table] = model._base_manager.filter(company=self.company, legacy_wp_id__isnull=False).count()
        kr_mismatches = []
        for wp_id, kr in self.maps["key_results"].items():
            computed = calculator.progress(kr.start_value, kr.target_value, kr.current_value)
            if abs(float(computed) - float(kr.progress)) > PROGRESS_TOLERANCE:
                kr_mismatches.append({"wp_id": wp_id, "wp_progress": str(kr.progress), "computed": str(computed)})
        objective_mismatches = []
        for wp_id, objective in self.maps["objectives"].items():
            krs = [kr for kr in OKRKeyResult._base_manager.filter(objective=objective, deleted_at__isnull=True)]
            if objective.approval_status == ApprovalStatus.APPROVED:
                krs = [kr for kr in krs if kr.approval_status == ApprovalStatus.APPROVED]
            computed = calculator.weighted_progress((kr.progress, kr.weight) for kr in krs)
            if abs(float(computed) - float(objective.progress)) > PROGRESS_TOLERANCE:
                objective_mismatches.append(
                    {"wp_id": wp_id, "wp_progress": str(objective.progress), "computed": str(computed)}
                )
        latest_mismatches = []
        for wp_id, kr in self.maps["key_results"].items():
            latest = OKRCheckin._base_manager.filter(key_result=kr).order_by("-created_at", "-id").first()
            if latest is not None and latest.current_value != kr.current_value:
                latest_mismatches.append(
                    {"wp_id": wp_id, "current_value": str(kr.current_value), "latest_checkin": str(latest.current_value)}
                )
                if str(wp_id) in self.updated_keys["key_results"] and wp_id not in self.conflicted["key_results"]:
                    # A re-import must never leave a key result disagreeing with its newest check-in.
                    self.conflict(
                        "key_results", wp_id, "value_vs_latest_checkin",
                        "after this update the key result value would differ from its newest check-in",
                        fields={"current_value": {"wordpress": canon(kr.current_value),
                                                  "latest_checkin": canon(latest.current_value)}},
                    )
        # Values WordPress holds without any check-in behind them (reviewed by a person).
        without_history = []
        for wp_id, kr in self.maps["key_results"].items():
            if kr.deleted_at is None and kr.current_value != kr.start_value and not OKRCheckin._base_manager.filter(
                    key_result=kr).exists():
                without_history.append({"wp_id": wp_id, "start_value": str(kr.start_value),
                                        "current_value": str(kr.current_value)})
        self.reconciliation.update({
            "source_counts": source,
            "imported_legacy_rows": imported,
            "key_result_progress_mismatches": kr_mismatches,
            "objective_progress_mismatches": objective_mismatches,
            "key_result_vs_latest_checkin": latest_mismatches,
            "key_results_without_checkin_history": without_history,
            "native_rows_untouched": {
                "objectives": OKRObjective._base_manager.filter(company=self.company, legacy_wp_id__isnull=True).count(),
                "checkins": OKRCheckin._base_manager.filter(company=self.company, legacy_wp_id__isnull=True).count(),
            },
        })
