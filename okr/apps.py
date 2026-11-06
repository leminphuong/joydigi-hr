from django.apps import AppConfig


class OkrConfig(AppConfig):
    """OKR module (Objectives, Key Results, check-ins, approvals).

    Ported from the JOYOKR WordPress theme v1.6.45. The module owns only
    ``okr_*`` tables; it reads Company, Department and Employee from the HR
    apps and never changes their schema.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "okr"
    verbose_name = "OKR"

    def ready(self):
        """Mount ``/api/okr/`` and ``/okr/`` without editing the project's URL files.

        Same approach as ``joydigi_api``: the project ends with a catch-all
        named ``custom-404``, so the route goes in directly before it.
        """
        from django.urls import include, path

        from joydigi.urls import urlpatterns

        if any(getattr(pattern, "app_name", None) == "okr-api" for pattern in urlpatterns):
            return
        catchall_index = next(
            (i for i, pattern in enumerate(urlpatterns) if getattr(pattern, "name", None) == "custom-404"),
            len(urlpatterns),
        )
        # The web app (JOYOKR's interface) and its API.
        urlpatterns.insert(catchall_index, path("okr/", include("okr.web.urls")))
        urlpatterns.insert(catchall_index, path("api/okr/", include("okr.api.urls")))
