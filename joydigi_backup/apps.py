from django.apps import AppConfig


class BackupConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_backup"

    def ready(self):
        from django.urls import include, path

        from joydigi.urls import urlpatterns

        urlpatterns.append(
            path("backup/", include("joydigi_backup.urls")),
        )
        super().ready()
