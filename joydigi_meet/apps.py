from django.apps import AppConfig
from django.conf import settings


class JoydigiMeetConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_meet"
    verbose_name = "Meet"

    def ready(self):
        from django.urls import include, path

        from joydigi.urls import urlpatterns
        from joydigi_meet import signals

        settings.APPS.append("joydigi_meet")

        urlpatterns.append(
            path("meet/", include("joydigi_meet.urls")),
        )
        super().ready()
