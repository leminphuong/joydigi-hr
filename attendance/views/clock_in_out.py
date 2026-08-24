"""
clock_in_out.py

This module is used register endpoints to the check-in check-out functionalities
"""

import ipaddress
import logging
import math

from django.shortcuts import render

from joydigi.http.response import JoydigiRedirect

logger = logging.getLogger(__name__)
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.messages.api import MessageFailure
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from attendance.methods.utils import (
    activity_datetime,
    employee_exists,
    format_time,
    overtime_calculation,
    shift_schedule_today,
    strtime_seconds,
)
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceGeneralSetting,
    AttendanceLateComeEarlyOut,
    GraceTime,
)
from attendance.views.views import attendance_validate
from base.context_processors import (
    enable_late_come_early_out_tracking,
    timerunner_enabled,
)
from base.models import (
    AttendanceAllowedIP,
    CheckInLocation,
    CheckInPolicy,
    Company,
    EmployeeShiftDay,
    OfficeWifi,
)
from base.checkin_tokens import resolve_kiosk_token
from joydigi.decorators import hx_request_required, login_required
from joydigi.joydigi_middlewares import _thread_locals


def _flash(level, request, text):
    """`django.contrib.messages` requires a real `HttpRequest` that
    went through `MessageMiddleware` — the lightweight `Request` shim
    used by device/API callers (`attendance.methods.utils.Request`)
    has no `_messages` storage at all, so calling `messages.error()`
    on it raises `MessageFailure`. That's purely web flash-message UX;
    the actual allow/reject decision has already been made by the
    time this runs, so for a non-web caller this is a no-op rather
    than a reason to fail the request."""
    try:
        level(request, text)
    except MessageFailure:
        pass


def _resolve_checkin_company(request):
    selected_company = request.session.get("selected_company")
    if selected_company and selected_company != "all":
        company = Company.objects.filter(pk=selected_company).first()
        if company:
            return company
    employee = getattr(request.user, "employee_get", None)
    try:
        return employee.employee_work_info.company_id
    except AttributeError:
        return None


def _distance_meters(lat1, lon1, lat2, lon2):
    """Khoảng cách đường chim bay theo công thức Haversine."""
    radius = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def validate_checkin_source(request, company):
    """Kiểm tra GPS/Wifi/QR cho một lượt chấm công.

    Phase 6.1: `request.trusted_device` (explicit, defaults `False`)
    replaces the old `request.__dict__.get("datetime")` check — every
    caller of `attendance.methods.utils.Request` sets `.datetime`
    (including the JWT-authenticated mobile API), so that check used
    to grant an unconditional bypass to any caller using this request
    shim, not just genuinely trusted fixed infrastructure. Only
    `attendance.scheduler`'s internal auto-punch-out job opts in.
    """
    if getattr(request, "trusted_device", False) or company is None:
        return {"allowed": True, "method": "Thiết bị tin cậy"}

    proof = request.GET.get("verification_proof") or request.POST.get(
        "verification_proof"
    )
    if proof:
        from attendance.methods.verification_proof import consume_verification_proof

        employee = getattr(request.user, "employee_get", None)
        method = consume_verification_proof(proof, getattr(employee, "id", None))
        if method is None:
            return {
                "allowed": False,
                "code": "VERIFICATION_REQUIRED",
                "message": "Xác thực đã hết hạn hoặc không hợp lệ. Vui lòng thử lại.",
            }
        return {"allowed": True, "method": f"Đã xác thực trước ({method})"}

    qr_token = request.GET.get("qr_token") or request.POST.get("qr_token")
    if qr_token:
        session = resolve_kiosk_token(qr_token)
        if session is None:
            return {
                "allowed": False,
                "code": "QR_EXPIRED",
                "message": "Mã QR đã hết hạn hoặc không hợp lệ.",
            }
        if session.get("company_id") != getattr(company, "id", company):
            return {
                "allowed": False,
                "code": "QR_WRONG_COMPANY",
                "message": "Mã QR không thuộc công ty của bạn.",
            }
        location = CheckInLocation.objects.filter(
            pk=session.get("location_id"), company_id=company, is_active=True
        ).first()
        if location is None:
            return {
                "allowed": False,
                "code": "QR_WRONG_LOCATION",
                "message": "Địa điểm chấm công của mã QR không còn hoạt động.",
            }
        return {
            "allowed": True,
            "method": "Mã QR văn phòng",
            "location": location.name,
        }

    numeric_code = (
        request.GET.get("numeric_code") or request.POST.get("numeric_code")
    )
    if numeric_code:
        from base.checkin_tokens import resolve_kiosk_code
        from base.throttling import check_and_record_numeric_code_attempt

        throttled = check_and_record_numeric_code_attempt(request.user)
        if throttled:
            return {
                "allowed": False,
                "code": "QR_CODE_THROTTLED",
                "message": "Bạn đã nhập sai quá nhiều lần. Vui lòng thử lại sau.",
            }
        session = resolve_kiosk_code(numeric_code)
        if session is None:
            return {
                "allowed": False,
                "code": "QR_CODE_INVALID",
                "message": "Mã số không đúng hoặc đã hết hạn.",
            }
        if session.get("company_id") != getattr(company, "id", company):
            return {
                "allowed": False,
                "code": "QR_WRONG_COMPANY",
                "message": "Mã số không thuộc công ty của bạn.",
            }
        location = CheckInLocation.objects.filter(
            pk=session.get("location_id"), company_id=company, is_active=True
        ).first()
        if location is None:
            return {
                "allowed": False,
                "code": "QR_WRONG_LOCATION",
                "message": "Địa điểm chấm công của mã số không còn hoạt động.",
            }
        return {
            "allowed": True,
            "method": "Mã số văn phòng",
            "location": location.name,
        }

    locations = list(CheckInLocation.objects.filter(company_id=company, is_active=True))
    wifi_networks = OfficeWifi.objects.filter(company_id=company, is_active=True)
    wifi_ssid = (request.GET.get("wifi_ssid") or request.POST.get("wifi_ssid") or "").strip()
    wifi_bssid = (request.GET.get("wifi_bssid") or request.POST.get("wifi_bssid") or "").strip()
    if wifi_ssid:
        if wifi_networks.filter(
            Q(ssid__iexact=wifi_ssid) & (Q(bssid="") | Q(bssid__iexact=wifi_bssid))
        ).exists():
            return {"allowed": True, "method": "Wifi văn phòng", "wifi": wifi_ssid}
        return {
            "allowed": False,
            "code": "WIFI_NOT_ALLOWED",
            "message": "Mạng Wifi hiện tại không được dùng để chấm công.",
        }

    latitude = request.GET.get("latitude") or request.POST.get("latitude")
    longitude = request.GET.get("longitude") or request.POST.get("longitude")
    if latitude not in (None, "") and longitude not in (None, ""):
        try:
            latitude, longitude = float(latitude), float(longitude)
        except (TypeError, ValueError):
            return {
                "allowed": False,
                "code": "LOCATION_INVALID",
                "message": "Tọa độ chấm công không hợp lệ.",
            }
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return {
                "allowed": False,
                "code": "LOCATION_INVALID",
                "message": "Tọa độ chấm công không hợp lệ.",
            }
        if not locations:
            return {
                "allowed": True,
                "method": "Vị trí GPS",
                "latitude": latitude,
                "longitude": longitude,
            }
        nearest = min(
            locations,
            key=lambda item: _distance_meters(
                latitude,
                longitude,
                float(item.latitude),
                float(item.longitude),
            ),
        )
        distance = round(
            _distance_meters(
                latitude,
                longitude,
                float(nearest.latitude),
                float(nearest.longitude),
            )
        )
        result = {
            "allowed": distance <= nearest.radius_meters,
            "outside_radius": distance > nearest.radius_meters,
            "method": "Vị trí GPS",
            "latitude": latitude,
            "longitude": longitude,
            "distance": distance,
            "radius": nearest.radius_meters,
            "location": nearest.name,
        }
        if result["outside_radius"]:
            policy = CheckInPolicy.objects.filter(company_id=company).first()
            result["allowed"] = bool(
                policy is None or policy.allow_outside_radius_request
            )
            result["code"] = None if result["allowed"] else "LOCATION_OUTSIDE"
            result["message"] = (
                f"Bạn đang cách {nearest.name} khoảng {distance} m, "
                f"ngoài bán kính {nearest.radius_meters} m."
            )
        return result

    if locations:
        return {
            "allowed": False,
            "code": "VERIFICATION_REQUIRED",
            "message": "Vui lòng bật quyền vị trí để chấm công.",
        }
    # Chưa cấu hình địa điểm/Wifi thì giữ tương thích với hệ thống cũ.
    return {"allowed": True, "method": "Trình duyệt"}


def _mark_outside_radius_request(attendance, source):
    if not attendance or not source.get("outside_radius"):
        return
    attendance.is_validate_request = True
    attendance.is_validate_request_approved = False
    attendance.request_type = "update_request"
    attendance.request_description = source.get("message")
    attendance.requested_data = {
        "outside_radius": True,
        "latitude": source.get("latitude"),
        "longitude": source.get("longitude"),
        "distance": source.get("distance"),
        "radius": source.get("radius"),
        "location": source.get("location"),
        "method": source.get("method"),
    }
    attendance.save(
        update_fields=[
            "is_validate_request",
            "is_validate_request_approved",
            "request_type",
            "request_description",
            "requested_data",
        ]
    )


def late_come_create(attendance):
    """
    used to create late come report
    args:
        attendance : attendance object
    """

    if AttendanceLateComeEarlyOut.objects.filter(
        type="late_come", attendance_id=attendance
    ).exists():
        late_come_obj = AttendanceLateComeEarlyOut.objects.filter(
            type="late_come", attendance_id=attendance
        ).first()
    else:
        late_come_obj = AttendanceLateComeEarlyOut()

    late_come_obj.type = "late_come"
    late_come_obj.attendance_id = attendance
    late_come_obj.employee_id = attendance.employee_id
    late_come_obj.save()
    return late_come_obj


def late_come(attendance, start_time, end_time, shift):
    """
    this method is used to mark the late check-in  attendance after the shift starts
    args:
        attendance : attendance obj
        start_time : attendance day shift start time
        end_time : attendance day shift end time

    """
    if not shift:
        return
    if not enable_late_come_early_out_tracking(None).get("tracking"):
        return
    request = getattr(_thread_locals, "request", None)
    now_sec = strtime_seconds(attendance.attendance_clock_in.strftime("%H:%M"))
    mid_day_sec = strtime_seconds("12:00")

    # Checking gracetime allowance before creating late come
    if shift and shift.grace_time_id:
        # checking grace time in shift, it has the higher priority
        if (
            shift.grace_time_id.is_active == True
            and shift.grace_time_id.allowed_clock_in == True
        ):
            # Setting allowance for the check in time
            now_sec -= shift.grace_time_id.allowed_time_in_secs
    else:
        work_info = getattr(attendance.employee_id, "employee_work_info", None)
        company = getattr(work_info, "company_id", None)
        policy = CheckInPolicy.objects.filter(company_id=company).first()
        if policy:
            now_sec -= policy.late_threshold_minutes * 60
        elif GraceTime.objects.filter(is_default=True, is_active=True).exists():
            grace_time = GraceTime.objects.filter(
                is_default=True,
                is_active=True,
            ).first()
            if grace_time.allowed_clock_in:
                now_sec -= grace_time.allowed_time_in_secs
    if start_time > end_time and start_time != end_time:
        # night shift
        if now_sec < mid_day_sec:
            # Here  attendance or attendance activity for new day night shift
            late_come_create(attendance)
        elif now_sec > start_time:
            # Here  attendance or attendance activity for previous day night shift
            late_come_create(attendance)
    elif start_time < now_sec:
        late_come_create(attendance)
    return True


def clock_in_attendance_and_activity(
    employee,
    date_today,
    attendance_date,
    day,
    now,
    shift,
    minimum_hour,
    start_time,
    end_time,
    in_datetime,
):
    """
    This method is used to create attendance activity or attendance when an employee clocks-in
    args:
        employee        : employee instance
        date_today      : date
        attendance_date : the date that attendance for
        day             : shift day
        now             : current time
        shift           : shift object
        minimum_hour    : minimum hour in shift schedule
        start_time      : start time in shift schedule
        end_time        : end time in shift schedule
    """

    # attendance activity create
    activity = AttendanceActivity.objects.filter(
        employee_id=employee,
        attendance_date=attendance_date,
        clock_in_date=date_today,
        shift_day=day,
        clock_out=None,
    ).first()

    if activity and not activity.clock_out:
        activity.clock_out = in_datetime
        activity.clock_out_date = date_today
        activity.save()

    new_activity = AttendanceActivity.objects.create(
        employee_id=employee,
        attendance_date=attendance_date,
        clock_in_date=date_today,
        shift_day=day,
        clock_in=in_datetime,
        in_datetime=in_datetime,
    )
    # create attendance if not exist
    attendance = Attendance.objects.filter(
        employee_id=employee, attendance_date=attendance_date
    )
    if not attendance.exists():
        attendance = Attendance()
        attendance.employee_id = employee
        attendance.shift_id = shift
        attendance.work_type_id = attendance.employee_id.employee_work_info.work_type_id
        attendance.attendance_date = attendance_date
        attendance.attendance_day = day
        attendance.attendance_clock_in = now
        attendance.attendance_clock_in_date = date_today
        attendance.minimum_hour = minimum_hour
        attendance.save()
        # check here late come or not

        attendance = Attendance.find(attendance.id)
        late_come(
            attendance=attendance, start_time=start_time, end_time=end_time, shift=shift
        )
    else:
        attendance = attendance[0]
        attendance.attendance_clock_out = None
        attendance.attendance_clock_out_date = None
        attendance.save()
        # delete if the attendance marked the early out
        early_out_instance = attendance.late_come_early_out.filter(type="early_out")
        if early_out_instance.exists():
            early_out_instance[0].delete()
    return attendance


def perform_clock_in(request):
    """
    Apply the existing clock-in mutation without rendering a response.

    Returns ``(attendance, allowed)`` so browser, API and Face ID entry points
    can share IP/GPS/Wifi, shifts, late-coming and attendance logic.
    """
    attendance, allowed, _reason = perform_clock_in(request)
    if not allowed:
        # `perform_clock_in` already queued the specific reason via
        # `messages.error`/`messages.warning` before returning.
        return JoydigiRedirect(request)
    request.user.employee_get.refresh_from_db()
    return render(request, "attendance/components/in_out_component.html", {"run": 1})


def perform_clock_in(request):
    """
    Pure clock-in mutation, mirroring `perform_clock_out` (Phase 6.1).
    Never renders a template; safe to call with the lightweight
    `Request` shim used by device/API callers. Returns
    `(attendance, allowed, reason)` — `reason` is `None` on success or
    `{"code", "message"}` on rejection.
    """
    # check wether check in/check out feature is enabled
    company = _resolve_checkin_company(request)
    attendance_general_settings = AttendanceGeneralSetting.objects.filter(
        company_id=company
    ).first() or AttendanceGeneralSetting.objects.filter(company_id=None).first()
    # request.__dict__.get("datetime")' used to check if the request is from a biometric device
    if (
        attendance_general_settings
        and attendance_general_settings.enable_check_in
        or request.__dict__.get("datetime")
    ):
        allowed_attendance_ips = AttendanceAllowedIP.objects.filter(
            company_id=company
        ).first()

        if (
            not getattr(request, "trusted_device", False)
            and allowed_attendance_ips
            and allowed_attendance_ips.is_enabled
        ):
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            ip = request.META.get("REMOTE_ADDR")
            if x_forwarded_for:
                ip = x_forwarded_for.split(",")[0]

            allowed_ips = (allowed_attendance_ips.additional_data or {}).get(
                "allowed_ips", []
            )
            ip_allowed = False
            for allowed_ip in allowed_ips:
                try:
                    if ipaddress.ip_address(ip) in ipaddress.ip_network(
                        allowed_ip, strict=False
                    ):
                        ip_allowed = True
                        break
                except ValueError:
                    continue

            if not ip_allowed:
                messages.error(
                    request,
                    _("Check-In Restricted: Your current network is not authorized "),
                )
                return None, False

        checkin_source = _validate_checkin_source(request, company)
        if not checkin_source["allowed"]:
            messages.error(request, checkin_source["message"])
            return None, False

        employee, work_info = employee_exists(request)
        datetime_now = timezone.localtime()
        if request.__dict__.get("datetime"):
            datetime_now = request.datetime
        if employee and work_info is not None:
            shift = work_info.shift_id
            date_today = date.today()
            if request.__dict__.get("date"):
                date_today = request.date
            attendance_date = date_today
            day = date_today.strftime("%A").lower()
            day = EmployeeShiftDay.objects.get(day=day)
            now = datetime.now().strftime("%H:%M")
            if request.__dict__.get("time"):
                now = request.time.strftime("%H:%M")
            now_sec = strtime_seconds(now)
            mid_day_sec = strtime_seconds("12:00")
            minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
                day=day, shift=shift
            )
            if start_time_sec > end_time_sec:
                # night shift
                # ------------------
                # Night shift in Joydigi consider a 24 hours from noon to next day noon,
                # the shift day taken today if the attendance clocked in after 12 O clock.

                if mid_day_sec > now_sec:
                    # Here you need to create attendance for yesterday

                    date_yesterday = date_today - timedelta(days=1)
                    day_yesterday = date_yesterday.strftime("%A").lower()
                    day_yesterday = EmployeeShiftDay.objects.get(day=day_yesterday)
                    minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
                        day=day_yesterday, shift=shift
                    )
                    attendance_date = date_yesterday
                    day = day_yesterday
            attendance = clock_in_attendance_and_activity(
                employee=employee,
                date_today=date_today,
                attendance_date=attendance_date,
                day=day,
                now=now,
                shift=shift,
                minimum_hour=minimum_hour,
                start_time=start_time_sec,
                end_time=end_time_sec,
                in_datetime=datetime_now,
            )
            _mark_outside_radius_request(attendance, checkin_source)
            if checkin_source.get("outside_radius"):
                _flash(messages.warning, 
                    request,
                    checkin_source["message"] + " Bản ghi đã được chuyển sang chờ duyệt.",
                )
            # Refresh employee from DB so template re-evaluates is_clocked_in correctly
            employee.refresh_from_db()
            return attendance, True
        messages.error(
            request,
            _(
                "Check-In Unavailable: Your employee profile or work information is incomplete."
            ),
        )
        return None, False
    else:
        reason = {
            "code": "METHOD_NOT_ENABLED",
            "message": str(
                _(
                    "The attendance check-in/check-out feature has not been enabled for your company."
                )
            ),
        )
        return None, False


@login_required
@hx_request_required
def clock_in(request):
    """Render wrapper around the reusable clock-in mutation."""
    attendance, allowed = perform_clock_in(request)
    if not allowed or attendance is None:
        return JoydigiRedirect(request)
    return render(request, "attendance/components/in_out_component.html", {"run": 1})


def clock_out_attendance_and_activity(employee, date_today, now, out_datetime=None):
    """
    Clock out the attendance and activity
    args:
        employee    : employee instance
        date_today  : today date
        now         : now
    """

    attendance_activities = AttendanceActivity.objects.filter(
        employee_id=employee,
    ).order_by("attendance_date", "id")
    attendance_activity = None  # Initialize attendance_activity

    if attendance_activities.filter(clock_out__isnull=True).exists():
        attendance_activity = attendance_activities.filter(
            clock_out__isnull=True
        ).last()
        attendance_activity.clock_out = out_datetime
        attendance_activity.clock_out_date = date_today
        attendance_activity.out_datetime = out_datetime
        attendance_activity.save()

        attendance_activities = attendance_activities.filter(
            attendance_date=attendance_activity.attendance_date
        )
        # Here calculate the total durations between the attendance activities

        duration = 0
        for activity in attendance_activities:
            in_datetime, out_datetime = activity_datetime(activity)
            difference = out_datetime - in_datetime
            days_second = difference.days * 24 * 3600
            seconds = difference.seconds
            total_seconds = days_second + seconds
            duration = duration + total_seconds
        duration = format_time(duration)
        # update clock out of attendance
        attendance = Attendance.objects.filter(employee_id=employee).order_by(
            "-attendance_date", "-id"
        )[0]
        attendance.attendance_clock_out = now + ":00"
        attendance.attendance_clock_out_date = date_today
        attendance.attendance_worked_hour = duration
        # Overtime calculation
        attendance.attendance_overtime = overtime_calculation(attendance)

        # Validate the attendance as per the condition
        attendance.attendance_validated = attendance_validate(attendance)
        attendance.save()

        return attendance

    logger.error("No attendance clock in activity found that needs clocking out.")
    return


def early_out_create(attendance):
    """
    Used to create early out report
    args:
        attendance : attendance obj
    """
    if AttendanceLateComeEarlyOut.objects.filter(
        type="early_out", attendance_id=attendance
    ).exists():
        late_come_obj = AttendanceLateComeEarlyOut.objects.filter(
            type="early_out", attendance_id=attendance
        ).first()
    else:
        late_come_obj = AttendanceLateComeEarlyOut()
    late_come_obj.type = "early_out"
    late_come_obj.attendance_id = attendance
    late_come_obj.employee_id = attendance.employee_id
    late_come_obj.save()
    return late_come_obj


def early_out(attendance, start_time, end_time, shift):
    """
    This method is used to mark the early check-out attendance before the shift ends
    args:
        attendance : attendance obj
        start_time : attendance day shift start time
        start_end : attendance day shift end time
    """
    if not shift:
        return
    if not enable_late_come_early_out_tracking(None).get("tracking"):
        return

    clock_out_time = attendance.attendance_clock_out
    if isinstance(clock_out_time, str):
        clock_out_time = datetime.strptime(clock_out_time, "%H:%M:%S")

    now_sec = strtime_seconds(clock_out_time.strftime("%H:%M"))
    mid_day_sec = strtime_seconds("12:00")
    # Checking gracetime allowance before creating early out
    if shift and shift.grace_time_id:
        if (
            shift.grace_time_id.is_active == True
            and shift.grace_time_id.allowed_clock_out == True
        ):
            now_sec += shift.grace_time_id.allowed_time_in_secs
    elif GraceTime.objects.filter(is_default=True, is_active=True).exists():
        grace_time = GraceTime.objects.filter(
            is_default=True,
            is_active=True,
        ).first()
        # Setting allowance for the check out time if grace allocate for clock out event
        if grace_time.allowed_clock_out:
            now_sec += grace_time.allowed_time_in_secs
    else:
        pass
    if start_time > end_time:
        # Early out condition for night shift
        if now_sec < mid_day_sec:
            if now_sec < end_time:
                # Early out condition for general shift
                early_out_create(attendance)
        else:
            early_out_create(attendance)
        return
    if end_time > now_sec:
        early_out_create(attendance)
    return


@login_required
@hx_request_required
def clock_out(request):
    """
    This method is used to set the out date and time for attendance and attendance activity
    """
    attendance, allowed, _reason = perform_clock_out(request)
    if not allowed:
        # `perform_clock_out` already queued the specific reason via
        # `messages.error` before returning `(None, False)` — this is a
        # plain redirect, not a duplicate message.
        return JoydigiRedirect(request)
    # Refresh employee from DB so template re-evaluates is_clocked_in correctly
    request.user.employee_get.refresh_from_db()
    return render(request, "attendance/components/in_out_component.html", {"run": 1})


def perform_clock_out(request):
    """
    Pure clock-out mutation, split out of `clock_out()` (Phase 5.2).

    Looks up the employee's shift/company context, records the
    clock-out time + activity, and applies early-out logic. Returns
    `(attendance, allowed)`: `allowed` is `False` only when the
    company hasn't enabled the attendance check-in/check-out feature
    at all (a distinct "not configured" state, not "already clocked
    out" or "no open attendance" — those are gated by the caller via
    `Employee.check_online()` before this runs).

    Callers must never depend on this function rendering a template or
    otherwise needing a real Django `HttpRequest` — that's exactly the
    bug this split fixes: `ClockOutAPIView` used to call `clock_out()`
    directly with the lightweight `Request` shim (see
    `attendance.methods.utils.Request`, built for device/API callers),
    and `clock_out()` unconditionally ended in `render(request, ...)`.
    That call happened *after* the real DB mutation below had already
    committed, so when it raised (`render()` needs a genuine
    `HttpRequest` for template context processors), the exception
    propagated to `ClockOutAPIView`'s `except Exception` and got turned
    into a false "already clocked-out" 400 — even though the checkout
    had already succeeded. This function never renders anything, so an
    API caller using it directly can't hit that failure mode.
    """
    # check wether check in/check out feature is enabled
    company = _resolve_checkin_company(request)
    attendance_general_settings = AttendanceGeneralSetting.objects.filter(
        company_id=company
    ).first() or AttendanceGeneralSetting.objects.filter(company_id=None).first()
    if (
        attendance_general_settings
        and attendance_general_settings.enable_check_in
        or request.__dict__.get("datetime")
    ):
        allowed_attendance_ips = AttendanceAllowedIP.objects.filter(
            company_id=company
        ).first()

        if (
            not getattr(request, "trusted_device", False)
            and allowed_attendance_ips
            and allowed_attendance_ips.is_enabled
        ):
            x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
            ip = request.META.get("REMOTE_ADDR")
            if x_forwarded_for:
                ip = x_forwarded_for.split(",")[0]

            allowed_ips = (allowed_attendance_ips.additional_data or {}).get(
                "allowed_ips", []
            )
            ip_allowed = False
            for allowed_ip in allowed_ips:
                try:
                    if ipaddress.ip_address(ip) in ipaddress.ip_network(
                        allowed_ip, strict=False
                    ):
                        ip_allowed = True
                        break
                except ValueError:
                    continue

            if not ip_allowed:
                reason = {
                    "code": "WIFI_NOT_ALLOWED",
                    "message": str(
                        _("Check-Out Restricted: Your current network is not authorized")
                    ),
                }
                _flash(messages.error, request, reason["message"])
                return None, False, reason

        checkin_source = validate_checkin_source(request, company)
        if not checkin_source["allowed"]:
            # Phase 6.1: mobile/API callers no longer get an automatic
            # pass here — see `validate_checkin_source`'s
            # `trusted_device` doc.
            _flash(messages.error, request, checkin_source["message"])
            reason = {
                "code": checkin_source.get("code") or "VERIFICATION_REQUIRED",
                "message": checkin_source["message"],
            }
            return None, False, reason

        datetime_now = timezone.localtime()
        if request.__dict__.get("datetime"):
            datetime_now = request.datetime
        employee, work_info = employee_exists(request)
        shift = work_info.shift_id
        date_today = date.today()
        if request.__dict__.get("date"):
            date_today = request.date
        day = date_today.strftime("%A").lower()
        day = EmployeeShiftDay.objects.get(day=day)
        attendance = (
            Attendance.objects.filter(employee_id=employee)
            .order_by("id", "attendance_date")
            .last()
        )
        if attendance is not None:
            if not attendance.attendance_day:
                day_name = attendance.attendance_date.strftime("%A").lower()
                attendance.attendance_day = EmployeeShiftDay.objects.get(day=day_name)
                attendance.save(update_fields=["attendance_day"])
            day = attendance.attendance_day
        now = datetime.now().strftime("%H:%M")
        if request.__dict__.get("time"):
            now = request.time.strftime("%H:%M")
        minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
            day=day, shift=shift
        )
        attendance = clock_out_attendance_and_activity(
            employee=employee, date_today=date_today, now=now, out_datetime=datetime_now
        )
        _mark_outside_radius_request(attendance, checkin_source)
        if attendance:
            early_out_instance = attendance.late_come_early_out.filter(type="early_out")
            is_night_shift = attendance.is_night_shift()
            next_date = attendance.attendance_date + timedelta(days=1)
            if not early_out_instance.exists():
                if is_night_shift:
                    now_sec = strtime_seconds(now)
                    mid_sec = strtime_seconds("12:00")

                    if (attendance.attendance_date == date_today) or (
                        # check is next day mid
                        mid_sec >= now_sec
                        and date_today == next_date
                    ):
                        early_out(
                            attendance=attendance,
                            start_time=start_time_sec,
                            end_time=end_time_sec,
                            shift=shift,
                        )
                elif attendance.attendance_date == date_today:
                    early_out(
                        attendance=attendance,
                        start_time=start_time_sec,
                        end_time=end_time_sec,
                        shift=shift,
                    )

        return attendance, True, None

    reason = {
        "code": "METHOD_NOT_ENABLED",
        "message": str(
            _(
                "The attendance check-in/check-out feature has not been enabled for your company."
            )
        ),
    }
    _flash(messages.error, request, reason["message"])
    return None, False, reason
