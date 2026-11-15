"""
leave_reconcile

READ-ONLY reconciliation of leave types and balances, grouped by company and
leave type, so an admin can see what is wrong before changing anything:

* balances missing for eligible employees (what ``backfill_available_leave``
  would open — same rules, including the leave type's conditions);
* leave types with the same name (after trimming spaces and case);
* more than one compensatory leave type (the model allows one);
* duplicate balances for one employee and type (should be impossible: the
  database has a unique constraint);
* balances whose type belongs to another company than the employee's current
  one (kept as history; the app no longer offers them);
* balances of inactive types;
* active employees with no work information, or with no company on it;
* balances that do not add up: negative days, or total_leave_days different
  from available_days + carryforward_days.

It never writes. Run it through ``manage.py shell`` on production so no
scheduler starts:

    python manage.py shell -c "from django.core.management import call_command; call_command('leave_reconcile')"
"""

import json
import unicodedata
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Count

from employee.models import Employee, EmployeeWorkInformation
from leave.models import AvailableLeave, LeaveType
from leave.services import evaluate_leave_type_conditions, leave_type_matches_company


def _norm(name):
    return " ".join(unicodedata.normalize("NFC", name or "").split()).casefold()


def reconcile():
    """The report as a dict (also used by tests)."""
    employees = list(
        Employee._base_manager.filter(is_active=True).select_related("employee_work_info__company_id")
    )
    work_by_employee = {
        row["employee_id"]: row["company_id"]
        for row in EmployeeWorkInformation._base_manager.filter(employee_id__in=[e.pk for e in employees]).values(
            "employee_id", "company_id"
        )
    }
    types = list(LeaveType._base_manager.all().prefetch_related("conditions"))
    active_types = [t for t in types if t.is_active]
    pairs = set(AvailableLeave._base_manager.values_list("employee_id", "leave_type_id"))

    missing = Counter()
    for employee in employees:
        company = work_by_employee.get(employee.pk)
        for leave_type in active_types:
            if (employee.pk, leave_type.pk) in pairs:
                continue
            if not leave_type_matches_company(leave_type, employee):
                continue
            eligible, _msg = evaluate_leave_type_conditions(leave_type, employee)
            if eligible:
                missing[(company, leave_type.pk)] += 1

    by_name = defaultdict(list)
    for leave_type in types:
        by_name[_norm(leave_type.name)].append(leave_type)
    duplicates = {
        name: [{"id": t.pk, "company_id": t.company_id_id, "active": t.is_active} for t in items]
        for name, items in by_name.items()
        if len(items) > 1
    }
    compensatory = [{"id": t.pk, "company_id": t.company_id_id, "active": t.is_active}
                    for t in types if t.is_compensatory_leave]

    duplicate_rows = list(
        AvailableLeave._base_manager.values("employee_id", "leave_type_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )

    type_company = {t.pk: t.company_id_id for t in types}
    type_active = {t.pk: t.is_active for t in types}
    wrong_company = Counter()
    inactive_type_rows = Counter()
    inconsistent = Counter()
    for row in AvailableLeave._base_manager.filter(employee_id__is_active=True).values(
        "employee_id", "leave_type_id", "available_days", "carryforward_days", "total_leave_days"
    ):
        lt = row["leave_type_id"]
        employee_company = work_by_employee.get(row["employee_id"])
        if type_company.get(lt) is not None and type_company.get(lt) != employee_company:
            wrong_company[(employee_company, lt)] += 1
        if not type_active.get(lt, True):
            inactive_type_rows[lt] += 1
        available = row["available_days"] or 0
        carry = row["carryforward_days"] or 0
        total = row["total_leave_days"]
        if available < 0 or carry < 0:
            inconsistent[(lt, "negative")] += 1
        elif total is not None and abs(total - (available + carry)) > 0.001:
            inconsistent[(lt, "total_mismatch")] += 1

    no_work_info = sum(1 for e in employees if e.pk not in work_by_employee)
    no_company = sum(1 for e in employees if e.pk in work_by_employee and work_by_employee[e.pk] is None)

    names = {t.pk: t.name for t in types}
    return {
        "active_employees": len(employees),
        "active_leave_types": len(active_types),
        "missing_balances": [
            {"company_id": c, "leave_type_id": t, "leave_type": names.get(t), "count": n}
            for (c, t), n in sorted(missing.items(), key=lambda kv: (str(kv[0][0]), names.get(kv[0][1]) or ""))
        ],
        "missing_total": sum(missing.values()),
        "duplicate_type_names": duplicates,
        "compensatory_types": compensatory,
        "duplicate_balance_rows": len(duplicate_rows),
        "wrong_company_balances": [
            {"employee_company_id": c, "leave_type_id": t, "leave_type": names.get(t), "count": n}
            for (c, t), n in sorted(wrong_company.items(), key=lambda kv: str(kv[0]))
        ],
        "inactive_type_balances": [
            {"leave_type_id": t, "leave_type": names.get(t), "count": n} for t, n in sorted(inactive_type_rows.items())
        ],
        "employees_without_work_info": no_work_info,
        "employees_without_company": no_company,
        "inconsistent_balances": [
            {"leave_type_id": t, "leave_type": names.get(t), "problem": p, "count": n}
            for (t, p), n in sorted(inconsistent.items())
        ],
        **leave_request_checks(),
    }


def leave_request_checks():
    """2026-11-15, read-only:

    * approved leaves whose recorded days differ from the days taken from
      the fund (approved_available_days + approved_carryforward_days) — what
      an edit of an approved leave's dates used to leave behind;
    * months in which an employee holds more of the shared fund than the
      monthly limit (requests from before the limit, kept as they are).
    """
    from collections import defaultdict as _dd

    from leave import monthly_limit
    from leave.models import LeaveRequest

    mismatched = []
    approved = LeaveRequest._base_manager.filter(status="approved").values(
        "pk", "employee_id", "leave_type_id", "start_date", "end_date",
        "requested_days", "approved_available_days", "approved_carryforward_days",
    )
    for row in approved.iterator():
        taken = float(row["approved_available_days"] or 0) + float(row["approved_carryforward_days"] or 0)
        if abs(float(row["requested_days"] or 0) - taken) > 1e-6:
            mismatched.append({
                "leave_request_id": row["pk"], "employee_id": row["employee_id"],
                "leave_type_id": row["leave_type_id"], "start_date": row["start_date"],
                "end_date": row["end_date"], "requested_days": row["requested_days"], "taken_days": taken,
            })
    over_limit = []
    limit = monthly_limit.limit_days()
    if limit > 0:
        seen = _dd(set)
        rows = LeaveRequest._base_manager.select_related("leave_type_id").filter(
            status__in=monthly_limit.COUNTED_STATUSES
        ).values_list("employee_id", "leave_type_id", "start_date", "end_date")
        for employee_id, leave_type_id, start, end in rows.iterator():
            day = start.replace(day=1)
            while day <= (end or start):
                seen[(employee_id, leave_type_id)].add((day.year, day.month))
                day = (day.replace(year=day.year + 1, month=1) if day.month == 12 else day.replace(month=day.month + 1))
        from employee.models import Employee
        from leave.models import LeaveType

        checked = set()
        for (employee_id, leave_type_id), months in seen.items():
            leave_type = LeaveType._base_manager.filter(pk=leave_type_id).first()
            if not monthly_limit.is_limited(leave_type):
                continue
            fund = tuple(monthly_limit.fund_type_ids(leave_type))
            employee = Employee._base_manager.filter(pk=employee_id).first()
            for year, month in sorted(months):
                if (employee_id, fund, year, month) in checked:
                    continue
                checked.add((employee_id, fund, year, month))
                usage = monthly_limit.month_usage(employee, leave_type, year, month)
                if usage["held_days"] > limit + 1e-6:
                    over_limit.append({"employee_id": employee_id, "year": year, "month": month,
                                       "held_days": usage["held_days"], "limit_days": limit})
    return {"approved_days_mismatch": mismatched, "months_over_limit": over_limit}


class Command(BaseCommand):
    help = "Read-only report of leave types and balances that need attention."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")

    def handle(self, *args, **options):
        report = reconcile()
        if options["json"]:
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return
        w = self.stdout.write
        w("Đối soát nghỉ phép (CHỈ ĐỌC)")
        w(f"  nhân viên đang làm: {report['active_employees']} | loại phép đang hoạt động: {report['active_leave_types']}")
        w(f"1. Thiếu quỹ phép cho người đủ điều kiện: {report['missing_total']}")
        for item in report["missing_balances"]:
            w(f"   công ty #{item['company_id']} | {item['leave_type']} (#{item['leave_type_id']}): {item['count']}")
        w(f"2. Tên loại phép trùng: {len(report['duplicate_type_names'])}")
        for name, items in report["duplicate_type_names"].items():
            w(f"   '{name}': " + ", ".join(f"#{i['id']} (công ty {i['company_id'] or 'chung'}, hoạt động={i['active']})" for i in items))
        flag = " ⚠ chỉ được có 1" if len(report["compensatory_types"]) > 1 else ""
        w(f"3. Loại nghỉ bù: {len(report['compensatory_types'])}{flag}")
        w(f"4. Quỹ phép trùng (cùng người, cùng loại): {report['duplicate_balance_rows']}")
        w("5. Quỹ phép thuộc loại của công ty khác (giữ làm lịch sử, App không hiện):")
        for item in report["wrong_company_balances"] or [{"count": 0}]:
            if item["count"]:
                w(f"   nhân viên công ty #{item['employee_company_id']} | {item['leave_type']} (#{item['leave_type_id']}): {item['count']}")
            else:
                w("   không có")
        w("6. Quỹ phép của loại đã tắt:")
        for item in report["inactive_type_balances"] or [{"count": 0}]:
            w(f"   {item['leave_type']} (#{item['leave_type_id']}): {item['count']}" if item["count"] else "   không có")
        w(f"7. Nhân viên đang làm KHÔNG có hồ sơ công việc: {report['employees_without_work_info']} | "
          f"có hồ sơ nhưng chưa gắn công ty: {report['employees_without_company']}")
        w("8. Số dư không khớp:")
        for item in report["inconsistent_balances"] or [{"count": 0}]:
            w(f"   {item['leave_type']} (#{item['leave_type_id']}) {item['problem']}: {item['count']}" if item["count"] else "   không có")
        w(f"9. Đơn đã duyệt có số ngày khác số ngày đã trừ quỹ: {len(report['approved_days_mismatch'])}")
        for item in report["approved_days_mismatch"][:50]:
            w(f"   đơn #{item['leave_request_id']} (NV #{item['employee_id']}): ghi {item['requested_days']} ngày, "
              f"đã trừ {item['taken_days']} ngày")
        w(f"10. Tháng vượt giới hạn sử dụng (đơn cũ, giữ nguyên): {len(report['months_over_limit'])}")
        for item in report["months_over_limit"][:50]:
            w(f"   NV #{item['employee_id']} tháng {item['month']:02d}/{item['year']}: "
              f"{item['held_days']} / {item['limit_days']} ngày")
        w("Không có gì bị ghi.")
