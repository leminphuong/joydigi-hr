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
from django.db import transaction
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
from attendance.methods.worktime import activities_worked_seconds
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


@login_required
@hx_request_required
def clock_in(request):
    """Render wrapper around the reusable clock-in mutation."""
    attendance, allowed, _reason = perform_clock_in(request)
    if not allowed or attendance is None:
        return JoydigiRedirect(request)
    request.user.employee_get.refresh_from_db()
    return render(request, "attendance/components/in_out_component.html", {"run": 1})


def perform_clock_in(request):
    """Apply clock-in without rendering and return attendance, status and reason."""
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
                reason = {
                    "code": "WIFI_NOT_ALLOWED",
                    "message": str(
                        _("Check-In Restricted: Your current network is not authorized ")
                    ),
                }
                _flash(messages.error, request, reason["message"])
                return None, False, reason

        checkin_source = validate_checkin_source(request, company)
        if not checkin_source["allowed"]:
            _flash(messages.error, request, checkin_source["message"])
            reason = {
                "code": checkin_source.get("code") or "VERIFICATION_REQUIRED",
                "message": checkin_source["message"],
            }
            return None, False, reason

        employee, work_info = employee_exists(request)
        datetime_now = timezone.localtime()
        if request.__dict__.get("datetime"):
            datetime_now = request.datetime
        if employee and work_info is not None:
            shift = work_info.shift_id
            # Phase ATT-TIME-2: date and time are both derived from the
            # single `datetime_now` captured above rather than from
            # separate `date.today()` / `datetime.now()` calls. Those
            # two read the *process* clock (unaffected by
            # `django.utils.timezone`) and, being independent reads,
            # could also disagree across midnight — one call landing on
            # 23:59:59 and the next on 00:00:00 would file the record
            # under the wrong day. The per-request overrides below are
            # unchanged, so biometric-device callers still win.
            date_today = datetime_now.date()
            if request.__dict__.get("date"):
                date_today = request.date
            attendance_date = date_today
            day = date_today.strftime("%A").lower()
            day = EmployeeShiftDay.objects.get(day=day)
            now = datetime_now.strftime("%H:%M")
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
                _flash(
                    messages.warning,
                    request,
                    checkin_source["message"] + " Bản ghi đã được chuyển sang chờ duyệt.",
                )
            return attendance, True, None
        reason = {
            "code": "PROFILE_INCOMPLETE",
            "message": str(
                _(
                    "Check-In Unavailable: Your employee profile or work information is incomplete."
                )
            ),
        }
        _flash(messages.error, request, reason["message"])
        return None, False, reason

    reason = {
        "code": "METHOD_NOT_ENABLED",
        "message": str(
            _(
                "The attendance check-in/check-out feature has not been enabled for your company."
            ),
        ),
    }
    _flash(messages.error, request, reason["message"])
    return None, False, reason


CHECKOUT_MAX = 2
"""One check-out plus exactly one correction. See `Attendance.checkout_count`."""

MIN_SECONDS_BEFORE_CHECKOUT = 30 * 60
"""An employee must stay at least 30 minutes before the day can be closed."""


def _day_activities(employee, attendance_date):
    """
    The day's activities, oldest first.

    Explicit ordering: `AttendanceActivity.Meta.ordering` sorts by
    `-attendance_date` first, so relying on the default would make
    `.first()`/`.last()` mean the opposite of what this code needs.
    """
    return AttendanceActivity.objects.filter(
        employee_id=employee,
        attendance_date=attendance_date,
    ).order_by("clock_in", "id")


def _aware(value):
    return timezone.make_aware(value) if timezone.is_naive(value) else value


def first_check_in_datetime(attendance, activities):
    """
    Aware datetime of the day's first check-in, or None if unrecorded.

    Prefers the activity's `in_datetime` (a real DateTimeField, seconds
    included) over recombining `Attendance`'s separate date + time
    columns, so the 30-minute lock compares against the exact recorded
    instant.
    """
    first = activities.first()
    if first is not None and first.in_datetime:
        return _aware(first.in_datetime)
    if attendance.attendance_clock_in and attendance.attendance_clock_in_date:
        return _aware(
            datetime.combine(
                attendance.attendance_clock_in_date, attendance.attendance_clock_in
            )
        )
    return None


def last_check_out_datetime(attendance, activities):
    """Aware datetime of the day's current check-out mark, or None."""
    last = activities.filter(clock_out__isnull=False).last()
    if last is not None and last.out_datetime:
        return _aware(last.out_datetime)
    if attendance.attendance_clock_out and attendance.attendance_clock_out_date:
        return _aware(
            datetime.combine(
                attendance.attendance_clock_out_date, attendance.attendance_clock_out
            )
        )
    return None


def validate_clock_out(attendance, out_datetime, business_today, system_checkout=False):
    """
    Decide whether this check-out may proceed.

    Returns `(is_correction, reason)`. `reason` is None when allowed;
    otherwise it is the same `{"code", "message"}` shape every other
    rejection in this module returns, so callers need no new handling.

    `is_correction` distinguishes the two legitimate shapes of the same
    action: closing an open day (check-out #1), and moving the mark of an
    already-closed day to a later time (check-out #2, "final wins").
    """
    if attendance is None:
        return False, {
            "code": "NO_ACTIVE_ATTENDANCE",
            "message": str(_("Không tìm thấy bản ghi chấm công để chấm công ra.")),
        }

    activities = _day_activities(attendance.employee_id, attendance.attendance_date)
    count = attendance.checkout_count or 0

    if count >= CHECKOUT_MAX:
        return False, {
            "code": "CHECKOUT_LIMIT_REACHED",
            "message": str(_("Bạn đã chấm công ra tối đa 2 lần trong ngày hôm nay.")),
        }

    is_open = attendance.attendance_clock_out_date is None

    if is_open:
        check_in_at = first_check_in_datetime(attendance, activities)
        if check_in_at is None:
            return False, {
                "code": "NO_ACTIVE_ATTENDANCE",
                "message": str(_("Không tìm thấy giờ vào để tính giờ ra.")),
            }
        # The scheduled auto-punch-out closes rows for people who simply
        # forgot; it must not be defeated by a rule aimed at employees
        # checking out moments after arriving, or a late check-in would
        # leave the row open forever.
        if not system_checkout:
            elapsed = (out_datetime - check_in_at).total_seconds()
            if elapsed < MIN_SECONDS_BEFORE_CHECKOUT:
                return False, {
                    "code": "CHECKOUT_TOO_SOON",
                    "message": str(
                        _(
                            "Bạn cần ở lại ít nhất 30 phút sau khi chấm công vào "
                            "trước khi chấm công ra."
                        )
                    ),
                }
        return False, None

    # --- the row is already closed: only a same-day correction qualifies ---

    # Rows closed before this phase shipped (and rows closed by any path
    # that does not maintain the counter) sit at 0. They are finished, not
    # eligible for a correction: treating 0 as "one correction remaining"
    # would retroactively re-open every historical attendance record.
    if count != 1:
        return False, {
            "code": "ALREADY_CLOCKED_OUT",
            "message": str(_("Bản ghi chấm công này đã kết thúc.")),
        }

    # Yesterday's finished day is history. Corrections are for the day in
    # progress only — never a backfill mechanism.
    if attendance.attendance_date != business_today:
        return False, {
            "code": "ALREADY_CLOCKED_OUT",
            "message": str(_("Chỉ có thể cập nhật giờ ra của ngày hôm nay.")),
        }

    previous_out = last_check_out_datetime(attendance, activities)
    if previous_out is not None and out_datetime <= previous_out:
        return False, {
            "code": "CHECKOUT_NOT_LATER",
            "message": str(
                _("Giờ ra mới phải muộn hơn giờ ra đã ghi nhận trước đó.")
            ),
        }

    return True, None


def clock_out_attendance_and_activity(
    employee,
    date_today,
    now,
    out_datetime=None,
    attendance=None,
    is_correction=False,
):
    """
    Record the check-out on both the activity and the attendance row.

    args:
        employee      : employee instance
        date_today    : the check-out's business date
        now           : "HH:MM" of the check-out
        out_datetime  : the authoritative check-out instant
        attendance    : the Attendance row being closed/corrected
        is_correction : True for check-out #2 (see below)

    A correction moves the *existing* mark rather than opening a second
    session. The rule the business asked for is "the last check-out wins":
    08:00 in, 16:00 out, then 17:00 out must read as one 08:00-17:00 day
    (8h paid), never as 08:00-16:00 plus a separate 16:00-17:00 stretch,
    which the old per-activity sum would have totalled as 9h. So the same
    activity row is rewritten in place — no new `AttendanceActivity`, no
    new `Attendance`, nothing counted twice.

    Returns the saved Attendance, or None if the expected activity row is
    missing. Callers must treat None as a failure: see `perform_clock_out`.
    """
    if attendance is None:
        return None

    activities = _day_activities(employee, attendance.attendance_date)

    if is_correction:
        target = activities.filter(clock_out__isnull=False).last()
    else:
        target = activities.filter(clock_out__isnull=True).last()

    if target is None:
        logger.error(
            "No attendance activity found to clock out (attendance=%s, "
            "correction=%s).",
            attendance.pk,
            is_correction,
        )
        return None

    target.clock_out = out_datetime
    target.clock_out_date = date_today
    target.out_datetime = out_datetime
    target.save()

    # Recomputed from scratch across the whole day, never accumulated on
    # top of the previous value — that is what keeps a correction from
    # double-counting the hours it replaces.
    duration = activities_worked_seconds(activities.all())

    attendance.attendance_clock_out = now + ":00"
    attendance.attendance_clock_out_date = date_today
    attendance.attendance_worked_hour = format_time(duration)
    attendance.attendance_overtime = overtime_calculation(attendance)
    attendance.attendance_validated = attendance_validate(attendance)
    attendance.checkout_count = (attendance.checkout_count or 0) + 1
    attendance.save()

    return attendance


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
        # Phase ATT-TIME-2: same single-captured-instant rule as
        # `perform_clock_in` — see the comment there.
        date_today = datetime_now.date()
        if request.__dict__.get("date"):
            date_today = request.date
        day = date_today.strftime("%A").lower()
        day = EmployeeShiftDay.objects.get(day=day)
        now = datetime_now.strftime("%H:%M")
        if request.__dict__.get("time"):
            now = request.time.strftime("%H:%M")

        # Everything from here to the counter increment runs in one
        # transaction with the attendance row locked. Two check-out
        # requests racing (a double tap, a retried mobile request) would
        # otherwise both read `checkout_count == 1`, both pass validation
        # and both write — producing a third check-out and a count of 3.
        # `select_for_update` makes the second request wait for the first
        # to commit, so it reads the already-incremented value and is
        # rejected with CHECKOUT_LIMIT_REACHED like any other third
        # attempt. (A no-op on SQLite, which serialises writes anyway.)
        with transaction.atomic():
            latest = (
                Attendance.objects.filter(employee_id=employee)
                .order_by("id", "attendance_date")
                .last()
            )
            # `.entire()` on the re-fetch, and only on the re-fetch.
            # `JoydigiCompanyManager` appends `.distinct()` whenever a
            # company is selected — which `CompanyMiddleware` does on every
            # request, API calls included — and PostgreSQL rejects
            # `SELECT DISTINCT ... FOR UPDATE` outright ("FOR UPDATE is not
            # allowed with DISTINCT clause"), so every real check-out was
            # answering 500. SQLite ignores `select_for_update` entirely, so
            # neither the local database nor the test suite could ever
            # surface it.
            #
            # This widens nothing: `latest` above was found through the
            # company-scoped manager AND filtered to the authenticated
            # employee, so `latest.pk` is a row this caller has already been
            # shown. Re-fetching that exact primary key only takes the lock
            # on it. The unscoped manager must never be used to *find* an
            # attendance row.
            attendance = (
                Attendance.objects.entire()
                .select_for_update()
                .filter(pk=latest.pk)
                .first()
                if latest is not None
                else None
            )

            if attendance is not None:
                if not attendance.attendance_day:
                    day_name = attendance.attendance_date.strftime("%A").lower()
                    attendance.attendance_day = EmployeeShiftDay.objects.get(
                        day=day_name
                    )
                    attendance.save(update_fields=["attendance_day"])
                day = attendance.attendance_day

            minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
                day=day, shift=shift
            )

            is_correction, reason = validate_clock_out(
                attendance,
                out_datetime=datetime_now,
                business_today=date_today,
                system_checkout=bool(request.__dict__.get("system_checkout")),
            )
            if reason is not None:
                _flash(messages.error, request, reason["message"])
                return None, False, reason

            attendance = clock_out_attendance_and_activity(
                employee=employee,
                date_today=date_today,
                now=now,
                out_datetime=datetime_now,
                attendance=attendance,
                is_correction=is_correction,
            )

        # Phase ATTENDANCE-CHECKOUT-FINAL-WORKTIME-2: never return
        # `(None, True, ...)`. That combination used to be reachable —
        # `clock_out_attendance_and_activity` returned None when it found
        # no activity to close, and this function carried on to `return
        # attendance, True, None`, so `ClockOutAPIView` answered HTTP 200
        # "Clocked-Out" with `attendance_id: null` for a check-out that
        # had written nothing at all. A caller must never be told a write
        # succeeded when it did not.
        if attendance is None:
            reason = {
                "code": "NO_ACTIVE_ATTENDANCE",
                "message": str(
                    _("Không tìm thấy hoạt động chấm công để ghi nhận giờ ra.")
                ),
            }
            _flash(messages.error, request, reason["message"])
            return None, False, reason

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
