"""
views.py
"""

from datetime import datetime
from ipaddress import ip_address

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from base.roles import checkin_admin_required
from joydigi.http.response import JoydigiRedirect
from joydigi_audit.forms import (
    AuditModelConfigForm,
    AuditModelFieldsForm,
    field_choices_for,
)
from joydigi_audit.models import AuditModelConfig, UserActivityLog
from joydigi_audit.registry import DEFAULT_TRACKED_MODELS


def _audit_tracking_context():
    """Shared context for the audit-tracking section on Audit & History."""
    return {
        "audit_model_form": AuditModelConfigForm(),
        "audit_model_configs": AuditModelConfig.objects.all().order_by(
            "app_label", "model_name"
        ),
    }


@login_required
def audit_history_settings_view(request):
    """
    Merged "Audit & History" settings page grouping History Tags and Audit
    Tracking under a single header.
    """
    context = {}
    if request.user.has_perm("joydigi_audit.view_auditmodelconfig"):
        context.update(_audit_tracking_context())
    return render(request, "base/settings/audit_history.html", context)


@login_required
@checkin_admin_required
def user_activity_log_view(request):
    """Nhật ký tập trung của quản trị viên, trưởng nhóm và nhân viên."""

    logs = UserActivityLog.objects.select_related("user", "company")
    selected_company = request.session.get("selected_company")
    if selected_company and selected_company != "all":
        try:
            logs = logs.filter(company_id=int(selected_company))
        except (TypeError, ValueError):
            logs = logs.none()
    elif not request.user.is_superuser:
        allowed_company_ids = getattr(request, "all_my_company_ids", None)
        if allowed_company_ids is not None:
            logs = logs.filter(company_id__in=allowed_company_ids)

    scoped_logs = logs

    query = request.GET.get("q", "").strip()
    role = request.GET.get("role", "").strip()
    method = request.GET.get("method", "").strip().upper()
    date_value = request.GET.get("date", "").strip()

    if query:
        search_filter = (
            Q(actor_name__icontains=query)
            | Q(actor_email__icontains=query)
            | Q(action__icontains=query)
            | Q(resource__icontains=query)
            | Q(path__icontains=query)
        )
        try:
            ip_address(query)
            search_filter |= Q(ip_address=query)
        except ValueError:
            pass
        logs = logs.filter(search_filter)
    if role in dict(UserActivityLog.ROLE_CHOICES):
        logs = logs.filter(role=role)
    if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        logs = logs.filter(method=method)
    if date_value:
        try:
            selected_date = datetime.strptime(date_value, "%Y-%m-%d").date()
            logs = logs.filter(created_at__date=selected_date)
        except ValueError:
            date_value = ""

    today = timezone.localdate()
    today_logs = scoped_logs.filter(created_at__date=today)
    stats = {
        "today": today_logs.count(),
        "changes": today_logs.exclude(method="GET").count(),
        "failures": today_logs.filter(status_code__gte=400).count(),
    }
    page_obj = Paginator(logs, 30).get_page(request.GET.get("page"))
    query_without_page = request.GET.copy()
    query_without_page.pop("page", None)

    return render(
        request,
        "joydigi_audit/user_activity_log.html",
        {
            "page_obj": page_obj,
            "stats": stats,
            "query": query,
            "selected_role": role,
            "selected_method": method,
            "selected_date": date_value,
            "role_choices": UserActivityLog.ROLE_CHOICES,
            "query_without_page": query_without_page.urlencode(),
        },
    )


@login_required
@permission_required("joydigi_audit.view_auditmodelconfig")
def audit_model_settings(request):
    """
    Legacy standalone Audit Tracking settings page. Merged into Audit & History;
    redirect direct visits to the merged page.
    """
    return redirect("audit-history-view")


@login_required
@permission_required("joydigi_audit.change_auditmodelconfig")
@require_http_methods(["POST"])
def save_audit_models(request):
    """Persist the list of audit-tracked models."""

    selected = request.POST.getlist("model_paths")
    selected_pairs = []
    for path in selected:
        if "." not in path:
            continue
        app_label, model_name = path.split(".", 1)
        selected_pairs.append((app_label, model_name))

    # Built-in defaults are always tracked — they cannot be turned off here
    # so audit history never silently disappears for the core Employee models.
    default_set = set(DEFAULT_TRACKED_MODELS)
    selected_set = set(selected_pairs) | default_set
    existing = {(c.app_label, c.model_name): c for c in AuditModelConfig.objects.all()}

    # Remove configs that were unchecked, but never delete defaults.
    for key, cfg in existing.items():
        if key in selected_set or key in default_set:
            continue
        cfg.delete()

    # Create new configs for newly checked entries (and ensure defaults exist).
    for app_label, model_name in selected_set:
        if (app_label, model_name) not in existing:
            AuditModelConfig.objects.create(
                app_label=app_label,
                model_name=model_name,
                is_enabled=True,
                tracked_fields=[],
            )

    messages.success(request, _("Audit tracking configuration updated."))

    if request.headers.get("HX-Request"):
        return HttpResponse(
            status=200,
            headers={"HX-Redirect": reverse("audit-history-view")},
        )
    return JoydigiRedirect(request)


@login_required
@permission_required("joydigi_audit.change_auditmodelconfig")
def edit_audit_model_fields(request, pk):
    """Edit which fields of a single model are tracked."""

    try:
        config = AuditModelConfig.objects.get(pk=pk)
    except AuditModelConfig.DoesNotExist:
        return HttpResponseBadRequest("Audit configuration not found.")

    if request.method == "POST":
        form = AuditModelFieldsForm(
            request.POST,
            app_label=config.app_label,
            model_name=config.model_name,
        )
        if form.is_valid():
            config.tracked_fields = form.cleaned_data["fields_to_track"]
            config.save()
            messages.success(
                request,
                _("Tracked fields updated for %(model)s.")
                % {"model": config.model_name},
            )
            if request.headers.get("HX-Request"):
                return HttpResponse(
                    status=200,
                    headers={"HX-Redirect": reverse("audit-history-view")},
                )
            return JoydigiRedirect(request)
    else:
        form = AuditModelFieldsForm(
            initial={"fields_to_track": config.tracked_fields or []},
            app_label=config.app_label,
            model_name=config.model_name,
        )

    return render(
        request,
        "joydigi_audit/audit_model_fields_form.html",
        {"form": form, "config": config},
    )
