"""
This module contains the configuration for the 'base' app.
"""

from django.apps import AppConfig, apps
from django.conf import settings


class BaseConfig(AppConfig):
    """
    Configuration class for the 'base' app.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "base"

    def ready(self) -> None:
        from base import request_decisions, sidebar, signals  # noqa: F401

        # Wired here, from `base`, because the request models it observes live
        # in three different apps (base, attendance, leave) and `ready()` runs
        # after every model has been loaded. See the module docstring for why
        # the hook is the models' own `save()` rather than ~50 approval views.
        request_decisions.connect()

        super().ready()
        check_for_no_permissions_models()


def check_for_no_permissions_models():

    model_names = set()
    for model in apps.get_models():
        if getattr(model, "_no_permission_model", False):
            model_names.add(model._meta.model_name)

    settings.NO_PERMISSION_MODALS.extend(list(model_names))
