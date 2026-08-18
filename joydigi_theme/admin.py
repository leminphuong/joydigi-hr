"""
Admin registration for the joydigi_theme app
"""

from django.contrib import admin

from joydigi_theme.models import CompanyTheme, JoydigiColorTheme

# Register your joydigi_theme models here.
admin.site.register(JoydigiColorTheme)
admin.site.register(CompanyTheme)
