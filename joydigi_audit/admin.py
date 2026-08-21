"""
admin.py
"""

from django.contrib import admin

from joydigi_audit.models import (
    AuditTag,
    JoydigiAuditInfo,
    JoydigiAuditLog,
    UserActivityLog,
)

# Register your models here.

admin.site.register(AuditTag)


@admin.register(UserActivityLog)
class UserActivityLogAdmin(admin.ModelAdmin):
    list_display = (
        "actor_name",
        "role",
        "action",
        "status_code",
        "ip_address",
        "created_at",
    )
    list_filter = ("role", "method", "status_code", "created_at")
    search_fields = ("actor_name", "actor_email", "action", "resource", "path")
    readonly_fields = tuple(field.name for field in UserActivityLog._meta.fields)
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
