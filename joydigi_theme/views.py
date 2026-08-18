from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from base.models import Company
from joydigi_theme.models import CompanyTheme, JoydigiColorTheme
from joydigi_views.cbv_methods import login_required, permission_required


@method_decorator(
    permission_required("joydigi_theme.view_joydigicolortheme"),
    name="dispatch",
)
class ThemeView(LoginRequiredMixin, TemplateView):
    """
    Displays the theme management interface for authenticated users.
    """

    template_name = "theme/theme_view.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["themes"] = JoydigiColorTheme.objects.all()
        context["active_theme"] = self._get_active_theme()

        # Get the global default theme (for login page) - this is what all companies should see
        default_theme = JoydigiColorTheme.get_default_theme()
        context["default_theme"] = default_theme

        return context

    def _get_active_theme(self):
        """Get the active theme for the current company"""
        selected_company = self.request.session.get("selected_company")
        active_company = (
            Company.objects.get(id=selected_company)
            if selected_company != "all"
            else None
        )
        return CompanyTheme.get_theme_for_company(active_company)


@method_decorator(
    permission_required("joydigi_theme.change_joydigicolortheme"),
    name="dispatch",
)
class ChangeThemeView(LoginRequiredMixin, View):
    """
    View to change the company theme via HTMX.
    """

    def post(self, request, *args, **kwargs):
        theme_id = request.POST.get("theme_id")
        is_default = request.POST.get("is_default") == "on"

        if not theme_id:
            return self._error_response(request, _("Theme ID is required"), 400)

        selected_company = self.request.session.get("selected_company")
        active_company = (
            Company.objects.get(id=selected_company)
            if selected_company != "all"
            else None
        )
        if selected_company == "all":
            messages.error(request, _("No active company found"))
            return self._render_themes(request)
        try:
            theme = JoydigiColorTheme.objects.get(pk=theme_id)
            self._update_company_theme(active_company, theme, is_default)

            if is_default:
                messages.success(
                    request,
                    _("Theme changed successfully and set as default for login page"),
                )
            else:
                messages.success(request, _("Theme changed successfully"))

            return self._render_themes(request, theme)

        except JoydigiColorTheme.DoesNotExist:
            return self._error_response(request, _("Theme not found"), 404)
        except Exception as e:
            return self._error_response(
                request, _("An error occurred while changing the theme"), 500
            )

    def _update_company_theme(self, company, theme, is_default=False):
        """Update or create the company theme."""
        with transaction.atomic():
            company_theme, created = CompanyTheme.objects.update_or_create(
                company=company, defaults={"theme": theme}
            )

            # If setting as default, set it on the theme itself
            if is_default:
                theme.is_default = True
                theme.save()  # This will automatically unset other defaults

    def _render_themes(self, request, active_theme=None, status=200):
        """Render the theme cards HTML."""
        if active_theme is None:
            active_company = getattr(request, "active_company", None)
            if active_company:
                active_theme = CompanyTheme.get_theme_for_company(active_company)
            else:
                active_theme = CompanyTheme.get_default_theme()

        themes = JoydigiColorTheme.objects.all()
        active_company = getattr(request, "active_company", None)

        # Get current company theme to check if it's default
        current_company_theme = None
        if active_company:
            current_company_theme = CompanyTheme.objects.filter(
                company=active_company
            ).first()

        # Get the global default theme (for login page) - this is what all companies should see
        default_theme = JoydigiColorTheme.get_default_theme()

        html = render_to_string(
            "theme/theme_cards.html",
            {
                "themes": themes,
                "active_theme": active_theme,
                "current_company_theme": current_company_theme,
                "default_theme": default_theme,
                "request": request,
            },
        )
        return HttpResponse(html, status=status)

    def _error_response(self, request, message, status):
        """Generate an error response with appropriate message and status."""
        messages.error(request, message)
        return self._render_themes(request, status=status)


@method_decorator(
    permission_required("joydigi_theme.add_joydigicolortheme"),
    name="dispatch",
)
class SetDefaultThemeView(LoginRequiredMixin, View):
    """
    View to set/unset a theme as default for login page via HTMX.
    """

    def post(self, request, *args, **kwargs):
        theme_id = request.POST.get("theme_id")

        if not theme_id:
            return self._error_response(request, _("Theme ID is required"), 400)

        # active_company = getattr(request, "active_company", None)
        # company_id = request.session.get("selected_company")
        # return self._error_response(request, _("No active company found"), 400)

        try:
            theme = JoydigiColorTheme.objects.get(pk=theme_id)

            # Check if this theme is already set as global default
            is_currently_default = theme.is_default

            if is_currently_default:
                # Unset as default (toggle off)
                theme.is_default = False
                theme.save()
                messages.success(request, _("Theme removed as default for login page"))
            else:
                # Set as default - this will automatically reset any existing default
                theme.is_default = True
                theme.save()  # The save() method will handle unsetting other defaults
                messages.success(request, _("Theme set as default for login page"))

            return self._render_themes(request)

        except Exception as e:
            pass

    def _render_themes(self, request, status=200):
        """Render the theme cards HTML."""
        active_company = getattr(request, "active_company", None)
        active_theme = None
        current_company_theme = None

        if active_company:
            active_theme = CompanyTheme.get_theme_for_company(active_company)
            current_company_theme = CompanyTheme.objects.filter(
                company=active_company
            ).first()
        else:
            active_theme = CompanyTheme.get_default_theme()

        themes = JoydigiColorTheme.objects.all()

        # Get the global default theme (for login page) - this is what all companies should see
        default_theme = JoydigiColorTheme.get_default_theme()

        html = render_to_string(
            "theme/theme_cards.html",
            {
                "themes": themes,
                "active_theme": active_theme,
                "current_company_theme": current_company_theme,
                "default_theme": default_theme,
                "request": request,
            },
        )
        return HttpResponse(html, status=status)

    def _error_response(self, request, message, status):
        """Generate an error response with appropriate message and status."""
        messages.error(request, message)
        return self._render_themes(request, status=status)
