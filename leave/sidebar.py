"""
leave/sidebar.py
"""

from django.apps import apps
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from base.templatetags.basefilters import is_leave_approval_manager, is_reportingmanager
from joydigi.menu import settings_menu
from leave.templatetags.leavefilters import is_compensatory

MENU = _("Nghỉ phép")
IMG_SRC = "images/ui/leave.svg"

SUBMENUS = [
    {
        "menu": _("Tổng quan nghỉ phép"),
        "redirect": reverse_lazy("leave-dashboard"),
        "accessibility": "leave.sidebar.dashboard_accessibility",
    },
    {
        "menu": _("Đơn nghỉ của tôi"),
        "redirect": reverse_lazy("user-request-view"),
    },
    {
        "menu": _("Đơn nghỉ bù"),
        "redirect": reverse_lazy("view-compensatory-leave"),
        "accessibility": "leave.sidebar.componstory_accessibility",
    },
    {
        "menu": _("Duyệt đơn nghỉ"),
        "redirect": reverse_lazy("request-view"),
        "accessibility": "leave.sidebar.leave_request_accessibility",
    },
    {
        "menu": _("Xin cấp ngày nghỉ"),
        "redirect": reverse_lazy("leave-allocation-request-view"),
    },
    {
        "menu": _("Số ngày nghỉ còn lại"),
        "redirect": reverse_lazy("assign-view"),
        "accessibility": "leave.sidebar.assign_accessibility",
    },
    {
        "menu": _("Thời gian hạn chế nghỉ"),
        "redirect": reverse_lazy("restrict-view"),
        "accessibility": "leave.sidebar.restrict_leave_accessibility",
    },
    {
        "menu": _("Ngày lễ"),
        "redirect": reverse_lazy("holiday-view"),
        # "accessibility": "leave.sidebar.holiday_accessibility",
    },
    {
        "menu": _("Ngày nghỉ hằng tuần"),
        "redirect": reverse_lazy("company-leave-view"),
        "accessibility": "leave.sidebar.company_leave_accessibility",
    },
]


def dashboard_accessibility(request, submenu, user_perms, *args, **kwargs):
    have_perm = request.user.is_superuser or request.user.has_perm(
        "leave.delete_leaverequest"
    )
    if not have_perm:
        submenu["redirect"] = (
            reverse_lazy("leave-employee-dashboard") + "?dashboard=true"
        )
    return True


def leave_request_accessibility(request, submenu, user_perms, *args, **kwargs):
    return (
        request.user.has_perm("leave.view_leaverequest")
        or is_leave_approval_manager(request.user)
        or is_reportingmanager(request.user)
    )


def leave_type_accessibility(request, submenu, user_perms, *args, **kwargs):
    # Phase LEAVE-7A.2: same permission the existing LeaveType CRUD
    # views already require (`leave.view_leavetype` — see
    # `leave/cbv/leave_types.py`) — an ordinary employee with no prior
    # LeaveType permission must not see this menu item appear.
    #
    # Phase LEAVE-7A.3A: "Loại nghỉ phép" is no longer a SUBMENUS entry
    # of the "Nghỉ phép" collapsible group — it's rendered as its own
    # permanent, non-collapsible top-level `<li>` directly in
    # `templates/sidebar.html` (the only way to get a flat, always-
    # visible link in this sidebar's markup — every `SUBMENUS` entry
    # registered the normal way is nested one level under a
    # collapsible parent `MENU`, by that template's own design). This
    # function is kept — and still covered by its own tests — as the
    # single source of truth for the permission check
    # (`leave.view_leavetype`); the template calls it the same way via
    # `{% if perms.leave.view_leavetype %}`, which resolves to the
    # exact same `user.has_perm(...)` check, superuser bypass included.
    return request.user.has_perm("leave.view_leavetype")


def assign_accessibility(request, submenu, user_perm, *args, **kwargs):
    submenu["redirect"] = submenu["redirect"] + "?field=leave_type_id"
    return request.user.has_perm("leave.view_availableleave") or is_reportingmanager(
        request.user
    )


def holiday_accessibility(request, submenu, user_perms, *args, **kwargs):
    return not request.user.is_superuser and not request.user.has_perm(
        "base.view_holidays"
    )


def company_leave_accessibility(request, submenu, user_perms, *args, **kwargs):
    return not request.user.is_superuser and not request.user.has_perm(
        "base.view_companyleaves"
    )


def restrict_leave_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("leave.view_restrictleave")


def componstory_accessibility(request, submenu, user_perms, *args, **kwargs):
    return apps.is_installed("attendance") and is_compensatory(request.user)


# ---------------------------------------------------------------------------
# Settings menu registrations
# ---------------------------------------------------------------------------


def leave_rules_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("leave.add_restrictleave") or (
        apps.is_installed("attendance")
        and request.user.has_perm("attendance.view_attendancevalidationcondition")
    )


def leave_settings_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("leave.view_restrictleave")


@settings_menu.register
class LeaveSettings:
    title = _("Nghỉ phép")
    order = 6
    condition = lambda self, request: apps.is_installed("leave")
    items = [
        {
            "label": _("Quy định nghỉ phép"),
            "url": reverse_lazy("leave-rules-view"),
            "accessibility": leave_rules_accessibility,
            "search_entries": [
                {
                    "text": _("Nghỉ bù"),
                    "description": _("Cho phép nhân viên gửi đơn nghỉ bù"),
                },
                {
                    "text": _("Giới hạn đơn nghỉ trong quá khứ"),
                    "description": _("Chỉ quản trị viên được tạo đơn nghỉ cho ngày đã qua"),
                },
            ],
        },
    ]
