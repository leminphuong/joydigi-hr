from django.apps import AppConfig
from django.conf import settings
import os
import sys


class PgGitBackupConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pg_backup"

    def ready(self):
        if not getattr(settings, "ENABLE_DB_BACKUP", True):
            return super().ready()
        ignored_commands = {"test", "migrate", "makemigrations", "shell", "collectstatic"}
        if any(command in sys.argv for command in ignored_commands):
            return super().ready()
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return super().ready()
        from pg_backup import scheduler

        scheduler.start()

        return super().ready()
