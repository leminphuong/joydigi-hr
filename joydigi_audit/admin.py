"""
admin.py
"""

from django.contrib import admin

from joydigi_audit.models import AuditTag, JoydigiAuditInfo, JoydigiAuditLog

# Register your models here.

admin.site.register(AuditTag)
