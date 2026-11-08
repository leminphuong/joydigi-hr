from django.apps import AppConfig


class AccessibilityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accessibility"

    def ready(self) -> None:
        from accessibility import signals
        from joydigi.urls import include, path, urlpatterns

        # joydigi.urls ends with a catch-all (`custom-404`) that matches every
        # path. Appending after it (as this hook used to) made the settings
        # page's "Accessibility" links answer 404 in production. Insert just
        # before the catch-all, found by name as joydigi_api/apps.py does,
        # since other ready() hooks may already have added routes after it.
        catchall_index = next(
            (
                index
                for index, pattern in enumerate(urlpatterns)
                if getattr(pattern, "name", None) == "custom-404"
            ),
            len(urlpatterns),
        )
        urlpatterns.insert(catchall_index, path("", include("accessibility.urls")))
        return super().ready()
