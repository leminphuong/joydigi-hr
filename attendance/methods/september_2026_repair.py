"""One-off, idempotent repair for the September 2026 attendance incident."""

from dataclasses import dataclass, field
from datetime import date
import unicodedata

from django.db import transaction
from django.db.models import Q

from attendance.methods.utils import strtime_seconds
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceConflictResolution,
    AttendanceDailyHours,
    AttendanceExplanationRequest,
    AttendanceLateEarlyRequest,
    OvertimeRequest,
    RemoteWorkRequest,
    WorkRecords,
)
from base.models import (
    Company,
    CompanyLeaves,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    Holidays,
    Roster,
)
from employee.models import Employee
from leave.models import LeaveRequest


NATIONAL_DAY_START = date(2027, 9, 1)
NATIONAL_DAY_END = date(2027, 9, 2)
FULL_DAY_REPAIRS = (
    (date(2026, 9, 3), "Chưa có hệ thống chấm công", "system_unavailable"),
    (date(2026, 9, 15), "Làm online – không chấm công", "remote_work"),
    (date(2026, 9, 16), "Làm online – không chấm công", "remote_work"),
)
OT_REQUEST_DATES = (date(2026, 9, 5), date(2026, 9, 12))
MOVE_SOURCE_DATE = date(2026, 9, 14)
MOVE_DESTINATION_DATE = date(2026, 9, 19)
SHORT_AUDIT_START = date(2026, 9, 1)
SHORT_AUDIT_END = date(2026, 9, 16)
FIVE_HOURS_SECONDS = 5 * 60 * 60


@dataclass
class RepairReport:
    holidays_created: int = 0
    holidays_updated: int = 0
    full_days_credited: int = 0
    full_days_unchanged: int = 0
    full_days_skipped_leave: int = 0
    full_days_skipped_non_working: int = 0
    schedule_conflicts: list = field(default_factory=list)
    ot_requests_approved: int = 0
    attendance_rows_moved: int = 0
    move_conflicts: list = field(default_factory=list)
    short_attendance: list = field(default_factory=list)


def _plain_text(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(
        char for char in normalized if not unicodedata.combining(char)
    ).lower()


def _duration_seconds(value):
    try:
        return strtime_seconds(value or "00:00")
    except (AttributeError, TypeError, ValueError):
        return 0


def _duration_text(seconds):
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def _national_day_candidate(company):
    holidays = Holidays.objects.entire().filter(
        company_id=company,
        start_date__year=NATIONAL_DAY_START.year,
    )
    for holiday in holidays.order_by("id"):
        normalized_name = _plain_text(holiday.name)
        end_date = holiday.end_date or holiday.start_date
        name_matches = any(
            label in normalized_name
            for label in ("quoc khanh", "national day", "independence day")
        )
        dates_overlap = (
            holiday.start_date <= NATIONAL_DAY_END
            and end_date >= NATIONAL_DAY_START
        )
        if name_matches and dates_overlap:
            return holiday
    return None


def _employees_for(day):
    """Employees employed by a company on the incident date, active or archived."""
    return (
        Employee.objects.entire()
        .filter(
            Q(employee_work_info__date_joining__isnull=True)
            | Q(employee_work_info__date_joining__lte=day),
            Q(employee_work_info__contract_end_date__isnull=True)
            | Q(employee_work_info__contract_end_date__gte=day),
            employee_work_info__company_id__isnull=False,
        )
        .select_related(
            "employee_work_info__shift_id",
            "employee_work_info__work_type_id",
        )
        .distinct()
    )


def _published_roster(employee, day):
    return (
        Roster.objects.entire()
        .filter(employee=employee, date=day, is_published=True)
        .select_related("shift")
        .first()
    )


def _is_company_weekly_off(employee, day):
    company_id = employee.employee_work_info.company_id_id
    if not company_id:
        return False
    first_day = day.replace(day=1)
    week_number = str((day.day + first_day.weekday() - 1) // 7)
    return (
        CompanyLeaves.objects.entire()
        .filter(
            company_id=company_id,
            based_on_week_day=str(day.weekday()),
        )
        .filter(
            Q(based_on_week__isnull=True)
            | Q(based_on_week="")
            | Q(based_on_week=week_number)
        )
        .exists()
    )


def _schedule_for(employee, day, *, attendance=None, roster=None):
    work_info = employee.employee_work_info
    if attendance is not None and attendance.shift_id_id:
        shift_id = attendance.shift_id_id
    elif roster is not None and roster.shift_id:
        shift_id = roster.shift_id
    else:
        shift_id = work_info.shift_id_id
    if not shift_id:
        return None
    return (
        EmployeeShiftSchedule.objects.entire()
        .filter(
            shift_id_id=shift_id,
            day__day=day.strftime("%A").lower(),
        )
        .select_related("day", "shift_id")
        .first()
    )


def _approved_leave(employee, day):
    return (
        LeaveRequest.objects.entire()
        .filter(
            employee_id=employee,
            status="approved",
            is_active=True,
            start_date__lte=day,
        )
        .filter(Q(end_date__gte=day) | Q(end_date__isnull=True, start_date=day))
        .select_related("leave_type_id")
        .first()
    )


def _prefer_leave_for_existing_attendance(employee, day, leave):
    if not Attendance.objects.entire().filter(
        employee_id=employee,
        attendance_date=day,
    ).exists():
        return
    resolution = (
        "paid_leave" if leave.leave_type_id.payment == "paid" else "unpaid_leave"
    )
    conflict = AttendanceConflictResolution.objects.entire().filter(
        employee_id=employee,
        date=day,
    ).first()
    if conflict is None:
        AttendanceConflictResolution.objects.entire().create(
            employee_id=employee,
            date=day,
            resolution=resolution,
            conflict_type=resolution,
        )
    elif conflict.resolution != resolution or conflict.conflict_type != resolution:
        conflict.resolution = resolution
        conflict.conflict_type = resolution
        conflict.save(update_fields=["resolution", "conflict_type"])


def _credit_full_day(
    employee,
    day,
    note,
    conflict_type,
    *,
    attendance,
    schedule,
    apply,
):
    """Credit the day without fabricating punch events; return whether it changed."""
    is_new_attendance = attendance is None
    if is_new_attendance:
        minimum_hour = schedule.minimum_working_hour
        attendance = Attendance(
            employee_id=employee,
            attendance_date=day,
            attendance_day=schedule.day,
            shift_id=schedule.shift_id,
            work_type_id=employee.employee_work_info.work_type_id,
            minimum_hour=minimum_hour,
        )
    else:
        # Historical metadata belongs to that day, not the employee's current setup.
        minimum_hour = attendance.minimum_hour

    daily_hours = AttendanceDailyHours.objects.entire().filter(
        employee_id=employee,
        date=day,
    ).first()
    conflict = AttendanceConflictResolution.objects.entire().filter(
        employee_id=employee,
        date=day,
    ).first()
    work_record = WorkRecords.objects.entire().filter(
        employee_id=employee,
        date=day,
    ).first()

    minimum_seconds = _duration_seconds(minimum_hour)
    scheduled_seconds = _duration_seconds(schedule.minimum_working_hour)
    current_seconds = max(
        attendance.at_work_second or 0,
        _duration_seconds(attendance.attendance_worked_hour),
    )
    credited_seconds = max(
        current_seconds,
        daily_hours.hours_second if daily_hours is not None else 0,
        work_record.at_work_second if work_record is not None else 0,
        scheduled_seconds,
    )
    credited_hours = _duration_text(credited_seconds)

    attendance_values = {
        "attendance_worked_hour": credited_hours,
        "attendance_validated": True,
        "is_validate_request": False,
        "is_validate_request_approved": False,
        "request_description": note,
    }
    attendance_changed = is_new_attendance or any(
        getattr(attendance, field_name) != value
        for field_name, value in attendance_values.items()
    )
    daily_changed = daily_hours is None or (
        daily_hours.hours_second != credited_seconds
        or not daily_hours.is_manually_edited
    )
    conflict_changed = conflict is None or (
        conflict.resolution != "full_present"
        or conflict.conflict_type != conflict_type
    )
    work_record_values = {
        "work_record_type": "FDP",
        "at_work": credited_hours,
        "min_hour": minimum_hour,
        "at_work_second": credited_seconds,
        "min_hour_second": minimum_seconds,
        "note": note,
        "message": "Present",
        "day_percentage": 1.0,
        "is_attendance_record": True,
        "shift_id_id": attendance.shift_id_id,
    }
    work_record_changed = work_record is None or any(
        getattr(work_record, field_name) != value
        for field_name, value in work_record_values.items()
    ) or (
        not is_new_attendance and work_record.attendance_id_id != attendance.pk
    )
    changed = (
        attendance_changed
        or daily_changed
        or conflict_changed
        or work_record_changed
    )
    if not apply or not changed:
        return changed

    if attendance_changed:
        for field_name, value in attendance_values.items():
            setattr(attendance, field_name, value)
        attendance.save()

    if daily_hours is None:
        AttendanceDailyHours.objects.entire().create(
            employee_id=employee,
            date=day,
            hours_second=credited_seconds,
            is_manually_edited=True,
        )
    elif daily_changed:
        daily_hours.hours_second = credited_seconds
        daily_hours.is_manually_edited = True
        daily_hours.save(
            update_fields=["hours_second", "is_manually_edited", "modified_at"]
        )

    if conflict is None:
        AttendanceConflictResolution.objects.entire().create(
            employee_id=employee,
            date=day,
            resolution="full_present",
            conflict_type=conflict_type,
        )
    elif conflict_changed:
        conflict.resolution = "full_present"
        conflict.conflict_type = conflict_type
        conflict.save(update_fields=["resolution", "conflict_type"])

    # Attendance's existing signal creates/updates WorkRecords, so reload it.
    work_record = WorkRecords.objects.entire().filter(
        employee_id=employee,
        date=day,
    ).first()
    work_record_values["attendance_id_id"] = attendance.pk
    if work_record is None:
        WorkRecords.objects.entire().create(
            employee_id=employee,
            date=day,
            **work_record_values,
        )
    elif any(
        getattr(work_record, field_name) != value
        for field_name, value in work_record_values.items()
    ):
        for field_name, value in work_record_values.items():
            setattr(work_record, field_name, value)
        work_record.save()

    return True


def _destination_has_attendance_data(employee):
    checks = (
        (Attendance.objects.entire(), "attendance_date"),
        (AttendanceActivity.objects.entire(), "attendance_date"),
        (WorkRecords.objects.entire(), "date"),
        (AttendanceDailyHours.objects.entire(), "date"),
        (AttendanceConflictResolution.objects.entire(), "date"),
    )
    return any(
        queryset.filter(
            employee_id=employee,
            **{date_field: MOVE_DESTINATION_DATE},
        ).exists()
        for queryset, date_field in checks
    )


def _source_employee_ids():
    checks = (
        (Attendance.objects.entire(), "attendance_date"),
        (AttendanceActivity.objects.entire(), "attendance_date"),
        (WorkRecords.objects.entire(), "date"),
        (AttendanceDailyHours.objects.entire(), "date"),
        (AttendanceConflictResolution.objects.entire(), "date"),
    )
    employee_ids = set()
    for queryset, date_field in checks:
        employee_ids.update(
            queryset.filter(**{date_field: MOVE_SOURCE_DATE}).values_list(
                "employee_id_id", flat=True
            )
        )
    return employee_ids


def _move_attendance_data(report, *, apply):
    delta = MOVE_DESTINATION_DATE - MOVE_SOURCE_DATE
    saturday = EmployeeShiftDay.objects.get(day="saturday")
    movable_attendance_ids = set()
    employees = Employee.objects.entire().filter(pk__in=_source_employee_ids())

    for employee in employees.order_by("pk"):
        attendance = Attendance.objects.entire().filter(
            employee_id=employee,
            attendance_date=MOVE_SOURCE_DATE,
        ).first()
        if _destination_has_attendance_data(employee):
            report.move_conflicts.append(
                {
                    "employee_id": employee.pk,
                    "badge_id": employee.badge_id,
                    "reason": "destination_has_data",
                }
            )
            continue

        if attendance is not None:
            report.attendance_rows_moved += 1
            movable_attendance_ids.add(attendance.pk)
        if not apply:
            continue

        for activity in AttendanceActivity.objects.entire().filter(
            employee_id=employee,
            attendance_date=MOVE_SOURCE_DATE,
        ):
            activity.attendance_date = MOVE_DESTINATION_DATE
            activity.shift_day = saturday
            if activity.clock_in_date:
                activity.clock_in_date += delta
            if activity.clock_out_date:
                activity.clock_out_date += delta
            if activity.in_datetime:
                activity.in_datetime += delta
            if activity.out_datetime:
                activity.out_datetime += delta
            activity.save(
                update_fields=[
                    "attendance_date",
                    "shift_day",
                    "clock_in_date",
                    "clock_out_date",
                    "in_datetime",
                    "out_datetime",
                ]
            )

        AttendanceDailyHours.objects.entire().filter(
            employee_id=employee,
            date=MOVE_SOURCE_DATE,
        ).update(date=MOVE_DESTINATION_DATE)
        AttendanceConflictResolution.objects.entire().filter(
            employee_id=employee,
            date=MOVE_SOURCE_DATE,
        ).update(date=MOVE_DESTINATION_DATE)
        WorkRecords.objects.entire().filter(
            employee_id=employee,
            date=MOVE_SOURCE_DATE,
        ).update(date=MOVE_DESTINATION_DATE)

        if attendance is not None:
            Attendance.objects.entire().filter(pk=attendance.pk).update(
                attendance_date=MOVE_DESTINATION_DATE,
                attendance_day=saturday,
                attendance_clock_in_date=(
                    attendance.attendance_clock_in_date + delta
                    if attendance.attendance_clock_in_date
                    else None
                ),
                attendance_clock_out_date=(
                    attendance.attendance_clock_out_date + delta
                    if attendance.attendance_clock_out_date
                    else None
                ),
            )

    return movable_attendance_ids


def _has_permission_request(attendance):
    employee = attendance.employee_id
    day = attendance.attendance_date
    if (
        attendance.is_validate_request
        or attendance.is_validate_request_approved
        or bool((attendance.request_description or "").strip())
    ):
        return True

    if (
        LeaveRequest.objects.entire()
        .filter(
            employee_id=employee,
            status__in=("requested", "approved"),
            is_active=True,
            start_date__lte=day,
        )
        .filter(Q(end_date__gte=day) | Q(end_date__isnull=True, start_date=day))
        .exists()
    ):
        return True

    if AttendanceLateEarlyRequest.objects.entire().filter(
        employee_id=employee,
        request_date=day,
        canceled=False,
        is_active=True,
    ).exists():
        return True

    if AttendanceExplanationRequest.objects.entire().filter(
        employee_id=employee,
        request_date=day,
        canceled=False,
        is_active=True,
    ).exists():
        return True

    if RemoteWorkRequest.objects.entire().filter(
        employee_id=employee,
        start_date__lte=day,
        end_date__gte=day,
        canceled=False,
        is_active=True,
    ).exists():
        return True

    return OvertimeRequest.objects.entire().filter(
        employee_id=employee,
        request_date=day,
        canceled=False,
        is_active=True,
    ).exists()


def _find_short_attendance(*, excluded_attendance_ids=()):
    rows = []
    attendances = (
        Attendance.objects.entire()
        .filter(attendance_date__range=(SHORT_AUDIT_START, SHORT_AUDIT_END))
        .exclude(pk__in=excluded_attendance_ids)
        .select_related("employee_id")
        .order_by("attendance_date", "employee_id__badge_id", "employee_id_id")
    )
    for attendance in attendances:
        worked_seconds = attendance.at_work_second
        if worked_seconds is None:
            worked_seconds = _duration_seconds(attendance.attendance_worked_hour)
        if worked_seconds >= FIVE_HOURS_SECONDS or _has_permission_request(attendance):
            continue
        rows.append(
            {
                "attendance_id": attendance.pk,
                "employee_id": attendance.employee_id_id,
                "badge_id": attendance.employee_id.badge_id,
                "employee_name": str(attendance.employee_id),
                "date": attendance.attendance_date,
                "worked_seconds": worked_seconds,
                "worked_hours": attendance.attendance_worked_hour or "00:00",
                "clock_in": attendance.attendance_clock_in,
                "clock_out": attendance.attendance_clock_out,
            }
        )
    return rows


@transaction.atomic
def repair_september_2026(*, apply=False):
    report = RepairReport()

    for company in Company.objects.all().order_by("id"):
        holiday = _national_day_candidate(company)
        if holiday is None:
            report.holidays_created += 1
            if apply:
                Holidays.objects.entire().create(
                    name="Quốc khánh",
                    start_date=NATIONAL_DAY_START,
                    end_date=NATIONAL_DAY_END,
                    recurring=False,
                    is_specific=False,
                    company_id=company,
                )
            continue

        has_assignments = (
            holiday.department.exists()
            or holiday.job_position.exists()
            or holiday.employees.exists()
        )
        needs_update = (
            holiday.name != "Quốc khánh"
            or holiday.start_date != NATIONAL_DAY_START
            or holiday.end_date != NATIONAL_DAY_END
            or holiday.is_specific
            or holiday.recurring
            or holiday.assigning_type is not None
            or has_assignments
        )
        if needs_update:
            report.holidays_updated += 1
            if apply:
                holiday.name = "Quốc khánh"
                holiday.start_date = NATIONAL_DAY_START
                holiday.end_date = NATIONAL_DAY_END
                holiday.recurring = False
                holiday.is_specific = False
                holiday.assigning_type = None
                holiday.save()
                holiday.department.clear()
                holiday.job_position.clear()
                holiday.employees.clear()

    audit_excluded_ids = set()
    for day, note, conflict_type in FULL_DAY_REPAIRS:
        for employee in _employees_for(day):
            attendance = Attendance.objects.entire().filter(
                employee_id=employee,
                attendance_date=day,
            ).first()
            leave = _approved_leave(employee, day)
            if leave:
                report.full_days_skipped_leave += 1
                if apply:
                    _prefer_leave_for_existing_attendance(employee, day, leave)
                continue

            roster = _published_roster(employee, day)
            is_explicit_working_roster = bool(
                roster is not None and not roster.is_off and roster.shift_id
            )
            if (roster is not None and roster.is_off) or (
                not is_explicit_working_roster
                and _is_company_weekly_off(employee, day)
            ):
                report.full_days_skipped_non_working += 1
                continue

            schedule = _schedule_for(
                employee,
                day,
                attendance=attendance,
                roster=roster,
            )
            if schedule is None or _duration_seconds(
                schedule.minimum_working_hour
            ) <= 0:
                report.schedule_conflicts.append(
                    {
                        "employee_id": employee.pk,
                        "badge_id": employee.badge_id,
                        "date": day,
                        "reason": "missing_work_schedule",
                    }
                )
                continue

            if attendance is not None:
                audit_excluded_ids.add(attendance.pk)
            changed = _credit_full_day(
                employee,
                day,
                note,
                conflict_type,
                attendance=attendance,
                schedule=schedule,
                apply=apply,
            )
            if changed:
                report.full_days_credited += 1
            else:
                report.full_days_unchanged += 1

    overtime_requests = OvertimeRequest.objects.entire().filter(
        request_date__in=OT_REQUEST_DATES,
        canceled=False,
        approved=False,
        is_active=True,
    )
    report.ot_requests_approved = overtime_requests.count()
    if apply:
        overtime_requests.update(approved=True)

    audit_excluded_ids.update(_move_attendance_data(report, apply=apply))
    report.short_attendance = _find_short_attendance(
        excluded_attendance_ids=audit_excluded_ids
    )

    return report
