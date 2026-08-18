from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class JoydigiTourConfig(AppConfig):
    """
    App config for the enterprise product-tour engine.

    Global tours (``company_id=None``) are visible to every tenant because
    ``JoydigiCompanyManager`` includes ``company_id__isnull=True`` rows, so no
    per-company seeding/signals are required.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_tour"
    verbose_name = _("Product Tours")

    def ready(self):
        # Register the "Product Tours" entry in the Settings sidebar menu.
        from joydigi_tour import sidebar  # noqa: F401

        return super().ready()
