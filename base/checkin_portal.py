"""Các màn hình chấm công tinh gọn dùng cho menu chính."""

import json
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from base.forms import CheckInLocationForm, CheckInPolicyForm, OfficeWifiForm
from base.models import CheckInLocation, CheckInPolicy, Company, OfficeWifi


def _current_company(request):
    employee = getattr(request.user, "employee_get", None)
    company = employee.get_company() if employee else None
    return company or Company.objects.filter(hq=True).first() or Company.objects.first()


def _visible_employees(request):
    from employee.models import Employee

    employees = Employee.objects.filter(is_active=True).select_related(
        "employee_work_info__department_id", "employee_work_info__company_id"
    )
    employee = getattr(request.user, "employee_get", None)
    if request.user.is_superuser or request.user.has_perm("attendance.view_attendance"):
        return employees
    if employee and Employee.objects.filter(
        employee_work_info__reporting_manager_id=employee, is_active=True
    ).exists():
        return employees.filter(
            Q(pk=employee.pk) | Q(employee_work_info__reporting_manager_id=employee)
        )
    return employees.filter(pk=getattr(employee, "pk", None))


def _working_days_ending_today(number=7):
    days = []
    current = date.today()
    while len(days) < number:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    return list(reversed(days))


def get_attendance_overview(request):
    """Dữ liệu chung cho Trang tổng quan và Chấm công hôm nay."""
    from attendance.models import Attendance, AttendanceLateComeEarlyOut
    from leave.models import LeaveRequest

    today = date.today()
    employees = _visible_employees(request)
    employee_ids = list(employees.values_list("pk", flat=True))
    attendances = list(
        Attendance.objects.filter(attendance_date=today, employee_id_id__in=employee_ids)
        .select_related("employee_id", "work_type_id", "employee_id__employee_work_info__department_id")
        .order_by("employee_id__employee_first_name", "employee_id__employee_last_name")
    )
    attendance_ids = [item.pk for item in attendances]
    late_attendance_ids = set(
        AttendanceLateComeEarlyOut.objects.filter(
            attendance_id_id__in=attendance_ids, type="late_come"
        ).values_list("attendance_id_id", flat=True)
    )
    leave_requests = LeaveRequest.objects.filter(
        employee_id_id__in=employee_ids,
        status="approved",
        start_date__lte=today,
    ).filter(Q(end_date__gte=today) | Q(end_date__isnull=True, start_date=today))
    leave_ids = set(leave_requests.values_list("employee_id_id", flat=True))

    remote_words = ("remote", "từ xa", "tai nha", "tại nhà", "home")
    remote_ids = set()
    log_rows = []
    for attendance in attendances:
        work_type = str(attendance.work_type_id or "Tại văn phòng")
        if any(word in work_type.lower() for word in remote_words):
            remote_ids.add(attendance.employee_id_id)
        if attendance.pk in late_attendance_ids:
            status = "Đi muộn"
            status_color = "amber"
        elif attendance.attendance_clock_out:
            status = "Đã hoàn thành"
            status_color = "green"
        else:
            status = "Đang làm việc"
            status_color = "blue"
        log_rows.append(
            {
                "employee": attendance.employee_id,
                "clock_in": attendance.attendance_clock_in,
                "clock_out": attendance.attendance_clock_out,
                "method": work_type,
                "status": status,
                "status_color": status_color,
            }
        )

    attended_ids = {item.employee_id_id for item in attendances}
    total = len(employee_ids)
    absent = max(total - len(attended_ids) - len(leave_ids - attended_ids), 0)

    groups = {}
    for employee in employees:
        department = getattr(getattr(employee, "employee_work_info", None), "department_id", None)
        key = department.pk if department else 0
        groups.setdefault(
            key,
            {"name": str(department or "Chưa xếp nhóm"), "total": 0, "attended": 0, "late": 0, "remote": 0, "leave": 0, "leave_without_attendance": 0},
        )
        row = groups[key]
        row["total"] += 1
        row["attended"] += employee.pk in attended_ids
        row["leave"] += employee.pk in leave_ids
        row["leave_without_attendance"] += employee.pk in leave_ids and employee.pk not in attended_ids
        row["remote"] += employee.pk in remote_ids
    for attendance in attendances:
        department = getattr(getattr(attendance.employee_id, "employee_work_info", None), "department_id", None)
        if attendance.pk in late_attendance_ids:
            groups[department.pk if department else 0]["late"] += 1
    for row in groups.values():
        row["absent"] = max(row["total"] - row["attended"] - row["leave_without_attendance"], 0)

    trend_labels, trend_rates = [], []
    for day in _working_days_ending_today():
        day_attendances = Attendance.objects.filter(
            attendance_date=day, employee_id_id__in=employee_ids
        )
        day_count = day_attendances.count()
        late_count = AttendanceLateComeEarlyOut.objects.filter(
            attendance_id__in=day_attendances, type="late_come"
        ).count()
        trend_labels.append(day.strftime("%d/%m"))
        trend_rates.append(round((late_count * 100 / day_count), 1) if day_count else 0)

    return {
        "today_summary": {
            "total": total,
            "attended": len(attended_ids),
            "late": len(late_attendance_ids),
            "remote": len(remote_ids),
            "absent": absent,
            "leave": len(leave_ids),
        },
        "today_logs": log_rows,
        "today_groups": list(groups.values()),
        "late_trend_labels": trend_labels,
        "late_trend_rates": trend_rates,
        "pending_leave_count": LeaveRequest.objects.filter(
            employee_id_id__in=employee_ids, status="requested"
        ).count(),
    }


@login_required
def today_attendance(request):
    return render(request, "checkin/today_attendance.html", get_attendance_overview(request))


def _request_data(attendance):
    data = attendance.requested_data or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            data = {}
    return data if isinstance(data, dict) else {}


def _outside_radius(attendance):
    data = _request_data(attendance)
    if data.get("outside_radius") or data.get("is_outside_radius"):
        return True
    try:
        if float(data.get("distance", 0)) > float(data.get("radius", 0)) > 0:
            return True
    except (TypeError, ValueError):
        pass
    description = (attendance.request_description or "").lower()
    return any(word in description for word in ("ngoài bán kính", "ngoai ban kinh", "outside radius", "gps", "vị trí"))


@login_required
def approval_hub(request):
    from attendance.models import Attendance
    from leave.models import LeaveRequest

    employee_ids = list(_visible_employees(request).values_list("pk", flat=True))
    leave_requests = LeaveRequest.objects.filter(
        employee_id_id__in=employee_ids, status="requested"
    ).select_related("employee_id", "leave_type_id").order_by("requested_date")
    attendance_requests = Attendance.objects.filter(
        employee_id_id__in=employee_ids,
        is_validate_request=True,
        is_validate_request_approved=False,
    ).select_related("employee_id").order_by("attendance_date")
    outside_requests = []
    for item in attendance_requests:
        if _outside_radius(item):
            data = _request_data(item)
            item.distance = data.get("distance") or data.get("distance_meters")
            outside_requests.append(item)
    return render(
        request,
        "checkin/approval_hub.html",
        {"leave_requests": leave_requests, "outside_requests": outside_requests},
    )


def _can_manage_settings(request):
    return request.user.is_superuser or request.user.has_perm("attendance.change_attendancegeneralsetting") or request.user.has_perm("base.change_company")


@login_required
def checkin_settings(request):
    if not _can_manage_settings(request):
        return HttpResponseForbidden("Bạn không có quyền thay đổi cài đặt chấm công.")
    company = _current_company(request)
    if company is None:
        messages.error(request, "Cần tạo công ty trước khi cài đặt chấm công.")
        return redirect("settings")
    policy, _ = CheckInPolicy.objects.get_or_create(company_id=company)
    location_instance = CheckInLocation.objects.filter(
        pk=request.GET.get("edit_location"), company_id=company
    ).first()
    wifi_instance = OfficeWifi.objects.filter(
        pk=request.GET.get("edit_wifi"), company_id=company
    ).first()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "policy":
            form = CheckInPolicyForm(request.POST, instance=policy)
            if form.is_valid():
                form.save()
                messages.success(request, "Đã lưu quy định chấm công.")
                return redirect("checkin-settings")
        elif action == "location":
            instance = CheckInLocation.objects.filter(
                pk=request.POST.get("object_id"), company_id=company
            ).first()
            form = CheckInLocationForm(request.POST, instance=instance)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.company_id = company
                obj.save()
                messages.success(request, "Đã lưu địa điểm chấm công.")
                return redirect("checkin-settings")
            location_instance = instance
        elif action == "wifi":
            instance = OfficeWifi.objects.filter(
                pk=request.POST.get("object_id"), company_id=company
            ).first()
            form = OfficeWifiForm(request.POST, instance=instance)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.company_id = company
                obj.save()
                messages.success(request, "Đã lưu Wifi văn phòng.")
                return redirect("checkin-settings")
            wifi_instance = instance

    context = {
        "company": company,
        "policy_form": CheckInPolicyForm(instance=policy),
        "location_form": CheckInLocationForm(instance=location_instance),
        "wifi_form": OfficeWifiForm(instance=wifi_instance),
        "location_instance": location_instance,
        "wifi_instance": wifi_instance,
        "locations": CheckInLocation.objects.filter(company_id=company),
        "wifi_networks": OfficeWifi.objects.filter(company_id=company),
    }
    if request.method == "POST":
        if request.POST.get("action") == "policy":
            context["policy_form"] = form
        elif request.POST.get("action") == "location":
            context["location_form"] = form
        elif request.POST.get("action") == "wifi":
            context["wifi_form"] = form
    return render(request, "checkin/settings.html", context)


@login_required
@require_POST
def delete_checkin_location(request, pk):
    if not _can_manage_settings(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    get_object_or_404(CheckInLocation, pk=pk, company_id=_current_company(request)).delete()
    messages.success(request, "Đã xóa địa điểm chấm công.")
    return redirect("checkin-settings")


@login_required
@require_POST
def delete_office_wifi(request, pk):
    if not _can_manage_settings(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    get_object_or_404(OfficeWifi, pk=pk, company_id=_current_company(request)).delete()
    messages.success(request, "Đã xóa Wifi văn phòng.")
    return redirect("checkin-settings")
