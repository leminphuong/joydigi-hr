"""
attendance/sidebar.py
"""

from django.apps import apps
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from base.context_processors import enable_late_come_early_out_tracking
from base.templatetags.basefilters import is_reportingmanager
from joydigi.menu import settings_menu

MENU = _("Chấm công")
IMG_SRC = "images/ui/attendances.svg"


SUBMENUS = [
    {
        "menu": _("Tổng quan chấm công"),
        "redirect": reverse_lazy("attendance-dashboard"),
        "accessibility": "attendance.sidebar.dashboard_accessibility",
    },
    {
        "menu": _("Công của tôi"),
        "redirect": reverse_lazy("view-my-attendance"),
    },
    {
        "menu": _("Chấm công nhân viên"),
        "redirect": reverse_lazy("attendance-view"),
        "accessibility": "attendance.sidebar.attendances_accessibility",
    },
    {
        "menu": _("Yêu cầu chỉnh công"),
        "redirect": reverse_lazy("request-attendance-view"),
    },
    {
        "menu": _("Tình trạng làm việc hôm nay"),
        "redirect": reverse_lazy("work-records"),
        "accessibility": "attendance.sidebar.work_record_accessibility",
    },
    {
        "menu": _("Lịch sử vào và ra"),
        "redirect": reverse_lazy("attendance-activity-view"),
    },
    {
        "menu": _("Đi muộn và về sớm"),
        "redirect": reverse_lazy("late-come-early-out-view"),
        "accessibility": "attendance.sidebar.tracking_accessibility",
    },
    {
        "menu": _("Bảng công tháng"),
        "redirect": reverse_lazy("attendance-monthly-summary"),
        "accessibility": "attendance.sidebar.monthly_summary_accessibility",
    },
    {
        "menu": _("Quy định chấm công"),
        "redirect": reverse_lazy("grace-time-view"),
        "accessibility": "attendance.sidebar.validation_condition_accessibility",
    },
]


def attendances_accessibility(request, submenu, user_perms, *args, **kwargs):
    """
    Check if the user has permission to view attendance or is a reporting manager.
    """
    return request.user.has_perm("attendance.view_attendance") or is_reportingmanager(
        request.user
    )


def work_record_accessibility(request, submenu, user_perms, *args, **kwargs):
    """
    Check if the user has permission to view attendance or is a reporting manager.
    """
    return (
        request.user.is_superuser
        or request.user.has_perm("attendance.view_attendance")
        or is_reportingmanager(request.user)
    )


def dashboard_accessibility(request, submenu, user_perms, *args, **kwargs):
    """
    Check if the user has permission to view attendance or is a reporting manager.
    """
    return (
        request.user.is_superuser
        or request.user.has_perm("attendance.view_attendance")
        or is_reportingmanager(request.user)
    )


def tracking_accessibility(request, submenu, user_perms, *args, **kwargs):
    """
    Determine if late come/early out tracking is enabled and user has access.
    """
    tracking_enabled = enable_late_come_early_out_tracking(None).get("tracking")
    has_access = (
        request.user.is_superuser
        or request.user.has_perm("attendance.view_attendancelatecomeearlyout")
        or is_reportingmanager(request.user)
    )
    return tracking_enabled and has_access


def monthly_summary_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("attendance.view_attendance") or is_reportingmanager(
        request.user
    )


# ---------------------------------------------------------------------------
# Settings menu registrations
# ---------------------------------------------------------------------------


def validation_condition_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("attendance.view_attendancevalidationcondition")


def ip_restriction_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("attendance.add_attendance")


def attendance_rule_accessibility(request, submenu, user_perms, *args, **kwargs):
    user = request.user
    return (
        user.has_perm("base.view_tracklatecomeearlyout")
        or user.has_perm("attendance.change_attendancegeneralsetting")
        or user.has_perm("attendance.view_attendancegeneralsetting")
        or user.has_perm("attendance.add_attendance")
    )


@settings_menu.register
class AttendanceSettings:
    title = _("Chấm công")
    order = 5
    condition = lambda self, request: apps.is_installed("attendance")
    items = [
        {
            "label": _("Quy định chấm công"),
            "url": reverse_lazy("attendance-rule-view"),
            "accessibility": attendance_rule_accessibility,
            "search_entries": [
                {
                    "text": _("Bật nút chấm công vào và ra"),
                    "description": _("Cho phép nhân viên chấm công bằng nút vào và ra"),
                },
                {
                    "text": _("Theo dõi thời gian làm việc"),
                    "description": _("Hiển thị thời gian đang làm việc ngay trên thanh menu"),
                },
                {
                    "text": _("Theo dõi đi muộn và về sớm"),
                    "description": _("Ghi nhận nhân viên đi muộn hoặc về sớm"),
                },
                {
                    "text": _("Giới hạn mạng chấm công"),
                    "description": _("Chỉ cho phép chấm công từ các mạng đã chọn"),
                },
            ],
        },
    ]
