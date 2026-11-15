"""
Module for registering LeaveType, LeaveRequest, AvailableLeave
models with the Django admin site.
"""

from django.apps import apps
from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from leave.forms import LeaveTypeAdminForm

from .models import (
    AvailableLeave,
    LeaveAllocationRequest,
    LeaveallocationrequestComment,
    LeaveGeneralSetting,
    LeaveRequest,
    LeaverequestComment,
    LeaveRequestConditionApproval,
    LeaveType,
    RestrictLeave,
)


class LeaveTypeAdmin(admin.ModelAdmin):
    form = LeaveTypeAdminForm


class LeaveRequestAdmin(admin.ModelAdmin):
    """2026-11-15: the decision and the days taken change only through the
    approve / reject / cancel flows (locked, deducted and refunded once)."""

    readonly_fields = ("status", "approved_available_days", "approved_carryforward_days", "requested_days")


# Register your models here.
admin.site.register(LeaveType, LeaveTypeAdmin)
admin.site.register(LeaveRequest, LeaveRequestAdmin)
admin.site.register(AvailableLeave)
admin.site.register(LeaveAllocationRequest, SimpleHistoryAdmin)
admin.site.register(LeaveRequestConditionApproval)
admin.site.register(LeaverequestComment)
admin.site.register(LeaveallocationrequestComment)
admin.site.register(RestrictLeave)
admin.site.register(LeaveGeneralSetting)
if apps.is_installed("attendance"):
    from .models import CompensatoryLeaveRequest

    admin.site.register(CompensatoryLeaveRequest)
