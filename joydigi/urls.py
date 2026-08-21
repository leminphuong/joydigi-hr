"""joydigi URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.conf.urls.static import static
from django.contrib import admin
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path, re_path
from django.views.i18n import JavaScriptCatalog

import notifications.urls
from base.views import custom404

from . import settings


handler404 = "base.views.custom404"


def health_check(request):
    """Liveness probe — cheap, no dependency checks (Docker HEALTHCHECK)."""
    return JsonResponse({"status": "ok"}, status=200)


def readiness_check(request):
    """
    Readiness probe — verifies database (and Redis cache when REDIS_URL is set).
    """
    checks = {}
    try:
        connection.ensure_connection()
        checks["database"] = "ok"
    except Exception as exc:
        return JsonResponse(
            {"status": "unavailable", "database": str(exc)},
            status=503,
        )

    if getattr(settings, "REDIS_URL", None):
        try:
            cache.set("joydigi_ready_probe", "1", timeout=5)
            if cache.get("joydigi_ready_probe") != "1":
                raise RuntimeError("cache readback failed")
            checks["cache"] = "ok"
        except Exception as exc:
            return JsonResponse(
                {"status": "unavailable", "cache": str(exc), **checks},
                status=503,
            )

    return JsonResponse({"status": "ok", **checks}, status=200)


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("base.urls")),
    path("", include("joydigi_views.urls")),
    path("", include("joydigi_audit.urls")),
    path("employee/", include("employee.urls")),
    # LeaveConfig registers these dynamically after this module has loaded.
    # Keep the retained leave/request workflow reachable before the custom
    # catch-all 404 route.
    path("leave/", include("leave.urls")),
    # Register attendance routes before the custom 404 fallback.  The
    # AttendanceConfig also registers these routes dynamically, but that runs
    # after this module is loaded; routes appended after the catch-all pattern
    # can never be reached.
    path("attendance/", include("attendance.urls")),
    path("joydigi-widget/", include("joydigi_widgets.urls")),
    re_path(
        "^inbox/notifications/", include(notifications.urls, namespace="notifications")
    ),
    path("i18n/", include("django.conf.urls.i18n")),
    path("jsi18n/", JavaScriptCatalog.as_view(), name="javascript-catalog"),
    path("health/", health_check),
    path("ready/", readiness_check),
    # Luôn đặt cuối cùng: mọi đường dẫn chưa được khai báo dùng trang 404 JoyDigi,
    # kể cả môi trường cục bộ đang bật DEBUG=True.
    re_path(r"^.*$", custom404, name="custom-404"),
]

# if settings.DEBUG:
#     urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
