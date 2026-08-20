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

        # Add API URLs to main project urlpatterns
        urlpatterns.append(
            path("api/", include("joydigi_api.urls")),
        )

        super().ready()
