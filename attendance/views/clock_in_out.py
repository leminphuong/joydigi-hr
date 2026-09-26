"""
clock_in_out.py

This module is used register endpoints to the check-in check-out functionalities
"""

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

from attendance.methods.session import (
    CHECKOUT_STATES,
    TODAY_MALFORMED,
    open_activities_for as session_open_activities,
    resolve_session,
)
from attendance.methods.client_ip import (
    client_ip_is_allowed,
    resolve_attendance_client_ip,
)
from attendance.methods.remote_work import (
    approved_remote_request,
    record_evidence,
    record_session_start,
    remote_check_in_evidence,
)
from attendance.methods.utils import (
    activity_datetime,
    employee_exists,
    format_time,
    overtime_calculation,
    shift_schedule_today,
    strtime_seconds,
)
from attendance.methods.workday_rules import (
    can_check_out_yet,
    is_early_out,
    is_late,
)
from attendance.methods.worktime import activities_worked_seconds
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceEvidence,
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


#: The refusal a company-network restriction produces. Unchanged from
#: before: same code, same Vietnamese sentence, same controlled 400.
_WIFI_NOT_ALLOWED = {
    "code": "WIFI_NOT_ALLOWED",
    "message": "Mạng hiện tại của bạn không được phép dùng để chấm công.",
}

#: Phase FIX A. Today's record cannot be resolved to one session —
#: its two check-out columns disagree, or more than one activity is
#: open for the same day. Refusing is the point: the alternative is
#: choosing a record to write to, and a wrong guess here silently
#: rewrites somebody's working day. The message names no row and no
#: internals; it tells the employee who can fix it.
_ATTENDANCE_STATE_CONFLICT = {
    "code": "ATTENDANCE_STATE_CONFLICT",
    "message": (
        "Dữ liệu chấm công hôm nay của bạn đang không nhất quán. "
        "Vui lòng liên hệ quản trị viên để được hỗ trợ."
    ),
}


def _network_refusal(request, company):
    """The refusal to return when this network may not mark attendance.

    `None` means carry on — either the company has no restriction, or it
    has one and this request satisfies it.

    Phase 3B FINAL. Check-in and check-out had two byte-identical copies
    of this, which is how they came to share a bug: `request.META.get`
    on the synthetic request answers every key with its default, so the
    address was `None`, `ipaddress.ip_address(None)` raised `ValueError`,
    the `except` swallowed it, and every range — `0.0.0.0/0` included —
    refused every mobile check-in. One copy now, so the two paths cannot
    drift again.

    `trusted_device` still exempts a caller entirely. That is the
    existing contract for `attendance.scheduler`'s auto-punch-out, which
    is internal infrastructure with no network origin to check, and it is
    deliberately left alone.

    Everything else fails closed: no resolvable address means refuse, not
    admit. See `attendance.methods.client_ip` for why an address is
    resolvable only behind this host's own proxy.
    """
    if getattr(request, "trusted_device", False):
        return None

    allowed_attendance_ips = AttendanceAllowedIP.objects.filter(
        company_id=company
    ).first()
    if not allowed_attendance_ips or not allowed_attendance_ips.is_enabled:
        return None

    allowed_ips = (allowed_attendance_ips.additional_data or {}).get(
        "allowed_ips", []
    )
    client_ip = resolve_attendance_client_ip(request)
    if client_ip_is_allowed(client_ip, allowed_ips):
        return None
    return dict(_WIFI_NOT_ALLOWED)


def _server_instant(request):
    """The instant this punch happens, by the server's clock.

    The same derivation the two punch functions already do inline, so
    the date a remote permission is checked against is the date the
    attendance row is written from. `request.datetime` is set by the
    mobile API from `django.utils.timezone` (never from the handset)
    and by the biometric importer; everything else falls through to
    the server clock. A device with a wrong clock cannot widen its own
    permission, because it never supplies this value.
    """
    if request.__dict__.get("datetime"):
        return request.datetime
    return timezone.localtime()


def _remote_permission(request, refusal):
    """Phase ONLINE-2. Whether an approved remote request rescues this
    refusal — `(reason, remote_request)`.

    Called only when `_network_refusal` has already refused, which is
    what keeps the exception narrow in both directions. The office
    path is evaluated first and is completely unchanged; permission is
    consulted second, and only ever about the one thing it is
    permission for — working somewhere other than the office network.
    A refusal for any other cause never reaches here, so an invalid
    proof, a malformed payload or a conflicted session still fails
    exactly as before.

    Consulting it lazily also means the ordinary office punch does not
    pay for a query it does not need: an employee on the office
    network never reaches this function at all.
    """
    employee, _work_info = employee_exists(request)
    if employee is None:
        return refusal, None
    remote_request = approved_remote_request(
        employee, _server_instant(request).date()
    )
    if remote_request is None:
        return refusal, None
    return None, remote_request


def _remote_session_permission(request, refusal):
    """Phase ONLINE-2. Whether the session being closed was opened
    remotely — `(reason, check_in_evidence)`.

    This deliberately does NOT ask whether the employee holds an
    approved request today. That question would let somebody who
    checked in at the office close their day from anywhere, which is
    the single most dangerous shape this feature could take. It asks
    the record instead: the canonical session for this date, and
    whether its own check-in was written as `REMOTE`.

    Because the answer was persisted when the session opened, a
    permission revoked since then cannot strand a session that is
    already running — the employee can still close their day.

    `resolve_session` is the FIX A resolver, unchanged and read-only;
    it runs here only on the refusal path, and again later in the
    normal flow, which costs one extra read for a punch that was about
    to be refused anyway.
    """
    employee, _work_info = employee_exists(request)
    if employee is None:
        return refusal, None
    date_today = _server_instant(request).date()
    if request.__dict__.get("date"):
        date_today = request.date
    session = resolve_session(employee, date_today)
    if session.state not in CHECKOUT_STATES or session.attendance is None:
        return refusal, None
    evidence = remote_check_in_evidence(session.attendance)
    if evidence is None:
        return refusal, None
    return None, evidence


def _remote_overrides_source(checkin_source):
    """Whether a source refusal is one that remote permission answers.

    Exactly two codes, and both say the same thing in different
    words: *you are not at the office*. `WIFI_NOT_ALLOWED` means this
    SSID is not one of the company's, and `LOCATION_OUTSIDE` means
    these coordinates are beyond the office radius. For somebody with
    permission to work remotely neither is a finding — it is the
    expected state, which is the whole point of the permission.

    Every other refusal survives untouched: `LOCATION_INVALID` is a
    malformed payload, `VERIFICATION_REQUIRED` is a proof that did not
    check out, and the `QR_*` family are statements about a kiosk code
    rather than about where the employee is standing. None of those
    becomes acceptable because somebody may work from home.
    """
    return (checkin_source or {}).get("code") in {
        "WIFI_NOT_ALLOWED",
        "LOCATION_OUTSIDE",
    }


#: Phase FIX A.1. A day shift is finalized only once its date is behind
#: us, not the moment its end time passes. Somebody still at their desk
#: at 17:05 has not forgotten anything, and writing a 17:00 check-out
#: over them would erase real hours and fight the end-of-day reminders,
#: which are still nudging them at end + 10 minutes. The invariant being
#: enforced is "never open into a *later workday*", and this is the line
#: that says so — one constant, so the policy is one place to change.
FINALIZE_ONLY_AFTER_DAY_ROLLOVER = True

#: Reported instead of a crash when the employee has no linked user or no
#: work information — the two things the shared check-out path reads
#: without a guard. Never a repair: the session is left exactly as it is.
MISSING_EMPLOYEE_PROFILE = "MISSING_EMPLOYEE_PROFILE"

#: Reported when closing one session raised. The session is left exactly
#: as it was, the employee's other sessions still run, and the next
#: scheduler pass will try this one again.
FINALIZATION_ERROR = "FINALIZATION_ERROR"


def finalize_forgotten_sessions(employee, now=None):
    """Close day shifts this employee forgot, at their configured end.

    Returns `(finalized, blocked)` — the sessions closed, and the
    `(attendance, reason)` pairs that were deliberately left alone.

    This is *not* the configurable Auto Check Out feature. It does not
    read `is_auto_punch_out_enabled` and it does not use
    `auto_punch_out_time`; it uses `EmployeeShiftSchedule.end_time` for
    the session being closed. Auto Check Out remains what it was: an
    administrator's choice to close at a time they nominate.

    What it will not touch, ever: a session dated before
    `ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF` (Phase FIX A.1B — those
    predate the policy and belong to an administrator, not to a
    scheduled job), a night shift even once expired (a night worker past
    their configured end is the case most likely to be genuinely still
    working), a row whose two check-out columns disagree, a session with
    no usable shift end, and a session whose activity cannot be paired
    one-to-one. Each of those is returned in `blocked` and logged, never
    guessed at. With no cutoff configured, nothing is finalized at all.

    Each session is closed through `perform_clock_out` — the same shared
    business logic a person's check-out uses, so worked hours, the lunch
    deduction, early-out and validation all behave identically. The one
    difference is `system_finalization`, which leaves
    `attendance_overtime` alone.
    """
    from attendance.methods.session import expired_sessions_for
    from attendance.methods.utils import Request as SystemRequest

    now = now or timezone.localtime()
    before = timezone.localdate(now) if FINALIZE_ONLY_AFTER_DAY_ROLLOVER else None
    ready, blocked = expired_sessions_for(employee, now=now, before_date=before)

    for attendance, reason in blocked:
        logger.warning(
            "forgotten_session_finalization skipped attendance=%s date=%s reason=%s",
            attendance.pk,
            attendance.attendance_date,
            reason,
        )

    finalized = []
    for session in ready:
        profile_gap = _incomplete_profile_reason(employee)
        if profile_gap is not None:
            # The shared check-out path dereferences the work info without
            # a guard, so reaching it with an incomplete profile raises
            # rather than refusing. Caught here instead, where the session
            # can be reported for what it is.
            logger.warning(
                "forgotten_session_finalization skipped attendance=%s "
                "date=%s reason=%s",
                session.attendance.pk,
                session.session_date,
                profile_gap,
            )
            blocked.append((session.attendance, profile_gap))
            continue

        ends_at = session.ends_at
        try:
            # A savepoint per session, for two reasons. The attendance row
            # and its activity are closed together or not at all; and one
            # session that fails cannot roll back a session that already
            # succeeded, nor the caller's own transaction — which, on the
            # check-in path, is today's check-in.
            with transaction.atomic():
                _attendance, allowed, refusal = perform_clock_out(
                    SystemRequest(
                        user=employee.employee_user_id,
                        date=session.session_date,
                        time=ends_at.time(),
                        datetime=ends_at,
                        # Internal reconciliation: no network origin to
                        # check, and no attendance-source evidence to
                        # supply.
                        trusted_device=True,
                        system_checkout=True,
                        system_finalization=True,
                    )
                )
                if not (allowed and _attendance is not None):
                    code = (refusal or {}).get("code") or "NO_OPEN_ATTENDANCE"
                    # A refusal can still have written the incidental
                    # `attendance_day` backfill on its way to refusing.
                    # Rolling back to the savepoint keeps a session that
                    # changed nothing from changing anything.
                    transaction.set_rollback(True)
                else:
                    code = None
        except Exception as error:
            # One unreasonable session must not cost this employee their
            # other sessions, and must never propagate into a caller that
            # is in the middle of checking somebody in. The class name is
            # logged, never the message: it may quote row content.
            logger.warning(
                "forgotten_session_finalization failed attendance=%s "
                "date=%s error=%s",
                session.attendance.pk,
                session.session_date,
                type(error).__name__,
            )
            blocked.append((session.attendance, FINALIZATION_ERROR))
            continue

        if code is None:
            finalized.append(session)
        else:
            logger.warning(
                "forgotten_session_finalization refused attendance=%s date=%s code=%s",
                session.attendance.pk,
                session.session_date,
                code,
            )
            blocked.append((session.attendance, code))
    return finalized, blocked


def _incomplete_profile_reason(employee):
    """Why a scheduled job cannot check this employee out, or None.

    `employee_exists` swallows both lookups and returns `None` for
    whatever is missing, and `perform_clock_out` then reads
    `work_info.shift_id` unguarded — so an employee with no linked user
    or no work information raises there instead of refusing. Checking
    first turns that into a named, reportable refusal and leaves the
    shared check-out path untouched.
    """
    if getattr(employee, "employee_user_id", None) is None:
        return MISSING_EMPLOYEE_PROFILE
    # A reverse one-to-one raises `RelatedObjectDoesNotExist` when absent,
    # which subclasses AttributeError — so the default is returned.
    if getattr(employee, "employee_work_info", None) is None:
        return MISSING_EMPLOYEE_PROFILE
    return None


def _finalize_before_check_in(employee, now):
    """Best-effort finalization, immediately before today's check-in.

    Never propagates. Refusing somebody's check-in because yesterday
    could not be tidied is precisely the trap FIX A exists to prevent,
    and one untidy historical row is a strictly better outcome than an
    employee who cannot start work. Each session inside already runs in
    its own savepoint, so a failure here cannot mark the surrounding
    transaction — the one about to write today's attendance — for
    rollback.
    """
    try:
        finalize_forgotten_sessions(employee, now=now)
    except Exception as error:
        logger.warning(
            "forgotten_session_finalization before check-in failed for "
            "employee=%s error=%s",
            employee.pk,
            type(error).__name__,
        )


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
    # Phase ATTENDANCE-WORKDAY-RULES-SAFE-IMPLEMENT-1: on an ordinary day
    # shift, lateness is a fixed clock time — on time through 08:30:59, late
    # from 08:31:00 — rather than the shift's start plus whatever grace
    # happens to be configured. The official start stays 08:00; the
    # allowance is the rule. Night shifts keep the branch above, which reads
    # the schedule, because a fixed morning boundary means nothing to them.
    elif is_late(attendance.attendance_clock_in):
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
        # Phase ONLINE-2. The office path runs first and unchanged. Only
        # a *network* refusal is offered a second answer, and only from
        # an approved RemoteWorkRequest covering today — so an employee
        # with no permission sees the identical refusal they saw before
        # this phase existed, and an employee who is on the office
        # network never reaches the lookup at all.
        remote_request = None
        reason = _network_refusal(request, company)
        if reason is not None:
            reason, remote_request = _remote_permission(request, reason)
        if reason is not None:
            _flash(messages.error, request, reason["message"])
            return None, False, reason

        checkin_source = validate_checkin_source(request, company)
        if (
            not checkin_source["allowed"]
            and remote_request is not None
            and _remote_overrides_source(checkin_source)
        ):
            # Working from home is not a failed office check. The
            # evidence itself is still recorded below; only its verdict
            # is answered by the permission.
            checkin_source = {"allowed": True, "method": "Làm online"}
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
            # Phase FIX A.1: the safety net for a scheduled job that did
            # not run. Before today's session is created, any *expired*
            # day shift the employee forgot to close is finalized at its
            # own configured end time — inside the same transaction, so a
            # failure cannot leave yesterday half-closed beside a fresh
            # row for today.
            #
            # A session that cannot be finalized safely is skipped, not
            # forced: a malformed row, a missing schedule, or an activity
            # that cannot be paired one-to-one is left exactly as it is
            # and logged. Today's check-in still proceeds — refusing it
            # would trap the employee, which is the bug FIX A exists to
            # prevent and would be a strictly worse outcome than one
            # untidy historical row.
            with transaction.atomic():
                _finalize_before_check_in(employee, datetime_now)
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
                # Phase ONLINE-2. Inside the same transaction as the row
                # it describes: a session that exists without the record
                # of how it was opened could never be closed remotely,
                # and a record without a session would describe nothing.
                #
                # Phase ONLINE-2F: written for an office punch too, not
                # only a remote one. `remote_request` is None on the
                # office path, which is exactly what makes the recorded
                # mode OFFICE and replaces any REMOTE row left over from
                # an earlier session of the same day.
                record_session_start(
                    attendance=attendance,
                    request=request,
                    company=company,
                    captured_at=datetime_now,
                    remote_work_request=remote_request,
                )
            # A remote punch is already approved — by a manager, before
            # the day began. Filing it into the outside-radius queue
            # would be asking for that same permission a second time, so
            # the existing queue is left for the office flow it was
            # built for and is not touched here.
            if remote_request is None:
                _mark_outside_radius_request(attendance, checkin_source)
                if checkin_source.get("outside_radius"):
                    _flash(
                        messages.warning,
                        request,
                        checkin_source["message"]
                        + " Bản ghi đã được chuyển sang chờ duyệt.",
                    )
            return attendance, True, None
        reason = {
            "code": "PROFILE_INCOMPLETE",
            "message": (
                "Hồ sơ nhân viên hoặc thông tin công việc của bạn chưa đầy đủ. "
                "Vui lòng liên hệ quản trị viên."
            ),
        }
        _flash(messages.error, request, reason["message"])
        return None, False, reason

    reason = {
        "code": "METHOD_NOT_ENABLED",
        "message": "Công ty của bạn chưa bật tính năng chấm công.",
    }
    _flash(messages.error, request, reason["message"])
    return None, False, reason


def _activity_check_in_moment(activity):
    """
    When an open `AttendanceActivity` was actually checked in.

    `in_datetime` is the authoritative stamp. Rows written before it existed
    only carry the date and time separately, so those are combined and read
    in the current timezone; anything else yields None and the 30-minute rule
    then simply has no opinion.
    """
    if activity.in_datetime:
        return activity.in_datetime
    clock_in_date = activity.clock_in_date or activity.attendance_date
    if not clock_in_date or not activity.clock_in:
        return None
    return timezone.make_aware(
        datetime.combine(clock_in_date, activity.clock_in),
        timezone.get_current_timezone(),
    )


def clock_out_attendance_and_activity(
    employee,
    date_today,
    now,
    out_datetime=None,
    session=None,
):
    """
    Clock out the attendance and activity
    args:
        employee    : employee instance
        date_today  : today date
        now         : now
        session     : the resolved `AttendanceSession` this check-out
                      belongs to (Phase FIX A). When given, both rows
                      are taken from that one session date.
    """

    # Phase FIX A: the session decides which rows this closes, and the
    # session is a single date. Before, the activity was chosen by
    # "newest open, any date" and the attendance row by "newest by date,
    # open or not" — two rules that never consulted each other, so a
    # check-out could close Tuesday's activity against Wednesday's row
    # and leave Tuesday open forever. `session` is passed in by
    # `perform_clock_out`, which resolved it; `None` keeps the old
    # entry point working for callers that have not been converted.
    if session is not None:
        session_date = session.session_date
        open_for_session = session_open_activities(employee, session_date)
        if not open_for_session:
            logger.error(
                "No attendance clock in activity found that needs clocking out."
            )
            return None
        attendance_activity = open_for_session[0]
        attendance_activities = AttendanceActivity.objects.filter(
            employee_id=employee, attendance_date=session_date
        ).order_by("attendance_date", "id")
        attendance = session.attendance
    else:
        attendance_activities = AttendanceActivity.objects.filter(
            employee_id=employee,
        ).order_by("attendance_date", "id")
        if not attendance_activities.filter(clock_out__isnull=True).exists():
            logger.error(
                "No attendance clock in activity found that needs clocking out."
            )
            return None
        attendance_activity = attendance_activities.filter(
            clock_out__isnull=True
        ).last()
        attendance_activities = attendance_activities.filter(
            attendance_date=attendance_activity.attendance_date
        )
        attendance = (
            Attendance.objects.filter(employee_id=employee)
            .order_by("-attendance_date", "-id")
            .first()
        )

    if attendance is not None:
        attendance_activity.clock_out = out_datetime
        attendance_activity.clock_out_date = date_today
        attendance_activity.out_datetime = out_datetime
        attendance_activity.save()
        # Total worked time for the day with the unpaid 12:00-13:00 lunch hour
        # excluded, so an 08:00-17:00 day is 8h rather than 9h.
        #
        # Phase ATTENDANCE-CHECKOUT-SAFE-ROLLBACK-1: the corrected-checkout
        # feature this function previously carried has been rolled back, but
        # the lunch rule has not. It is a separate business rule, it is not
        # what made check-out fail, and `attendance.methods.worktime` is now
        # shared with the weekend-overtime summary — so reverting it here
        # would change worked hours everywhere for no reason.
        duration = format_time(activities_worked_seconds(attendance_activities))
        # The row was decided above, from the session. It is no longer
        # re-selected here — that second, independent lookup is what
        # let a check-out close a different day than the one it had
        # just closed the activity for.
        attendance.attendance_clock_out = now + ":00"
        attendance.attendance_clock_out_date = date_today
        attendance.attendance_worked_hour = duration
        # Overtime calculation.
        #
        # Phase FIX A.1 note, because this looks like a place to suppress
        # overtime for a system-finalized day and is not: `attendance_overtime`
        # is *derived*, not stored. `Attendance.save()` calls
        # `update_attendance_overtime()` unconditionally and recomputes it —
        # along with `overtime_second` and `at_work_second` — from
        # `attendance_worked_hour` and `minimum_hour`. Assigning something
        # else here is simply overwritten two lines below, and forcing it
        # afterwards would leave those three fields disagreeing with each
        # other.
        #
        # What keeps finalization from inventing overtime is the time it
        # writes: the shift's configured end. An 08:00-17:00 day closed at
        # 17:00 works exactly its minimum hours and yields 00:00. Overtime
        # can only appear when the employee's own check-in was earlier than
        # their shift start, which is their record, not the system's
        # invention. Suppressing it properly belongs in the model, where the
        # derivation lives, and would change manual check-out too.
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
    # Phase ATTENDANCE-WORKDAY-RULES-SAFE-IMPLEMENT-1: on an ordinary day
    # shift, leaving early is a fixed clock time — early before 16:30:00,
    # not early from 16:30:00 — rather than "before the shift's end_time".
    # The official end stays 17:00. Night shifts keep the branch above.
    if is_early_out(clock_out_time):
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
        # Phase ONLINE-2. The network gate may only be answered here by
        # the session's OWN check-in record, never by "does this person
        # have an approved request today?" — that question would let an
        # office session be closed from anywhere, which is the one shape
        # this feature must not take. See `_remote_session_permission`.
        remote_evidence = None
        reason = _network_refusal(request, company)
        if reason is not None:
            reason, remote_evidence = _remote_session_permission(request, reason)
        if reason is not None:
            _flash(messages.error, request, reason["message"])
            return None, False, reason

        checkin_source = validate_checkin_source(request, company)
        if (
            not checkin_source["allowed"]
            and remote_evidence is not None
            and _remote_overrides_source(checkin_source)
        ):
            checkin_source = {"allowed": True, "method": "Làm online"}
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

        # Phase ATTENDANCE-CHECKOUT-30MIN-SAFE-IMPLEMENT-1: someone must stay
        # checked in for a full 30 minutes before they may check out. This sits
        # here, above every write below — including the `attendance_day`
        # backfill — so a refusal leaves the database exactly as it found it:
        # the activity stays open, no clock-out is stored, no early-out or
        # work record is created. It is a plain read plus a subtraction, with
        # no row lock of any kind; see `test_checking_out_takes_no_row_lock`.
        open_activity = (
            AttendanceActivity.objects.filter(
                employee_id=employee, clock_out__isnull=True
            )
            .order_by("attendance_date", "id")
            .last()
        )
        # `system_checkout` exempts the scheduled end-of-day job (and the
        # opt-in auto-punch-out) from this rule only. The minimum exists to
        # stop an employee checking out moments after arriving; a day that
        # began late must still be closed rather than left open into
        # tomorrow. `Request.system_checkout` defaults to False, so no
        # user-facing caller can reach this branch.
        if (
            open_activity is not None
            and not getattr(request, "system_checkout", False)
            and not getattr(request, "system_finalization", False)
            and not can_check_out_yet(
                _activity_check_in_moment(open_activity), datetime_now
            )
        ):
            reason = {
                "code": "CHECKOUT_TOO_SOON",
                "message": str(
                    _(
                        "Bạn chỉ có thể chấm công ra sau 30 phút "
                        "kể từ lúc chấm công vào."
                    )
                ),
            }
            _flash(messages.error, request, reason["message"])
            return None, False, reason

        shift = work_info.shift_id
        # Phase ATT-TIME-2: same single-captured-instant rule as
        # `perform_clock_in` — see the comment there.
        date_today = datetime_now.date()
        if request.__dict__.get("date"):
            date_today = request.date
        day = date_today.strftime("%A").lower()
        day = EmployeeShiftDay.objects.get(day=day)

        # Phase FIX A: one resolved session decides everything below.
        # This used to be `order_by("id", "attendance_date").last()` —
        # the employee's newest row by id, with no relation to the day
        # being checked out of, and a third different rule again from
        # the two inside `clock_out_attendance_and_activity`.
        session = resolve_session(employee, date_today)
        if session.state == TODAY_MALFORMED:
            reason = dict(_ATTENDANCE_STATE_CONFLICT)
            _flash(messages.error, request, reason["message"])
            return None, False, reason
        if session.state not in CHECKOUT_STATES:
            # Nothing open for this day. A day shift left open
            # yesterday is deliberately not a candidate: it belongs to
            # yesterday, and closing it now would stamp it with a time
            # nobody observed. The caller turns this into
            # NO_OPEN_ATTENDANCE.
            return None, True, None
        if len(session_open_activities(employee, session.session_date)) > 1:
            # Two activities open on the same day: which one this
            # check-out belongs to cannot be known, and guessing would
            # close the wrong half of somebody's day.
            reason = dict(_ATTENDANCE_STATE_CONFLICT)
            _flash(messages.error, request, reason["message"])
            return None, False, reason

        attendance = session.attendance
        if attendance is not None:
            if not attendance.attendance_day:
                day_name = attendance.attendance_date.strftime("%A").lower()
                attendance.attendance_day = EmployeeShiftDay.objects.get(day=day_name)
                attendance.save(update_fields=["attendance_day"])
            day = attendance.attendance_day
        now = datetime_now.strftime("%H:%M")
        if request.__dict__.get("time"):
            now = request.time.strftime("%H:%M")
        minimum_hour, start_time_sec, end_time_sec = shift_schedule_today(
            day=day, shift=shift
        )
        # One transaction: the activity and the attendance row either
        # both close or neither does. No row lock — `select_for_update`
        # on this path is what took production down, because PostgreSQL
        # refuses FOR UPDATE alongside the manager's DISTINCT and
        # `Meta.ordering`'s outer join.
        with transaction.atomic():
            attendance = clock_out_attendance_and_activity(
                employee=employee,
                date_today=date_today,
                now=now,
                out_datetime=datetime_now,
                session=session,
            )
            # Phase ONLINE-2. Carries the same RemoteWorkRequest the
            # check-in recorded rather than looking one up again, so the
            # pair describes one session authorised once.
            if remote_evidence is not None and attendance is not None:
                record_evidence(
                    attendance=attendance,
                    action=AttendanceEvidence.ACTION_CHECK_OUT,
                    attendance_mode=AttendanceEvidence.MODE_REMOTE,
                    request=request,
                    company=company,
                    captured_at=datetime_now,
                    remote_work_request=remote_evidence.remote_work_request,
                )
        if remote_evidence is None:
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
        "message": "Công ty của bạn chưa bật tính năng chấm công.",
    }
    _flash(messages.error, request, reason["message"])
    return None, False, reason
