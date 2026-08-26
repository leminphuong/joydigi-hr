"""Các màn hình chấm công tinh gọn dùng cho menu chính."""

import json
import io
from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from base.forms import CheckInLocationForm, CheckInPolicyForm, OfficeWifiForm
from base.checkin_tokens import create_kiosk_session, resolve_kiosk_token
from base.models import CheckInLocation, CheckInPolicy, Company, OfficeWifi
from base.roles import (
    checkin_admin_required,
    checkin_leader_required,
    is_checkin_admin,
)


def _current_company(request):
    employee = getattr(request.user, "employee_get", None)
    company = employee.get_company() if employee else None
    return company or Company.objects.filter(hq=True).first() or Company.objects.first()


def _sync_annual_leave(company, days):
    """Đồng bộ tham số phép năm với loại phép và quỹ phép của nhân viên."""
    from employee.models import Employee
    from leave.models import AvailableLeave, LeaveType

    leave_type = LeaveType.objects.filter(
        company_id=company, name="Nghỉ phép năm"
    ).first()
    previous_total = float(leave_type.total_days or 0) if leave_type else 0
    if leave_type is None:
        leave_type = LeaveType.objects.create(
            company_id=company,
            name="Nghỉ phép năm",
            payment="paid",
            payment_type="paid",
            limit_leave=True,
            total_days=days,
            require_approval="yes",
            exclude_company_leave="yes",
            exclude_holiday="yes",
        )
    elif previous_total != float(days):
        leave_type.total_days = days
        leave_type.save(update_fields=["total_days"])

    difference = float(days) - previous_total
    employees = Employee.objects.filter(
        is_active=True, employee_work_info__company_id=company
    )
    for employee in employees:
        balance, created = AvailableLeave.objects.get_or_create(
            employee_id=employee,
            leave_type_id=leave_type,
            defaults={"available_days": days},
        )
        if not created and difference:
            balance.available_days = max(float(balance.available_days) + difference, 0)
            balance.save(update_fields=["available_days", "total_leave_days"])


def _kiosk_location(request):
    """Resolves the `location_id` GET param to a `CheckInLocation` the
    requester's company actually owns. Returns `None` if missing or
    doesn't belong to this company — never falls back to "any
    location" (Phase 6.1: the kiosk display must be explicitly bound
    to one real, active location before it can mint a session)."""
    location_id = request.GET.get("location_id")
    if not location_id:
        return None
    company = _current_company(request)
    return CheckInLocation.objects.filter(
        pk=location_id, company_id=company, is_active=True
    ).first()


@login_required
@checkin_leader_required
def kiosk(request):
    """Màn hình QR động tại văn phòng; không chứa dữ liệu nhân viên.

    Phase 6.1: this used to be a fully public page. It's now gated to
    an authenticated checkin-leader/admin, and must be bound to one
    specific `CheckInLocation` the caller's company actually owns —
    that binding is what the minted QR/code sessions carry, so a
    displayed QR always answers "which real office location is this
    kiosk?" instead of nothing at all.
    """
    location = _kiosk_location(request)
    company = _current_company(request)
    response = render(
        request,
        "checkin/kiosk.html",
        {
            "location": location,
            "locations": CheckInLocation.objects.filter(
                company_id=company, is_active=True
            ),
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@login_required
@checkin_leader_required
def kiosk_qr(request):
    import qrcode

    token = request.GET.get("token", "")
    if not resolve_kiosk_token(token):
        return HttpResponseForbidden("Mã QR đã hết hạn hoặc không hợp lệ.")
    destination = request.build_absolute_uri(
        f"{reverse('qr-checkin')}?qr_token={token}"
    )
    image = qrcode.make(destination)
    output = io.BytesIO()
    image.save(output, format="PNG")
    response = HttpResponse(output.getvalue(), content_type="image/png")
    response["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@login_required
@checkin_leader_required
def kiosk_data(request):
    """Mints a fresh kiosk session for one specific, caller-owned
    `CheckInLocation` and returns its matching QR URL + 6-digit code —
    both opaque lookup keys into that same session (see
    `base.checkin_tokens.create_kiosk_session`)."""
    location = _kiosk_location(request)
    if location is None:
        return JsonResponse(
            {"error": "location_id không hợp lệ hoặc không thuộc công ty của bạn."},
            status=400,
        )
    session = create_kiosk_session(company_id=location.company_id_id, location_id=location.id)
    return JsonResponse(
        {
            "qr_url": f"{reverse('checkin-kiosk-qr')}?token={session['token']}",
            "code": session["code"],
            "location_id": location.id,
            "location_name": location.name,
            "ttl": session["ttl"],
        }
    )


@login_required
def qr_checkin(request):
    token = request.GET.get("qr_token", "")
    if not valid_kiosk_token(token):
        return render(request, "checkin/qr_checkin.html", {"token_expired": True})
    return render(request, "checkin/qr_checkin.html", {"qr_token": token})


def _visible_employees(request):
    from employee.models import Employee

    # Lấy danh sách gốc rồi tự giới hạn theo công ty đang chọn. Cách này tránh
    # việc bộ lọc công ty của manager được áp dụng hai lần và làm danh sách
    # Duyệt đơn rỗng trong khi trang quản trị đầy đủ vẫn có dữ liệu.
    employees = Employee.objects.entire().filter(is_active=True)
    selected_company = request.session.get("selected_company")
    if selected_company and selected_company != "all":
        employees = employees.filter(
            employee_work_info__company_id_id=selected_company
        )
    elif selected_company == "all" and not is_checkin_admin(request.user):
        allowed_company_ids = getattr(request, "all_my_company_ids", None)
        if allowed_company_ids is not None:
            employees = employees.filter(
                employee_work_info__company_id_id__in=allowed_company_ids
            )

    employees = employees.select_related(
        "employee_work_info__department_id", "employee_work_info__company_id"
    )
    employee = getattr(request.user, "employee_get", None)
    if is_checkin_admin(request.user):
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
@checkin_leader_required
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
@checkin_leader_required
def approval_hub(request):
    from attendance.models import Attendance
    from base.models import ShiftRequest
    from leave.models import LeaveRequest

    employee_ids = list(_visible_employees(request).values_list("pk", flat=True))
    # employee_ids đã chứa đúng phạm vi Admin/Trưởng nhóm, vì vậy dùng
    # queryset gốc để kết quả tại đây khớp với trang duyệt đầy đủ.
    leave_queryset = LeaveRequest.objects.entire().filter(
        employee_id_id__in=employee_ids
    ).select_related("employee_id", "leave_type_id").prefetch_related(
        "leaverequestconditionapproval_set__manager_id"
    ).order_by("-requested_date", "-id")[:100]
    actor = getattr(request.user, "employee_get", None)
    admin_view = is_checkin_admin(request.user)
    leave_requests = []
    for item in leave_queryset:
        approvals = list(item.leaverequestconditionapproval_set.all())
        item.approval_steps = approvals
        pending_step = next(
            (
                step
                for step in approvals
                if not step.is_approved and not step.is_rejected
            ),
            None,
        )
        item.current_approval = pending_step
        item.is_own_request = item.employee_id_id == getattr(actor, "id", None)
        item.can_act = False
        if item.status != "requested":
            item.current_approval = None
        elif approvals:
            item.can_act = bool(
                pending_step
                and pending_step.manager_id_id == getattr(actor, "id", None)
            )
        else:
            item.can_act = bool(
                not item.is_own_request
                and (
                    admin_view
                    or item.employee_id.get_reporting_manager() == actor
                )
            )
        leave_requests.append(item)
    attendance_requests = Attendance.objects.entire().filter(
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
    # Phase UI-4B.3: same employee_ids scope as the sections above —
    # employee_ids already came from _visible_employees(request), which
    # already filters is_active=True. ShiftRequest also inherits its own
    # is_active from JoydigiModel (defaults True, never toggled by the
    # approve/cancel views) — filtered explicitly below for correctness,
    # not because a row is ever observed with it False today.
    shift_requests = (
        ShiftRequest.objects.entire()
        .filter(
            employee_id_id__in=employee_ids,
            approved=False,
            canceled=False,
            is_active=True,
        )
        .select_related(
            "employee_id",
            "employee_id__employee_work_info__department_id",
            "employee_id__employee_work_info__job_position_id",
            "shift_id",
            "previous_shift_id",
        )
        .order_by("-requested_date", "-id")[:100]
    )
    # Phase UI-4C.1: same explicit employee_ids scope as every section
    # above — never implicit thread-local/manager scoping alone.
    from attendance.models import AttendanceLateEarlyRequest

    late_early_requests = (
        AttendanceLateEarlyRequest.objects.entire()
        .filter(
            employee_id_id__in=employee_ids,
            approved=False,
            canceled=False,
            is_active=True,
        )
        .select_related(
            "employee_id",
            "employee_id__employee_work_info__department_id",
            "employee_id__employee_work_info__job_position_id",
        )
        .order_by("-request_date", "-id")[:100]
    )
    response = render(
        request,
        "checkin/approval_hub.html",
        {
            "leave_requests": leave_requests,
            "outside_requests": outside_requests,
            "shift_requests": shift_requests,
            "late_early_requests": late_early_requests,
        },
    )
    response["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@login_required
@checkin_leader_required
@require_POST
def late_early_request_approve(request, id):
    """Phase UI-4C.1 admin action — same visible-employee scope as the
    /duyet-don/ query above. Approval has no automatic effect on
    Attendance/Timesheet/AttendanceLateComeEarlyOut (see phase report)."""
    from attendance.models import AttendanceLateEarlyRequest

    instance = get_object_or_404(AttendanceLateEarlyRequest.objects.entire(), pk=id)
    visible_ids = set(_visible_employees(request).values_list("pk", flat=True))
    if instance.employee_id_id not in visible_ids:
        messages.error(request, "Bạn không có quyền duyệt đơn này.")
        return redirect("approval-hub")
    if not instance.approved:
        instance.approved = True
        instance.canceled = False
        instance.save()
        messages.success(request, "Đã duyệt đơn xin đi muộn/về sớm.")
    return redirect("approval-hub")


@login_required
@checkin_leader_required
@require_POST
def late_early_request_reject(request, id):
    """Phase UI-4C.1 admin action — labeled "Từ chối" in the UI; sets
    canceled=True, the project's established rejected/cancelled state
    (same convention as ShiftRequest.canceled)."""
    from attendance.models import AttendanceLateEarlyRequest

    instance = get_object_or_404(AttendanceLateEarlyRequest.objects.entire(), pk=id)
    visible_ids = set(_visible_employees(request).values_list("pk", flat=True))
    if instance.employee_id_id not in visible_ids:
        messages.error(request, "Bạn không có quyền từ chối đơn này.")
        return redirect("approval-hub")
    if not instance.canceled:
        instance.canceled = True
        instance.approved = False
        instance.save()
        messages.success(request, "Đã từ chối đơn xin đi muộn/về sớm.")
    return redirect("approval-hub")


def _can_manage_settings(request):
    return is_checkin_admin(request.user)


def _settings_instance(model, raw_id, company):
    """Trả về bản ghi cần sửa; mã rỗng có nghĩa là tạo mới."""
    if not raw_id:
        return None
    try:
        object_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    if object_id <= 0:
        return None
    return model.objects.filter(pk=object_id, company_id=company).first()


def _checkin_settings_context(
    company,
    *,
    policy_form=None,
    location_form=None,
    wifi_form=None,
    location_instance=None,
    wifi_instance=None,
    success_message="",
):
    policy, _ = CheckInPolicy.objects.get_or_create(company_id=company)
    return {
        "company": company,
        "policy_form": policy_form or CheckInPolicyForm(instance=policy),
        "location_form": location_form or CheckInLocationForm(instance=location_instance),
        "wifi_form": wifi_form or OfficeWifiForm(instance=wifi_instance),
        "location_instance": location_instance,
        "wifi_instance": wifi_instance,
        "locations": CheckInLocation.objects.filter(company_id=company),
        "wifi_networks": OfficeWifi.objects.filter(company_id=company),
        "success_message": success_message,
    }


def _render_checkin_settings(request, context):
    """Trả trang cài đặt; HTMX tự lấy riêng vùng nội dung cần cập nhật."""
    return render(request, "checkin/settings.html", context)


@login_required
@checkin_admin_required
def checkin_settings(request):
    if not _can_manage_settings(request):
        return HttpResponseForbidden("Bạn không có quyền thay đổi cài đặt chấm công.")
    company = _current_company(request)
    if company is None:
        messages.error(request, "Cần tạo công ty trước khi cài đặt chấm công.")
        return redirect("settings")
    policy, _ = CheckInPolicy.objects.get_or_create(company_id=company)
    location_instance = _settings_instance(
        CheckInLocation, request.GET.get("edit_location"), company
    )
    wifi_instance = _settings_instance(
        OfficeWifi, request.GET.get("edit_wifi"), company
    )

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "policy":
            form = CheckInPolicyForm(request.POST, instance=policy)
            if form.is_valid():
                saved_policy = form.save()
                _sync_annual_leave(company, saved_policy.annual_leave_days)
                if request.headers.get("HX-Target") == "checkinSettingsContent":
                    return _render_checkin_settings(
                        request,
                        _checkin_settings_context(
                            company, success_message="Đã lưu quy định chấm công."
                        ),
                    )
                messages.success(request, "Đã lưu quy định chấm công.")
                return redirect("checkin-settings")
        elif action == "location":
            instance = _settings_instance(
                CheckInLocation, request.POST.get("object_id"), company
            )
            form = CheckInLocationForm(request.POST, instance=instance)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.company_id = company
                obj.save()
                if request.headers.get("HX-Target") == "checkinSettingsContent":
                    return _render_checkin_settings(
                        request,
                        _checkin_settings_context(
                            company, success_message="Đã lưu địa điểm chấm công."
                        ),
                    )
                messages.success(request, "Đã lưu địa điểm chấm công.")
                return redirect("checkin-settings")
            location_instance = instance
        elif action == "wifi":
            instance = _settings_instance(
                OfficeWifi, request.POST.get("object_id"), company
            )
            form = OfficeWifiForm(request.POST, instance=instance)
            if form.is_valid():
                obj = form.save(commit=False)
                obj.company_id = company
                obj.save()
                if request.headers.get("HX-Target") == "checkinSettingsContent":
                    return _render_checkin_settings(
                        request,
                        _checkin_settings_context(
                            company, success_message="Đã lưu Wi-Fi văn phòng."
                        ),
                    )
                messages.success(request, "Đã lưu Wifi văn phòng.")
                return redirect("checkin-settings")
            wifi_instance = instance

    context = _checkin_settings_context(
        company,
        location_instance=location_instance,
        wifi_instance=wifi_instance,
    )
    if request.method == "POST":
        if request.POST.get("action") == "policy":
            context["policy_form"] = form
        elif request.POST.get("action") == "location":
            context["location_form"] = form
        elif request.POST.get("action") == "wifi":
            context["wifi_form"] = form
    return _render_checkin_settings(request, context)


@login_required
@checkin_admin_required
@require_POST
def delete_checkin_location(request, pk):
    if not _can_manage_settings(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    company = _current_company(request)
    get_object_or_404(CheckInLocation, pk=pk, company_id=company).delete()
    if request.headers.get("HX-Target") == "checkinSettingsContent":
        return _render_checkin_settings(
            request,
            _checkin_settings_context(
                company, success_message="Đã xóa địa điểm chấm công."
            ),
        )
    messages.success(request, "Đã xóa địa điểm chấm công.")
    return redirect("checkin-settings")


@login_required
@checkin_admin_required
@require_POST
def delete_office_wifi(request, pk):
    if not _can_manage_settings(request):
        return HttpResponseForbidden("Bạn không có quyền thực hiện thao tác này.")
    company = _current_company(request)
    get_object_or_404(OfficeWifi, pk=pk, company_id=company).delete()
    if request.headers.get("HX-Target") == "checkinSettingsContent":
        return _render_checkin_settings(
            request,
            _checkin_settings_context(company, success_message="Đã xóa Wi-Fi văn phòng."),
        )
    messages.success(request, "Đã xóa Wifi văn phòng.")
    return redirect("checkin-settings")
