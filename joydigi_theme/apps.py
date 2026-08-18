"""
AppConfig for the joydigi_theme app
"""

from django.apps import AppConfig
from django.conf import settings
from django.utils.translation import gettext_lazy as _


class JoydigiThemeConfig(AppConfig):
    """App configuration class for joydigi_theme."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_theme"
    verbose_name = _("Appearance")

    def ready(self):
        """Run app initialization logic (executed after Django setup).
        Used to auto-register URLs and connect signals if required.
        """
        try:
            # Auto-register this app's URLs and add to installed apps
            from django.urls import include, path

            from joydigi.urls import urlpatterns

            settings.APPS.append(("joydigi_theme"))
            # Add app URLs to main urlpatterns
            urlpatterns.append(
                path("theme/", include("joydigi_theme.urls")),
            )

            __import__("joydigi_theme.signals")
        except Exception as e:
            import logging

            logging.warning("JoydigiThemeConfig.ready failed: %s", e)

        super().ready()
