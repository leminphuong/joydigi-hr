from django.apps import AppConfig


class JoydigiWidgetsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_widgets"

    def ready(self):
        from joydigi_widgets.widgets.file_widgets import patch_clearable_file_input

        patch_clearable_file_input()
