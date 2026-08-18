from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class JoydigiDoumentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_documents"
    verbose_name = _("Documents")
