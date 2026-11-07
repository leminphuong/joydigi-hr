"""WordPress user → HR employee and JOYOKR department → HR department.

``propose()`` only *suggests* matches; every suggestion starts as
``proposed`` (or ``unmatched``) and a person must set it to ``verified`` in
the mapping file before the importer will use it. An e-mail match alone is
marked as such so a reviewer checks it; nothing is ever verified here, no
employee or department is created, and a match is only offered inside the
target company.

Mapping file (JSON)::

    {"format": "joyokr-mapping", "format_version": 1, "company_id": 1,
     "export_checksum": "...",
     "users": [{"wp_user_id", "wp_login", "wp_email", "wp_employee_code",
                "wp_roles", "employee_id", "employee_name", "matched_by",
                "status", "notes"}],
     "departments": [{"wp_department_id", "wp_code", "wp_name",
                      "hr_department_id", "hr_department_name", "status",
                      "notes"}]}
"""

from __future__ import annotations

import csv
import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from okr.legacy_import.export import Export, ExportError, ref, text
from okr.models.base import department_allows_company, employee_company_id
from okr.models.choices import IdentityMatch, IdentityStatus

FORMAT = "joyokr-mapping"
FORMAT_VERSION = 1
DEPARTMENT_STATUSES = {"proposed", "verified", "rejected", "unmatched"}


def _norm(value) -> str:
    folded = unicodedata.normalize("NFC", text(value)).strip().casefold()
    return " ".join(folded.split())


def _company_employees(company_id):
    from employee.models import Employee, EmployeeWorkInformation

    ids = EmployeeWorkInformation._base_manager.filter(company_id_id=company_id).values_list(
        "employee_id", flat=True
    )
    return Employee._base_manager.filter(pk__in=list(ids)).select_related("employee_user_id")


def _company_departments(company_id):
    from base.models import Department

    return [d for d in Department._base_manager.all() if department_allows_company(d, company_id)]


def propose(export: Export, company_id: int) -> dict:
    employees = list(_company_employees(company_id))
    by_email, by_badge = {}, {}
    for employee in employees:
        emails = {_norm(employee.email)}
        user = getattr(employee, "employee_user_id", None)
        if user is not None and getattr(user, "email", ""):
            emails.add(_norm(user.email))
        for email in emails - {""}:
            by_email.setdefault(email, set()).add(employee)
        badge = _norm(employee.badge_id)
        if badge:
            by_badge.setdefault(badge, set()).add(employee)

    users = []
    for wp_id, user in sorted(export.users.items()):
        email, code = _norm(user.get("email")), _norm(user.get("employee_code"))
        email_hits = by_email.get(email, set()) if email else set()
        badge_hits = by_badge.get(code, set()) if code else set()
        row = {
            "wp_user_id": wp_id,
            "wp_login": text(user.get("login")),
            "wp_email": text(user.get("email")),
            "wp_employee_code": text(user.get("employee_code")),
            "wp_roles": list(user.get("roles") or []),
            "employee_id": None,
            "employee_name": "",
            "matched_by": "",
            "status": IdentityStatus.UNMATCHED,
            "notes": "",
        }
        match, reason = None, ""
        if len(email_hits) > 1 or len(badge_hits) > 1:
            reason = "nhiều nhân viên trùng email hoặc mã nhân viên"
        elif email_hits and badge_hits:
            if email_hits == badge_hits:
                match, row["matched_by"] = next(iter(email_hits)), IdentityMatch.EMAIL_AND_BADGE
            else:
                reason = "email và mã nhân viên trỏ tới hai nhân viên khác nhau"
        elif badge_hits:
            match, row["matched_by"] = next(iter(badge_hits)), IdentityMatch.BADGE
            reason = "chỉ khớp mã nhân viên — cần kiểm tra"
        elif email_hits:
            match, row["matched_by"] = next(iter(email_hits)), IdentityMatch.EMAIL
            reason = "chỉ khớp email — bắt buộc kiểm tra thủ công"
        else:
            reason = "không tìm thấy nhân viên trong công ty"
        if match is not None:
            row.update(
                employee_id=match.pk,
                employee_name=match.get_full_name(),
                status=IdentityStatus.PROPOSED,
            )
        row["notes"] = reason
        users.append(row)

    departments_by_name = {}
    for department in _company_departments(company_id):
        departments_by_name.setdefault(_norm(department.department), []).append(department)
    departments = []
    for wp_department in sorted(export.rows("departments"), key=lambda r: ref(r.get("id")) or 0):
        hits = departments_by_name.get(_norm(wp_department.get("name")), [])
        row = {
            "wp_department_id": ref(wp_department.get("id")),
            "wp_code": text(wp_department.get("code")),
            "wp_name": text(wp_department.get("name")),
            "hr_department_id": None,
            "hr_department_name": "",
            "status": "unmatched",
            "notes": "không có phòng ban HR trùng tên",
        }
        if len(hits) == 1:
            row.update(
                hr_department_id=hits[0].pk,
                hr_department_name=hits[0].department,
                status="proposed",
                notes="trùng tên — cần xác nhận",
            )
        elif len(hits) > 1:
            row["notes"] = "nhiều phòng ban HR trùng tên"
        departments.append(row)

    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "company_id": company_id,
        "export_checksum": export.checksum,
        "users": users,
        "departments": departments,
    }


# The review table (CSV, opens in Excel): one row per WordPress user and per
# JOYOKR department. A reviewer fills ``target_id`` and sets ``status``.
CSV_COLUMNS = [
    "kind", "wp_id", "wp_login", "wp_email", "wp_employee_code", "wp_roles", "wp_name",
    "company_id", "target_id", "target_name", "matched_by", "status", "notes",
]


def write_mapping_csv(data: dict, path) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in data["users"]:
            writer.writerow({
                "kind": "user", "wp_id": row["wp_user_id"], "wp_login": row["wp_login"],
                "wp_email": row["wp_email"], "wp_employee_code": row["wp_employee_code"],
                "wp_roles": " ".join(row["wp_roles"]), "wp_name": "", "company_id": data["company_id"],
                "target_id": row["employee_id"] or "", "target_name": row["employee_name"],
                "matched_by": row["matched_by"], "status": row["status"], "notes": row["notes"],
            })
        for row in data["departments"]:
            writer.writerow({
                "kind": "department", "wp_id": row["wp_department_id"], "wp_login": row["wp_code"],
                "wp_email": "", "wp_employee_code": "", "wp_roles": "", "wp_name": row["wp_name"],
                "company_id": data["company_id"], "target_id": row["hr_department_id"] or "",
                "target_name": row["hr_department_name"], "matched_by": "", "status": row["status"],
                "notes": row["notes"],
            })


def _read_mapping_csv(path) -> dict:
    users, departments, companies = [], [], set()
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(CSV_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ExportError(f"Mapping CSV lacks columns: {', '.join(sorted(missing))}")
        for line in reader:
            kind = (line.get("kind") or "").strip()
            companies.add((line.get("company_id") or "").strip())
            target = ref(line.get("target_id"))
            status = (line.get("status") or "").strip().lower()
            if kind == "user":
                users.append({
                    "wp_user_id": ref(line.get("wp_id")), "wp_login": line.get("wp_login", ""),
                    "wp_email": line.get("wp_email", ""), "wp_employee_code": line.get("wp_employee_code", ""),
                    "wp_roles": (line.get("wp_roles") or "").split(), "employee_id": target,
                    "employee_name": line.get("target_name", ""), "matched_by": line.get("matched_by", ""),
                    "status": status, "notes": line.get("notes", ""),
                })
            elif kind == "department":
                departments.append({
                    "wp_department_id": ref(line.get("wp_id")), "wp_code": line.get("wp_login", ""),
                    "wp_name": line.get("wp_name", ""), "hr_department_id": target,
                    "hr_department_name": line.get("target_name", ""), "status": status,
                    "notes": line.get("notes", ""),
                })
            else:
                raise ExportError(f"Mapping CSV: unknown kind {kind!r}.")
    if len(companies) != 1:
        raise ExportError("Mapping CSV must name exactly one company_id.")
    return {"format": FORMAT, "format_version": FORMAT_VERSION, "company_id": ref(companies.pop()),
            "users": users, "departments": departments}


@dataclass
class Mapping:
    company_id: int
    employees: dict  # wp user id -> Employee (verified only)
    departments: dict  # wp department id -> Department (verified only)
    user_rows: dict  # wp user id -> mapping row (any status)
    department_rows: dict
    problems: list = field(default_factory=list)

    def status_of(self, wp_user_id) -> str:
        row = self.user_rows.get(wp_user_id)
        return row["status"] if row else "absent"


def load_mapping(path, company_id: int) -> Mapping:
    try:
        if Path(path).suffix.lower() == ".csv":
            data = _read_mapping_csv(path)
        else:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, csv.Error) as exc:
        raise ExportError(f"Cannot read mapping file: {exc}") from exc
    if data.get("format") != FORMAT or data.get("format_version") != FORMAT_VERSION:
        raise ExportError("Not a JOYOKR mapping file (format 'joyokr-mapping', version 1).")
    if data.get("company_id") != company_id:
        raise ExportError(
            f"Mapping was prepared for company {data.get('company_id')!r}, not {company_id}."
        )
    from base.models import Department
    from employee.models import Employee

    problems, employees, user_rows = [], {}, {}
    for row in data.get("users") or []:
        wp_id = ref(row.get("wp_user_id"))
        if wp_id is None:
            raise ExportError("Every mapping user needs 'wp_user_id'.")
        if wp_id in user_rows:
            raise ExportError(f"WordPress user {wp_id} appears twice in the mapping.")
        user_rows[wp_id] = row
        if row.get("status") != IdentityStatus.VERIFIED:
            continue
        employee = Employee._base_manager.filter(pk=ref(row.get("employee_id"))).first()
        if employee is None:
            problems.append(f"user {wp_id}: verified but employee {row.get('employee_id')!r} not found")
            continue
        if employee_company_id(employee) != company_id:
            problems.append(f"user {wp_id}: employee {employee.pk} does not work for company {company_id}")
            continue
        employees[wp_id] = employee
    claimed = {}
    for wp_id, employee in employees.items():
        if employee.pk in claimed:
            problems.append(
                f"employee {employee.pk} verified for two WordPress users ({claimed[employee.pk]}, {wp_id})"
            )
        claimed[employee.pk] = wp_id

    departments, department_rows = {}, {}
    for row in data.get("departments") or []:
        wp_id = ref(row.get("wp_department_id"))
        if wp_id is None:
            raise ExportError("Every mapping department needs 'wp_department_id'.")
        if row.get("status") not in DEPARTMENT_STATUSES:
            raise ExportError(f"Department {wp_id}: unknown status {row.get('status')!r}.")
        department_rows[wp_id] = row
        if row.get("status") != "verified":
            continue
        department = Department._base_manager.filter(pk=ref(row.get("hr_department_id"))).first()
        if department is None or not department_allows_company(department, company_id):
            problems.append(f"department {wp_id}: verified HR department is missing or not linked to the company")
            continue
        departments[wp_id] = department
    return Mapping(
        company_id=company_id,
        employees=employees,
        departments=departments,
        user_rows=user_rows,
        department_rows=department_rows,
        problems=problems,
    )
