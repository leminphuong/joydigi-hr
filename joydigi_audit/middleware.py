"""Middleware ghi nhật ký thao tác của người dùng trong hệ thống chấm công."""

from ipaddress import ip_address
from time import monotonic

from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError

from base.roles import is_checkin_admin, is_checkin_leader
from joydigi_audit.models import UserActivityLog


RESOURCE_LABELS = {
    "dashboard": "Trang tổng quan",
    "today-attendance": "Chấm công hôm nay",
    "approval-hub": "Duyệt đơn",
    "attendance-monthly-summary": "Bảng chấm công",
    "attendance-monthly-summary-conflict-resolve": "Xử lý xung đột bảng công",
    "roster-home": "Xếp ca",
    "employee-view": "Nhân sự",
    "bulletin": "Bảng tin",
    "checkin-settings": "Địa điểm và Wi-Fi",
    "delete-checkin-location": "Địa điểm chấm công",
    "delete-office-wifi": "Wi-Fi văn phòng",
    "holiday-view": "Ngày nghỉ lễ",
    "settings": "Cài đặt",
    "view-my-attendance": "Bảng công của tôi",
    "user-request-view": "Đơn của tôi",
    "employee-profile": "Hồ sơ cá nhân",
    "login": "Đăng nhập",
    "logout": "Đăng xuất",
    "employee-force-mobile-logout": "Đăng xuất khỏi thiết bị",
}

ACTION_HINTS = {
    "policy": "Cập nhật quy định chấm công",
    "location": "Lưu địa điểm chấm công",
    "wifi": "Lưu Wi-Fi văn phòng",
    "approve": "Duyệt yêu cầu",
    "reject": "Từ chối yêu cầu",
    "delete": "Xóa dữ liệu",
}


def _role_of(user):
    if is_checkin_admin(user):
        return UserActivityLog.ROLE_ADMIN
    if is_checkin_leader(user):
        return UserActivityLog.ROLE_LEADER
    return UserActivityLog.ROLE_EMPLOYEE


def _actor_details(user):
    employee = None
    try:
        employee = user.employee_get
    except (AttributeError, ObjectDoesNotExist):
        pass

    if employee is not None:
        name = employee.get_full_name()
        email = employee.email or getattr(user, "email", "")
    else:
        name = user.get_full_name() or getattr(user, "username", "") or str(user)
        email = getattr(user, "email", "")
    return name or "Người dùng", email or "", employee


def _client_ip(request):
    raw_ip = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",", 1)[0].strip()
    raw_ip = raw_ip or request.META.get("REMOTE_ADDR")
    try:
        return str(ip_address(raw_ip)) if raw_ip else None
    except ValueError:
        return None


def _resource_name(route_name, path):
    if route_name in RESOURCE_LABELS:
        return RESOURCE_LABELS[route_name]
    route_text = (route_name or "").replace("-", " ").replace("_", " ").strip()
    if route_text:
        return route_text[:255]
    clean_path = path.strip("/").replace("-", " ")
    return clean_path[:255] or "Hệ thống"


def _action_name(request, route_name, resource):
    method = request.method.upper()
    if route_name == "login" or request.path.rstrip("/") == "/login":
        return "Đăng nhập hệ thống"
    if route_name == "logout" or request.path.rstrip("/") == "/logout":
        return "Đăng xuất hệ thống"
    hint = str(request.POST.get("action", "")).strip().lower() if method == "POST" else ""
    if hint in ACTION_HINTS:
        return ACTION_HINTS[hint]

    route = (route_name or "").lower()
    if method == "GET":
        return f"Xem {resource}"
    if "delete" in route or "remove" in route or method == "DELETE":
        return f"Xóa {resource}"
    if any(word in route for word in ("approve", "validate", "confirm")):
        return f"Duyệt {resource}"
    if any(word in route for word in ("reject", "cancel")):
        return f"Từ chối {resource}"
    if any(word in route for word in ("create", "add")):
        return f"Tạo {resource}"
    return f"Cập nhật {resource}"


def _safe_details(request):
    details = {
        "htmx": request.headers.get("HX-Request") == "true",
        "query_fields": sorted(request.GET.keys())[:20],
    }
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        details["submitted_fields"] = sorted(
            key
            for key in request.POST.keys()
            if key.lower() not in {"csrfmiddlewaretoken", "password", "token", "secret"}
        )[:30]
        for key in ("object_id", "employee_id", "pk", "id"):
            value = request.POST.get(key)
            if value and str(value).isdigit():
                details[key] = str(value)[:40]
    return details


class UserActivityLogMiddleware:
    """Ghi mọi yêu cầu có ý nghĩa của tài khoản đã đăng nhập, không lưu nội dung nhạy cảm."""

    IGNORED_PREFIXES = (
        "/static/",
        "/media/",
        "/health/",
        "/ready/",
        "/jsi18n/",
        "/i18n/",
        "/inbox/notifications/",
        "/nhat-ky-hoat-dong/",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started_at = monotonic()
        initial_user = getattr(request, "user", None)
        company_id = self._company_id(request, initial_user)
        response = self.get_response(request)

        user = initial_user
        if not getattr(user, "is_authenticated", False):
            user = getattr(request, "user", None)
        if not self._should_log(request, user):
            return response

        try:
            actor_name, actor_email, _employee = _actor_details(user)
            route_name = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
            resource = _resource_name(route_name, request.path)
            UserActivityLog.objects.create(
                user=user,
                company_id=company_id,
                actor_name=actor_name,
                actor_email=actor_email,
                role=_role_of(user),
                action=_action_name(request, route_name, resource),
                resource=resource,
                method=request.method.upper(),
                path=request.path[:500],
                route_name=route_name[:150],
                status_code=getattr(response, "status_code", 200),
                ip_address=_client_ip(request),
                user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:500],
                duration_ms=max(0, int((monotonic() - started_at) * 1000)),
                details=_safe_details(request),
            )
        except (DatabaseError, AttributeError, TypeError, ValueError):
            # Nhật ký không bao giờ được phép làm gián đoạn thao tác chính.
            pass
        return response

    def _should_log(self, request, user):
        if request.method.upper() in {"HEAD", "OPTIONS"}:
            return False
        if not getattr(user, "is_authenticated", False):
            return False
        return not request.path.startswith(self.IGNORED_PREFIXES)

    @staticmethod
    def _company_id(request, user):
        selected = request.session.get("selected_company") if hasattr(request, "session") else None
        if selected and selected != "all":
            try:
                return int(selected)
            except (TypeError, ValueError):
                pass
        try:
            return user.employee_get.employee_work_info.company_id_id
        except (AttributeError, ObjectDoesNotExist):
            return None
