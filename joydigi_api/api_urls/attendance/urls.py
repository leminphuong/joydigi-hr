"""
joydigi_api/urls/attendance/urls.py
"""

from django.urls import path

from joydigi_api.api_views.attendance.permission_views import AttendancePermissionCheck
from joydigi_api.api_views.attendance.views import *

urlpatterns = [
    path("clock-in/", ClockInAPIView.as_view(), name="api-check-in"),
    path("clock-out/", ClockOutAPIView.as_view(), name="api-check-out"),
    path("attendance/", AttendanceView.as_view(), name="api-attendance-list"),
    path("attendance/<int:pk>", AttendanceView.as_view(), name="api-attendance-detail"),
    path(
        "attendance/list/<str:type>",
        AttendanceView.as_view(),
        name="api-attendance-list",
    ),
    path("attendance-validate/<int:pk>", ValidateAttendanceView.as_view()),
    path(
        "attendance-request/",
        AttendanceRequestView.as_view(),
        name="api-attendance-request-view",
    ),
    path(
        "attendance-request/<int:pk>",
        AttendanceRequestView.as_view(),
        name="api-attendance-request-view",
    ),
    path(
        "attendance-request-approve/<int:pk>",
        AttendanceRequestApproveView.as_view(),
        name="api-",
    ),
    path(
        "attendance-request-cancel/<int:pk>",
        AttendanceRequestCancelView.as_view(),
        name="api-",
    ),
    path("overtime-approve/<int:pk>", OvertimeApproveView.as_view(), name="api-"),
    path(
        "attendance-hour-account/<int:pk>/",
        AttendanceOverTimeView.as_view(),
        name="api-",
    ),
    path("attendance-hour-account/", AttendanceOverTimeView.as_view(), name="api-"),
    path("late-come-early-out-view/", LateComeEarlyOutView.as_view(), name="api-"),
    path("attendance-activity/", AttendanceActivityView.as_view(), name="api-"),
    path("today-attendance/", TodayAttendance.as_view(), name="api-"),
    path("offline-employees/count/", OfflineEmployeesCountView.as_view(), name="api-"),
    path("offline-employees/list/", OfflineEmployeesListView.as_view(), name="api-"),
    path("permission-check/attendance", AttendancePermissionCheck.as_view()),
    path("checking-in", CheckingStatus.as_view()),
    path("offline-employee-mail-send", OfflineEmployeeMailsend.as_view()),
    path("converted-mail-template", ConvertedMailTemplateConvert.as_view()),
    path("mail-templates", MailTemplateView.as_view()),
    path("my-attendance/", UserAttendanceView.as_view()),
    path("attendance-type-check/", AttendanceTypeAccessCheck.as_view()),
    path("my-attendance-detailed/<int:id>/", UserAttendanceDetailedView.as_view()),
    path("timesheet/", TimesheetMonthView.as_view(), name="api-timesheet-month"),
    path("policy/", AttendancePolicyView.as_view(), name="api-attendance-policy"),
    path(
        "verify-source/",
        AttendanceVerifySourceView.as_view(),
        name="api-attendance-verify-source",
    ),
    path(
        "verify-face/",
        AttendanceVerifyFaceView.as_view(),
        name="api-attendance-verify-face",
    ),
    # Phase UI-4C.1 — Late/Early permission requests (additive; distinct
    # from AttendanceLateComeEarlyOut, which is system-computed).
    path(
        "late-early-requests/",
        LateEarlyRequestListCreateAPIView.as_view(),
        name="api-late-early-requests",
    ),
    path(
        "late-early-requests/<int:pk>/",
        LateEarlyRequestDetailAPIView.as_view(),
        name="api-late-early-request-detail",
    ),
    path(
        "late-early-request-cancel/<int:pk>/",
        LateEarlyRequestCancelAPIView.as_view(),
        name="api-late-early-request-cancel",
    ),
    # Phase UI-4E.1 — Overtime (OT) permission requests (additive;
    # structurally independent of Attendance.attendance_overtime /
    # attendance_overtime_approve and the existing overtime-approve/
    # <pk> endpoint, which approves real worked OT on an Attendance row,
    # not a pre-request).
    path(
        "overtime-requests/",
        OvertimeRequestListCreateAPIView.as_view(),
        name="api-overtime-requests",
    ),
    path(
        "overtime-requests/<int:pk>/",
        OvertimeRequestDetailAPIView.as_view(),
        name="api-overtime-request-detail",
    ),
    path(
        "overtime-request-cancel/<int:pk>/",
        OvertimeRequestCancelAPIView.as_view(),
        name="api-overtime-request-cancel",
    ),
    # Phase UI-4F.1 — Attendance explanation requests (additive; never
    # touches Attendance/WorkRecords/Timesheet — structurally distinct
    # from the pre-existing attendance-request/ endpoints above, which
    # request a CREATE/UPDATE of the real Attendance row).
    path(
        "explanation-requests/",
        AttendanceExplanationRequestListCreateAPIView.as_view(),
        name="api-explanation-requests",
    ),
    path(
        "explanation-requests/<int:pk>/",
        AttendanceExplanationRequestDetailAPIView.as_view(),
        name="api-explanation-request-detail",
    ),
    path(
        "explanation-request-cancel/<int:pk>/",
        AttendanceExplanationRequestCancelAPIView.as_view(),
        name="api-explanation-request-cancel",
    ),
]
