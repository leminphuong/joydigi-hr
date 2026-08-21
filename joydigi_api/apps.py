from django.apps import AppConfig
from django.conf import settings


class JoydigiApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_api"

    def ready(self):
        """
        Register the internal API used by check-in and the retained HR screens.
        """
        # Import here to avoid circular imports
        from django.urls import include, path

        from joydigi.urls import urlpatterns

        # `joydigi.urls.urlpatterns` contains a catch-all
        # (`re_path(r"^.*$", custom404, name="custom-404")`, see
        # joydigi/urls.py) that matches every path Django hasn't
        # already matched. Appending here used to add "api/" *after*
        # that catch-all, so it could never be reached: every request
        # under /api/ (including /api/auth/login/) silently resolved
        # to `custom404` instead — and since `custom404` is an
        # ordinary (non `csrf_exempt`) Django view, requests through it
        # were also subject to Django's CSRF checks, which is why even
        # `LoginAPIView` (itself correctly `csrf_exempt`) never
        # actually ran.
        #
        # Other apps' `ready()` hooks append their own routes the same
        # way and may run before this one, so the catch-all is not
        # reliably the *last* element by the time this runs (`-1`
        # isn't safe) — locate it by name instead and insert directly
        # before it.
        catchall_index = next(
            (
                i
                for i, pattern in enumerate(urlpatterns)
                if getattr(pattern, "name", None) == "custom-404"
            ),
            len(urlpatterns),
        )
        urlpatterns.insert(catchall_index, path("api/", include("joydigi_api.urls")))

        super().ready()
