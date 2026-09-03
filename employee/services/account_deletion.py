"""Phase EMPLOYEE-BULK-DELETE-2 — complete, safe employee account deletion.

Policy (C): the operator may permanently delete selected employees together
with the data those employees *own*, but never shared company configuration
and never records that merely mention them.

Why a service instead of more code in the view
----------------------------------------------
Audit EMPLOYEE-BULK-DELETE-AUDIT-1 mapped 57 relations into ``Employee`` and
193 into ``JoydigiUser``. Seventeen of them are ``PROTECT``, so the previous
implementation — a bare ``user.delete()`` in a loop — raised ``ProtectedError``
for any employee who had ever clocked in, caught it per employee, and kept
going. That produced exactly the partial result this phase forbids, and it
reported ``{"message": "Success"}`` either way.

Ownership is derived, not hard-coded
------------------------------------
The relations are read from ``Employee._meta.related_objects`` at import time
and split by the *field name*, which is what actually encodes ownership in
this schema::

    Attendance.employee_id   -> the employee's own attendance   -> delete
    Attendance.approved_by   -> somebody else's attendance      -> detach

A hard-coded list would silently miss a relation added later; deriving means a
new ``PROTECT`` relation is either handled or reported as a blocker, but never
skipped. A non-nullable cross-reference is refused rather than guessed at —
see ``BlockedRelation``.
"""

from __future__ import annotations

from django.db import transaction

from employee.models import Employee

#: Field names that mean "this row belongs to that employee". Everything else
#: on ``Employee`` — ``approved_by``, ``created_by``, ``acted_by``,
#: ``reporting_manager_id``, ``reallocate_to`` and the ``employees`` /
#: ``specific_employees`` M2Ms — is a reference to a record owned by someone
#: else, or by the company.
OWNERSHIP_FIELD_NAMES = frozenset({"employee_id", "employee"})

#: Grouping for the confirmation dialog. These are presentation buckets only:
#: what actually gets deleted is decided by ownership, not by this list.
_ATTENDANCE_LABELS = frozenset(
    {
        "attendance.Attendance",
        "attendance.AttendanceActivity",
        "attendance.AttendanceLateComeEarlyOut",
        "attendance.AttendanceOverTime",
    }
)
_REQUEST_LABELS = frozenset(
    {
        "attendance.OvertimeRequest",
        "attendance.AttendanceExplanationRequest",
        "attendance.AttendanceLateEarlyRequest",
        "attendance.RemoteWorkRequest",
        "base.ShiftRequest",
        "base.WorkTypeRequest",
        "leave.LeaveRequest",
        "leave.LeaveAllocationRequest",
    }
)
_DOCUMENT_LABELS = frozenset({"joydigi_documents.Document"})


class BlockedRelation(Exception):
    """A cross-employee reference that cannot be detached without inventing
    data. Raised instead of guessing a replacement value."""


def _on_delete_name(relation):
    handler = relation.field.remote_field.on_delete
    return getattr(handler, "__name__", str(handler))


def _classify():
    """Split ``Employee``'s reverse relations into the three groups that
    matter. Concrete foreign keys only — reverse M2M rows are unlinked by
    Django, and the objects on the far side (announcements, company-wide
    notices) belong to the company, not to the employee.
    """
    owned_blocking = []  # PROTECT/DO_NOTHING + owned -> delete explicitly
    owned_cascade = []  # CASCADE + owned            -> Django removes these
    detach = []  # not owned                  -> NULL the reference

    for relation in Employee._meta.related_objects:
        field = relation.field
        if field.many_to_many:
            continue
        behaviour = _on_delete_name(relation)
        entry = (relation.related_model, field.name, behaviour)

        if field.name in OWNERSHIP_FIELD_NAMES:
            if behaviour in ("PROTECT", "DO_NOTHING"):
                owned_blocking.append(entry)
            elif behaviour == "CASCADE":
                owned_cascade.append(entry)
            # SET_NULL on an ownership field neither blocks nor dangles:
            # Django clears it and the row stops naming the employee.
        elif behaviour in ("PROTECT", "DO_NOTHING"):
            detach.append(entry)

    # DO_NOTHING first: those are the rows that would otherwise be left
    # dangling, and clearing them before their parent disappears keeps the
    # database referentially clean at every step.
    owned_blocking.sort(key=lambda e: (e[2] != "DO_NOTHING", e[0]._meta.label))
    return owned_blocking, owned_cascade, detach


OWNED_BLOCKING_RELATIONS, OWNED_CASCADE_RELATIONS, DETACH_RELATIONS = _classify()

#: Every relation holding employee-owned rows, blocking or not. Used by the
#: preview so the operator sees the true volume, not only the ``PROTECT``
#: part of it.
OWNED_RELATIONS = OWNED_BLOCKING_RELATIONS + OWNED_CASCADE_RELATIONS


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _error(code, message, employee_id=None, name=None):
    return {
        "code": code,
        "message": message,
        "employee_id": employee_id,
        "name": name,
    }


def parse_ids(raw):
    """Normalise the id list.

    Anything non-integral rejects the whole payload rather than being quietly
    dropped: silently ignoring one malformed entry would delete a *subset* of
    what the operator selected and confirmed.
    """
    if not isinstance(raw, (list, tuple)):
        return None, _error("INVALID_IDS", "Danh sách nhân viên không hợp lệ.")
    ids = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None, _error(
                "INVALID_IDS", "Danh sách nhân viên chứa giá trị không hợp lệ."
            )
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            return None, _error(
                "INVALID_IDS", "Danh sách nhân viên chứa giá trị không hợp lệ."
            )
    if not ids:
        return None, _error("NO_IDS", "Chưa chọn nhân viên nào.")
    return list(dict.fromkeys(ids)), None


def validate(request, ids):
    """Return ``(employees, errors)``.

    ``Employee.objects`` is a ``JoydigiCompanyManager``, so the lookup is
    already confined to the requesting session's company scope: an id posted
    by hand for another tenant simply does not come back, and is reported as
    not found. The answer is identical for "does not exist" and "belongs to
    another company", so this cannot be used to probe foreign ids.
    """
    errors = []
    found = list(
        Employee.objects.filter(id__in=ids).select_related("employee_user_id")
    )
    by_id = {employee.id: employee for employee in found}

    for missing in [i for i in ids if i not in by_id]:
        errors.append(
            _error(
                "NOT_FOUND",
                "Không tìm thấy nhân viên trong phạm vi công ty của bạn.",
                employee_id=missing,
            )
        )

    own_employee_id = getattr(
        getattr(request.user, "employee_get", None), "id", None
    )

    for employee in found:
        user = employee.employee_user_id
        if employee.id == own_employee_id or (
            user is not None and user.pk == request.user.pk
        ):
            errors.append(
                _error(
                    "SELF",
                    "Không thể tự xóa tài khoản của chính bạn.",
                    employee_id=employee.id,
                    name=str(employee),
                )
            )
        if user is not None and user.is_superuser:
            errors.append(
                _error(
                    "SUPERUSER",
                    "Không thể xóa tài khoản quản trị cấp cao qua màn hình này.",
                    employee_id=employee.id,
                    name=str(employee),
                )
            )

    return found, errors


# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------


def _owned_counts(employee):
    """Count every row the employee owns, in the buckets the dialog shows."""
    counts = {
        "attendance_count": 0,
        "request_count": 0,
        "document_count": 0,
        "other_owned_records": 0,
    }
    for model, field_name, _behaviour in OWNED_RELATIONS:
        total = model._base_manager.filter(**{field_name: employee}).count()
        if not total:
            continue
        label = model._meta.label
        if label in _ATTENDANCE_LABELS:
            counts["attendance_count"] += total
        elif label in _REQUEST_LABELS:
            counts["request_count"] += total
        elif label in _DOCUMENT_LABELS:
            counts["document_count"] += total
        else:
            counts["other_owned_records"] += total
    return counts


def preview(employees):
    """Per-employee dependency summary.

    Read-only — ``count()`` queries only. The delete path recomputes all of
    this from the database, so the numbers the browser was shown are never
    accepted back as input.
    """
    rows = []
    total = 0
    for employee in employees:
        counts = _owned_counts(employee)
        subtotal = sum(counts.values())
        total += subtotal
        rows.append(
            {
                "employee_id": employee.id,
                "name": str(employee),
                "badge_id": employee.badge_id,
                "has_user": employee.employee_user_id is not None,
                "total_owned_records": subtotal,
                **counts,
            }
        )
    return rows, total


def confirmation_phrase(count):
    """The exact text the operator must type. Defined once so the view, the
    browser and the tests cannot drift apart."""
    return f"XOA {count} TAI KHOAN"


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------


def _detach_cross_references(employees):
    """Keep other people's records; drop only the pointer to the departing
    employee.

    This is the rule that separates "delete this employee's history" from
    "corrupt everybody else's". A subordinate is not deleted because their
    manager is, and an attendance record approved by the departing employee
    stays with the employee who actually worked it.
    """
    detached = {}
    for model, field_name, _behaviour in DETACH_RELATIONS:
        field = model._meta.get_field(field_name)
        if not field.null:
            raise BlockedRelation(
                f"{model._meta.label}.{field_name} không cho phép NULL — "
                "không thể tách tham chiếu mà không bịa dữ liệu."
            )
        updated = model._base_manager.filter(
            **{f"{field_name}__in": employees}
        ).update(**{field_name: None})
        if updated:
            detached[f"{model._meta.label}.{field_name}"] = updated
    return detached


def _delete_owned(employees):
    """Remove the rows that would block deletion, or that would dangle.

    ``_base_manager`` deliberately: the default manager on several of these
    models is company-scoped, and re-filtering here could leave a subset
    behind that then raises ``ProtectedError`` from inside the transaction.
    The *employees* were already validated against the caller's scope, which
    is where that check belongs.
    """
    removed = {}
    for model, field_name, _behaviour in OWNED_BLOCKING_RELATIONS:
        deleted, _detail = model._base_manager.filter(
            **{f"{field_name}__in": employees}
        ).delete()
        if deleted:
            label = model._meta.label
            removed[label] = removed.get(label, 0) + deleted
    return removed


def _delete_accounts(employees):
    """``Employee.employee_user_id`` is a ``OneToOneField(CASCADE)`` owned by
    ``Employee``, so deleting the *user* takes the employee with it. That is
    the only order that leaves no login-capable orphan behind.

    An employee with no user — the schema permits it — is deleted directly.
    """
    count = 0
    for employee in employees:
        user = employee.employee_user_id
        if user is not None:
            user.delete()
        else:
            employee.delete()
        count += 1
    return count


@transaction.atomic
def delete_employees(employees):
    """Permanently delete the given employees and the data they own.

    One transaction for the whole batch: if any part fails, nothing is
    deleted. A half-finished batch would leave the operator unable to tell
    which accounts still exist, which is worse than an outright failure.
    """
    employees = list(employees)
    detached = _detach_cross_references(employees)
    removed = _delete_owned(employees)
    deleted_count = _delete_accounts(employees)
    return {
        "deleted_count": deleted_count,
        "owned_records_deleted": removed,
        "references_detached": detached,
    }
