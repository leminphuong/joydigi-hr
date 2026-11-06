"""Fake-data builders for OKR tests. Never use real names, e-mails or passwords."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from base.models import Department
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from okr.models import OKRCycle, OKRKeyResult, OKRObjective, OKRRole
from okr.models.choices import Scope

FAKE_PASSWORD = "okr-fake-pass-1"


def okr_company(name: str):
    return make_company(name, address=f"{name} street")


def okr_department(company, name: str = "Phong Ky Thuat"):
    department = Department.objects.create(department=name)
    department.company_id.add(company)
    return department


def okr_employee(company, username: str, department=None):
    user = make_user(username, password=FAKE_PASSWORD, email=f"{username}@example.com")
    return make_employee(
        company=company,
        email=f"{username}@example.com",
        user=user,
        department=department,
    )


def okr_cycle(company, name: str = "Q4 2026", **overrides):
    values = {
        "company": company,
        "name": name,
        "cycle_type": "quarter",
        "start_date": date(2026, 10, 1),
        "end_date": date(2026, 12, 31),
        "status": "active",
    }
    values.update(overrides)
    return OKRCycle.objects.create(**values)


def okr_objective(cycle, owner, **overrides):
    values = {
        "company": cycle.company,
        "cycle": cycle,
        "owner": owner,
        "title": "Tang ty le giu chan khach hang",
        "scope": Scope.PERSONAL,
    }
    values.update(overrides)
    return OKRObjective.objects.create(**values)


def okr_key_result(objective, owner, **overrides):
    values = {
        "objective": objective,
        "owner": owner,
        "title": "Dat 80 diem NPS",
        "start_value": Decimal("40"),
        "current_value": Decimal("40"),
        "target_value": Decimal("80"),
        "weight": Decimal("50"),
    }
    values.update(overrides)
    return OKRKeyResult.objects.create(**values)


def okr_role(employee, role: str = "manager"):
    company = EmployeeWorkInformation._base_manager.get(employee_id=employee.pk).company_id
    return OKRRole.objects.create(company=company, employee=employee, role=role)


def set_reporting_manager(employee, manager):
    EmployeeWorkInformation._base_manager.filter(employee_id=employee.pk).update(reporting_manager_id=manager)
