from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class JoydigiAuthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_auth"
    verbose_name = _("Joydigi Auth")
