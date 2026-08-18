from django.conf import settings
from django.urls import include, path
from drf_yasg import openapi
from drf_yasg.views import get_schema_view
from rest_framework import permissions

from joydigi_api.schema import OrderedTagSchemaGenerator

# Create schema view for Swagger and ReDoc
schema_view = get_schema_view(
    openapi.Info(
        title="Joydigi API",
        default_version="v1",
        description="API documentation for Joydigi HRMS. Click the 'Authorize' button at the top to authenticate.",
        terms_of_service="https://www.joydigi.com/terms/",
        contact=openapi.Contact(email="support@joydigi.com"),
        license=openapi.License(name="BSD License"),
    ),
    public=True,
    permission_classes=(permissions.AllowAny,),
    generator_class=OrderedTagSchemaGenerator,
)

urlpatterns = [
    # API Documentation URLs
    path(
        "swagger<format>/", schema_view.without_ui(cache_timeout=0), name="schema-json"
    ),
    path(
        "swagger/",
        schema_view.with_ui("swagger", cache_timeout=0),
        name="schema-swagger-ui",
    ),
    path("redoc/", schema_view.with_ui("redoc", cache_timeout=0), name="schema-redoc"),
    path("docs/", schema_view.with_ui("swagger", cache_timeout=0), name="schema-docs"),
    # API Endpoints (static configuration)
    path("auth/", include("joydigi_api.api_urls.auth.urls")),
    path("asset/", include("joydigi_api.api_urls.asset.urls")),
    path("base/", include("joydigi_api.api_urls.base.urls")),
    path("employee/", include("joydigi_api.api_urls.employee.urls")),
    path("notifications/", include("joydigi_api.api_urls.notifications.urls")),
    path("payroll/", include("joydigi_api.api_urls.payroll.urls")),
    path("attendance/", include("joydigi_api.api_urls.attendance.urls")),
    path("leave/", include("joydigi_api.api_urls.leave.urls")),
    path("helpdesk/", include("joydigi_api.api_urls.helpdesk.urls")),
    path("project/", include("joydigi_api.api_urls.project.urls")),
    path("onboarding/", include("joydigi_api.api_urls.onboarding.urls")),
    path("offboarding/", include("joydigi_api.api_urls.offboarding.urls")),
    path("recruitment/", include("joydigi_api.api_urls.recruitment.urls")),
    path("pms/", include("joydigi_api.api_urls.pms.urls")),
]
