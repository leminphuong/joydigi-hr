"""
admin.py

This page is used to register the model with admins site.
"""

from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from employee.models import (
    Actiontype,
    BonusPoint,
    DisciplinaryAction,
    Employee,
    EmployeeBankDetails,
    EmployeeFace,
    EmployeeNote,
    EmployeeTag,
    EmployeeWorkInformation,
    Policy,
    PolicyMultipleFile,
)

# Register your models here.

# admin.site.register(Employee)
admin.site.register(EmployeeBankDetails)
admin.site.register([EmployeeNote, EmployeeTag, PolicyMultipleFile, Policy, BonusPoint])
admin.site.register([DisciplinaryAction, Actiontype])


class EmployeeWorkInformationAdmin(SimpleHistoryAdmin):
    list_display = (
        "employee_id",
        "department_id",
        "job_position_id",
        "job_role_id",
        "reporting_manager_id",
        "shift_id",
        "work_type_id",
        "company_id",
    )
    search_fields = (
        "employee_id__employee_first_name",
        "employee_id__employee_last_name",
    )


class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "badge_id",
        "employee_first_name",
        "employee_last_name",
        "employee_user_id",
        "face_id_registered",
        "is_active",
    )

    search_fields = (
        "badge_id",
        "employee_user_id__username",
        "employee_first_name",
        "employee_last_name",
    )

    list_filter = ("is_active",)

    ordering = ("employee_first_name", "employee_last_name")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("face_profile")

    @admin.display(boolean=True, description="Face ID")
    def face_id_registered(self, obj):
        return hasattr(obj, "face_profile")

    def delete_view(self, request, object_id, extra_context=None):
        extra_context = extra_context or {}
        extra_context["custom_message"] = (
            "Are you sure you want to delete this item? This action cannot be undone."
        )
        return super().delete_view(request, object_id, extra_context=extra_context)


admin.site.register(Employee, EmployeeAdmin)
admin.site.register(EmployeeWorkInformation, EmployeeWorkInformationAdmin)


@admin.register(EmployeeFace)
class EmployeeFaceAdmin(admin.ModelAdmin):
    list_display = ("employee", "created_at", "updated_at")
    search_fields = (
        "employee__badge_id",
        "employee__employee_first_name",
        "employee__employee_last_name",
        "employee__employee_user_id__username",
    )
    readonly_fields = ("employee", "created_at", "updated_at")
    exclude = ("embedding",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
