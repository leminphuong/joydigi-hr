"""
App configuration for the Joydigi Automations app.
Initializes model choices and starts automation when the server runs.
"""

import os
import sys

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class JoydigiAutomationConfig(AppConfig):
    """Configuration class for the Joydigi Automations Django app."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "joydigi_automations"
    verbose_name = _("Automations")

    def ready(self) -> None:
        ready = super().ready()
        if any(
            cmd in sys.argv
            for cmd in [
                "makemigrations",
                "migrate",
                "compilemessages",
                "flush",
                "shell",
            ]
        ):
            return ready
        try:

            from base.templatetags.joydigifilters import app_installed
            from employee.models import Employee
            from joydigi_automations.methods.methods import get_related_models
            from joydigi_automations.models import MODEL_CHOICES

            recruitment_installed = False
            if app_installed("recruitment"):
                recruitment_installed = True

            models = [Employee]
            if recruitment_installed:
                from recruitment.models import Candidate

                models.append(Candidate)

            main_models = models
            for main_model in main_models:
                related_models = get_related_models(main_model)

                for model in related_models:
                    path = f"{model.__module__}.{model.__name__}"
                    MODEL_CHOICES.append((path, model.__name__))
            MODEL_CHOICES.append(("employee.models.Employee", "Employee"))
            MODEL_CHOICES.append(
                ("pms.models.EmployeeKeyResult", "Employee Key Results")
            )

            MODEL_CHOICES = list(set(MODEL_CHOICES))
            try:
                from joydigi_automations.signals import start_automation

                start_automation()
            except Exception as e:
                print(e)
                """
                Migrations are not affected yet
                """
        except:
            """
            Models not ready yet
            """
        return ready
