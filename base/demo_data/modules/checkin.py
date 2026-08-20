"""Dữ liệu mẫu JOYDIGI dành riêng cho hệ thống chấm công."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone

from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceLateComeEarlyOut,
)
from base.models import (
    Announcement,
    CheckInLocation,
    CheckInPolicy,
    Company,
    CompanyGroupAssignment,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    EmployeeType,
    Holidays,
    JobPosition,
    OfficeWifi,
    Roster,
    WorkType,
)
from base.roles import (
    ADMIN_ROLE,
    EMPLOYEE_ROLE,
    LEADER_ROLE,
    STANDARD_ROLE_NAMES,
    ensure_standard_roles,
)
from employee.models import Employee, EmployeeWorkInformation
from joydigi_auth.models import JoydigiUser
from leave.models import AvailableLeave, LeaveRequest, LeaveType


DEMO_MARKER = "joydigi-checkin-v1"
DEFAULT_EMPLOYEE_PASSWORD = "123456"


@dataclass(frozen=True)
class DemoEmployee:
    username: str
    first_name: str
    last_name: str
    department: str
    position: str
    role: str = EMPLOYEE_ROLE
    manager: str | None = None
    remote: bool = False

    @property
    def email(self) -> str:
        return f"{self.username}@joydigi.vn"


DEMO_EMPLOYEES = (
    DemoEmployee("admin", "Minh Anh", "Nguyễn", "Ban Giám đốc", "Giám đốc", ADMIN_ROLE),
    DemoEmployee("lan.phuong", "Lan Phương", "Trần", "Kỹ thuật", "Trưởng nhóm kỹ thuật", LEADER_ROLE, "admin", True),
    DemoEmployee("quoc.huy", "Quốc Huy", "Lê", "Kinh doanh", "Trưởng nhóm kinh doanh", LEADER_ROLE, "admin", True),
    DemoEmployee("hoang.nam", "Hoàng Nam", "Phạm", "Kỹ thuật", "Lập trình viên", manager="lan.phuong", remote=True),
    DemoEmployee("thu.ha", "Thu Hà", "Đỗ", "Kỹ thuật", "Lập trình viên", manager="lan.phuong", remote=True),
    DemoEmployee("gia.bao", "Gia Bảo", "Võ", "Kỹ thuật", "Kiểm thử phần mềm", manager="lan.phuong", remote=True),
    DemoEmployee("ngoc.anh", "Ngọc Anh", "Bùi", "Kỹ thuật", "Thiết kế sản phẩm", manager="lan.phuong", remote=True),
    DemoEmployee("minh.khang", "Minh Khang", "Đặng", "Kinh doanh", "Chuyên viên kinh doanh", manager="quoc.huy"),
    DemoEmployee("thanh.truc", "Thanh Trúc", "Hồ", "Kinh doanh", "Chuyên viên kinh doanh", manager="quoc.huy"),
    DemoEmployee("duc.thinh", "Đức Thịnh", "Ngô", "Kinh doanh", "Chăm sóc khách hàng", manager="quoc.huy"),
    DemoEmployee("mai.chi", "Mai Chi", "Dương", "Hành chính - Nhân sự", "Chuyên viên nhân sự", manager="admin"),
    DemoEmployee("tuan.kiet", "Tuấn Kiệt", "Lý", "Hành chính - Nhân sự", "Hành chính tổng hợp", manager="admin"),
    DemoEmployee("bao.tran", "Bảo Trân", "Huỳnh", "Kế toán", "Kế toán viên", manager="admin"),
    DemoEmployee("nhat.linh", "Nhật Linh", "Trương", "Kế toán", "Kế toán viên", manager="admin"),
    DemoEmployee("phuong.linh", "Phương Linh", "Phan", "Kinh doanh", "Chăm sóc khách hàng", manager="quoc.huy"),
)


def _synchronize_sequences() -> None:
    """Đồng bộ bộ đếm khóa chính sau khi từng nạp fixture có mã cố định."""
    models = (
        JoydigiUser,
        Company,
        Department,
        JobPosition,
        Employee,
        EmployeeWorkInformation,
        Attendance,
        AttendanceActivity,
        AttendanceLateComeEarlyOut,
        LeaveType,
        AvailableLeave,
        LeaveRequest,
        Roster,
        Announcement,
    )
    statements = connection.ops.sequence_reset_sql(no_style(), models)
    if statements:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)


def _ensure_company() -> Company:
    company = Company.objects.filter(company__iexact="JOYDIGI").first()
    if company is None:
        company = Company.objects.filter(company__icontains="joydigi").first()
    values = {
        "company": "JOYDIGI",
        "hq": True,
        "address": "Tòa nhà JOYDIGI, Quận 1",
        "country": "Việt Nam",
        "state": "Thành phố Hồ Chí Minh",
        "city": "Thành phố Hồ Chí Minh",
        "zip": "700000",
        "date_format": "DD/MM/YYYY",
        "time_format": "hh:mm A",
    }
    if company is None:
        company = Company.objects.create(**values)
    else:
        for field, value in values.items():
            setattr(company, field, value)
        company.save()
    return company


def _ensure_named_m2m(model, name_field, name, company, **defaults):
    queryset = model.objects.entire()
    instance = queryset.filter(**{name_field: name}, company_id=company).first()
    if instance is None:
        instance = queryset.filter(**{name_field: name}).first()
    if instance is None:
        instance = queryset.create(**{name_field: name}, **defaults)
    instance.company_id.add(company)
    return instance


def _ensure_organization(company: Company) -> dict:
    department_names = (
        "Ban Giám đốc",
        "Kỹ thuật",
        "Kinh doanh",
        "Hành chính - Nhân sự",
        "Kế toán",
    )
    departments = {
        name: _ensure_named_m2m(Department, "department", name, company)
        for name in department_names
    }

    positions = {}
    for spec in DEMO_EMPLOYEES:
        key = (spec.department, spec.position)
        if key in positions:
            continue
        queryset = JobPosition.objects.entire()
        position = queryset.filter(
            job_position=spec.position,
            department_id=departments[spec.department],
        ).first()
        if position is None:
            position = queryset.create(
                job_position=spec.position,
                department_id=departments[spec.department],
            )
        position.company_id.add(company)
        positions[key] = position

    employee_type = _ensure_named_m2m(
        EmployeeType,
        "employee_type",
        "Nhân viên chính thức",
        company,
    )
    office_type = _ensure_named_m2m(
        WorkType,
        "work_type",
        "Tại văn phòng",
        company,
    )
    remote_type = _ensure_named_m2m(
        WorkType,
        "work_type",
        "Làm việc từ xa",
        company,
    )
    shift = _ensure_named_m2m(
        EmployeeShift,
        "employee_shift",
        "Ca hành chính",
        company,
        weekly_full_time="40:00",
        full_time="176:00",
    )

    shift_days = {}
    for day_name in (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ):
        day = EmployeeShiftDay.objects.entire().filter(
            day=day_name, company_id=company
        ).first()
        if day is None:
            day = EmployeeShiftDay.objects.entire().filter(day=day_name).first()
        if day is None:
            day = EmployeeShiftDay.objects.entire().create(day=day_name)
        day.company_id.add(company)
        shift_days[day_name] = day
        schedule, _ = EmployeeShiftSchedule.objects.entire().update_or_create(
            shift_id=shift,
            day=day,
            defaults={
                "minimum_working_hour": "08:00",
                "start_time": time(8, 30),
                "end_time": time(17, 30),
                "is_night_shift": False,
            },
        )
        schedule.company_id.add(company)

    return {
        "departments": departments,
        "positions": positions,
        "employee_type": employee_type,
        "office_type": office_type,
        "remote_type": remote_type,
        "shift": shift,
        "shift_days": shift_days,
    }


def _ensure_user_and_employee(
    spec: DemoEmployee,
    number: int,
    company: Company,
    organization: dict,
) -> Employee:
    user = JoydigiUser.objects.filter(username=spec.username).first()
    if user is None:
        user = JoydigiUser.objects.filter(email=spec.email).first()
    if user is None:
        user = JoydigiUser(username=spec.username, email=spec.email)
    user.username = spec.username
    user.email = spec.email
    user.first_name = spec.first_name
    user.last_name = spec.last_name
    user.is_active = True
    user.is_staff = spec.role == ADMIN_ROLE
    user.is_superuser = spec.role == ADMIN_ROLE
    user.set_password(
        settings.DB_INIT_PASSWORD
        if spec.role == ADMIN_ROLE
        else DEFAULT_EMPLOYEE_PASSWORD
    )
    user.save()

    employee = Employee.objects.entire().filter(employee_user_id=user).first()
    if employee is None:
        employee = Employee.objects.entire().filter(email=spec.email).first()
    if employee is None:
        employee = Employee(employee_user_id=user)
    employee.employee_user_id = user
    employee.badge_id = f"JD{number:03d}"
    employee.employee_first_name = spec.first_name
    employee.employee_last_name = spec.last_name
    employee.email = spec.email
    employee.phone = f"090000{number:04d}"
    employee.gender = "female" if number in {2, 5, 7, 9, 11, 13, 14, 15} else "male"
    employee.is_active = True
    employee.save()

    work_info, _ = EmployeeWorkInformation.objects.entire().get_or_create(
        employee_id=employee
    )
    work_info.department_id = organization["departments"][spec.department]
    work_info.job_position_id = organization["positions"][(spec.department, spec.position)]
    work_info.shift_id = organization["shift"]
    work_info.work_type_id = organization["office_type"]
    work_info.employee_type_id = organization["employee_type"]
    work_info.company_id = company
    work_info.email = spec.email
    work_info.mobile = employee.phone
    work_info.location = "Văn phòng JOYDIGI"
    work_info.allow_remote = spec.remote
    work_info.date_joining = date(2024, 1, 8) + timedelta(days=number * 17)
    work_info.save()

    ensure_standard_roles()
    role_group = Group.objects.get(name=spec.role)
    other_roles = Group.objects.filter(name__in=STANDARD_ROLE_NAMES).exclude(
        pk=role_group.pk
    )
    user.groups.remove(*other_roles)
    user.groups.add(role_group)
    CompanyGroupAssignment.objects.filter(
        user=user, company=company, group__name__in=STANDARD_ROLE_NAMES
    ).exclude(group=role_group).delete()
    CompanyGroupAssignment.objects.get_or_create(
        user=user,
        company=company,
        group=role_group,
    )
    return employee


def _ensure_people(company: Company, organization: dict) -> dict[str, Employee]:
    employees = {
        spec.username: _ensure_user_and_employee(
            spec, number, company, organization
        )
        for number, spec in enumerate(DEMO_EMPLOYEES, start=1)
    }
    for spec in DEMO_EMPLOYEES:
        work_info = EmployeeWorkInformation.objects.entire().get(
            employee_id=employees[spec.username]
        )
        work_info.reporting_manager_id = (
            employees[spec.manager] if spec.manager else None
        )
        work_info.save()
        # Employee.save() có thể đã lưu bộ nhớ đệm của quan hệ work_info trước
        # khi các trường công ty/phòng ban được bổ sung.
        employees[spec.username].refresh_from_db()
    return employees


def _ensure_checkin_settings(company: Company) -> None:
    CheckInPolicy.objects.update_or_create(
        company_id=company,
        defaults={
            "late_threshold_minutes": 10,
            "annual_leave_days": 12,
            "allow_remote": True,
            "allow_outside_radius_request": True,
        },
    )
    CheckInLocation.objects.update_or_create(
        company_id=company,
        name="Văn phòng JOYDIGI",
        defaults={
            "latitude": "10.776889",
            "longitude": "106.700806",
            "radius_meters": 250,
            "is_active": True,
        },
    )
    OfficeWifi.objects.update_or_create(
        company_id=company,
        ssid="JoyDigi-Office",
        defaults={
            "name": "Wifi văn phòng chính",
            "bssid": "",
            "is_active": True,
        },
    )


def _ensure_leave_data(
    company: Company,
    employees: dict[str, Employee],
    today: date,
) -> set[tuple[int, date]]:
    leave_type = LeaveType.objects.entire().filter(
        company_id=company, name="Nghỉ phép năm"
    ).first()
    if leave_type is None:
        leave_type = LeaveType.objects.entire().create(
            company_id=company,
            name="Nghỉ phép năm",
            payment="paid",
            payment_type="paid",
            limit_leave=True,
            total_days=12,
            require_approval="yes",
            exclude_company_leave="yes",
            exclude_holiday="yes",
        )

    for employee in employees.values():
        AvailableLeave.objects.entire().update_or_create(
            employee_id=employee,
            leave_type_id=leave_type,
            defaults={
                "available_days": 10,
                "carryforward_days": 0,
                "total_leave_days": 12,
            },
        )

    admin = employees["admin"]
    approved_employee = employees["nhat.linh"]
    LeaveRequest.objects.entire().update_or_create(
        employee_id=approved_employee,
        description=f"[{DEMO_MARKER}] Nghỉ việc gia đình",
        defaults={
            "leave_type_id": leave_type,
            "start_date": today,
            "end_date": today,
            "start_date_breakdown": "full_day",
            "end_date_breakdown": "full_day",
            "status": "approved",
            "created_by": approved_employee,
            "approved_available_days": 1,
        },
    )

    future_start = today + timedelta(days=3)
    while future_start.weekday() >= 5:
        future_start += timedelta(days=1)
    requester = employees["thanh.truc"]
    LeaveRequest.objects.entire().update_or_create(
        employee_id=requester,
        description=f"[{DEMO_MARKER}] Khám sức khỏe định kỳ",
        defaults={
            "leave_type_id": leave_type,
            "start_date": future_start,
            "end_date": future_start,
            "start_date_breakdown": "full_day",
            "end_date_breakdown": "full_day",
            "status": "requested",
            "created_by": requester,
        },
    )

    past_day = today.replace(day=1) - timedelta(days=7)
    while past_day.weekday() >= 5:
        past_day -= timedelta(days=1)
    past_employee = employees["gia.bao"]
    LeaveRequest.objects.entire().update_or_create(
        employee_id=past_employee,
        description=f"[{DEMO_MARKER}] Nghỉ phép cá nhân",
        defaults={
            "leave_type_id": leave_type,
            "start_date": past_day,
            "end_date": past_day,
            "start_date_breakdown": "full_day",
            "end_date_breakdown": "full_day",
            "status": "approved",
            "created_by": admin,
            "approved_available_days": 1,
        },
    )
    attendance_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    leave_days = set()
    approved_requests = LeaveRequest.objects.entire().filter(
        employee_id__in=employees.values(),
        status="approved",
        start_date__lte=today,
        end_date__gte=attendance_start,
    )
    for request in approved_requests:
        for leave_day in request.requested_dates():
            if attendance_start <= leave_day <= today:
                leave_days.add((request.employee_id_id, leave_day))
    return leave_days


def _aware(day: date, value: time):
    combined = datetime.combine(day, value)
    return timezone.make_aware(combined) if settings.USE_TZ else combined


def _ensure_attendance(
    employees: dict[str, Employee],
    organization: dict,
    leave_days: set[tuple[int, date]],
    today: date,
) -> tuple[int, date]:
    start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    employee_list = [employees[spec.username] for spec in DEMO_EMPLOYEES]
    created_or_updated = 0
    current = start
    while current <= today:
        if current.weekday() < 5:
            day_name = current.strftime("%A").lower()
            day_model = organization["shift_days"][day_name]
            for index, employee in enumerate(employee_list, start=1):
                if (employee.pk, current) in leave_days:
                    continue
                if current == today:
                    absent = index == 15
                else:
                    absent = (current.toordinal() + index * 3) % 29 == 0
                if absent:
                    continue

                late = index in {4, 9} if current == today else (
                    current.toordinal() + index * 5
                ) % 13 == 0
                remote = (
                    employee.employee_work_info.allow_remote
                    and (
                        index == 5
                        if current == today
                        else (current.toordinal() + index) % 11 == 0
                    )
                )
                minute = 42 + index % 7 if late else 18 + index % 10
                clock_in = time(8, minute)
                clock_out = time(17, 32 + index % 13)
                worked_seconds = int(
                    (
                        datetime.combine(current, clock_out)
                        - datetime.combine(current, clock_in)
                    ).total_seconds()
                )
                attendance, _ = Attendance.objects.entire().update_or_create(
                    employee_id=employee,
                    attendance_date=current,
                    defaults={
                        "shift_id": organization["shift"],
                        "work_type_id": (
                            organization["remote_type"]
                            if remote
                            else organization["office_type"]
                        ),
                        "attendance_day": day_model,
                        "attendance_clock_in_date": current,
                        "attendance_clock_in": clock_in,
                        "attendance_clock_out_date": current,
                        "attendance_clock_out": clock_out,
                        "attendance_worked_hour": f"{worked_seconds // 3600:02d}:{(worked_seconds % 3600) // 60:02d}",
                        "minimum_hour": "08:00",
                        "attendance_overtime": "00:00",
                        "attendance_validated": True,
                        "at_work_second": worked_seconds,
                        "requested_data": {
                            "demo_seed": DEMO_MARKER,
                            "method": "remote" if remote else "office",
                        },
                    },
                )
                activity = AttendanceActivity.objects.entire().filter(
                    employee_id=employee, attendance_date=current
                ).first()
                if activity is None:
                    activity = AttendanceActivity(
                        employee_id=employee,
                        attendance_date=current,
                    )
                activity.shift_day = day_model
                activity.clock_in_date = current
                activity.clock_in = clock_in
                activity.clock_out_date = current
                activity.clock_out = clock_out
                activity.in_datetime = _aware(current, clock_in)
                activity.out_datetime = _aware(current, clock_out)
                activity.save()
                if late:
                    late_entry = AttendanceLateComeEarlyOut.objects.entire().filter(
                        attendance_id=attendance,
                        type="late_come",
                    ).first()
                    if late_entry is None:
                        # Model này tự lưu hai lần để bổ sung employee_id nên
                        # không dùng get_or_create (Django truyền force_insert).
                        AttendanceLateComeEarlyOut(
                            attendance_id=attendance,
                            type="late_come",
                        ).save()
                created_or_updated += 1
        current += timedelta(days=1)

    outside_employee = employees["tuan.kiet"]
    outside = Attendance.objects.entire().filter(
        employee_id=outside_employee,
        attendance_date=today,
    ).first()
    if outside:
        outside.is_validate_request = True
        outside.is_validate_request_approved = False
        outside.request_description = "Gặp khách hàng ngoài văn phòng"
        outside.requested_data = {
            "demo_seed": DEMO_MARKER,
            "outside_radius": True,
            "distance": 680,
            "radius": 250,
        }
        outside.save(
            update_fields=[
                "is_validate_request",
                "is_validate_request_approved",
                "request_description",
                "requested_data",
            ]
        )
    return created_or_updated, start


def _ensure_roster_and_content(
    company: Company,
    employees: dict[str, Employee],
    organization: dict,
    today: date,
) -> None:
    week_start = today - timedelta(days=today.weekday())
    admin = employees["admin"]
    for offset in range(14):
        roster_day = week_start + timedelta(days=offset)
        is_off = roster_day.weekday() >= 5
        for employee in employees.values():
            Roster.objects.entire().update_or_create(
                employee=employee,
                date=roster_day,
                defaults={
                    "shift": None if is_off else organization["shift"],
                    "department": employee.employee_work_info.department_id,
                    "is_published": True,
                    "is_off": is_off,
                    "notes": "Nghỉ cuối tuần" if is_off else "",
                    "created_by": admin,
                },
            )

    announcements = (
        (
            "Chào mừng đến với hệ thống chấm công JOYDIGI",
            "Nhân viên vui lòng kiểm tra lịch làm việc và chấm công đúng giờ mỗi ngày.",
            True,
        ),
        (
            "Nhắc kiểm tra bảng công tháng này",
            "Vui lòng rà soát giờ vào, giờ ra và gửi giải trình trước ngày chốt công.",
            True,
        ),
        (
            "Lịch làm việc tuần mới đã được cập nhật",
            "Các nhóm kiểm tra lịch làm việc và báo trưởng nhóm nếu cần thay đổi.",
            False,
        ),
    )
    for title, description, pinned in announcements:
        announcement = Announcement.objects.entire().filter(title=title).first()
        if announcement is None:
            announcement = Announcement(title=title)
        announcement.description = description
        announcement.expire_date = today + timedelta(days=45)
        announcement.is_pinned = pinned
        announcement.send_notification = True
        announcement.save()
        announcement.company_id.add(company)

    Holidays.objects.entire().update_or_create(
        company_id=company,
        name="Quốc khánh",
        start_date=date(today.year, 9, 2),
        defaults={
            "end_date": date(today.year, 9, 2),
            "recurring": True,
            "is_specific": False,
        },
    )


@transaction.atomic
def seed_joydigi_checkin_demo(today: date | None = None) -> dict:
    """Tạo dữ liệu mẫu JOYDIGI; có thể chạy lại mà không nhân đôi dữ liệu."""
    today = today or timezone.localdate()
    _synchronize_sequences()
    company = _ensure_company()
    organization = _ensure_organization(company)
    employees = _ensure_people(company, organization)
    _ensure_checkin_settings(company)
    leave_days = _ensure_leave_data(company, employees, today)
    attendance_count, attendance_start = _ensure_attendance(
        employees, organization, leave_days, today
    )
    _ensure_roster_and_content(company, employees, organization, today)
    return {
        "company_id": company.pk,
        "company": company.company,
        "employees": len(employees),
        "attendance_records": attendance_count,
        "attendance_from": attendance_start.isoformat(),
        "attendance_to": today.isoformat(),
        "admin_username": "admin",
    }
