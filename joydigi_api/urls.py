from django.urls import include, path

urlpatterns = [
    # Only endpoints used by the retained check-in/HR scope are exposed.
    path("auth/", include("joydigi_api.api_urls.auth.urls")),
    path("base/", include("joydigi_api.api_urls.base.urls")),
    path("employee/", include("joydigi_api.api_urls.employee.urls")),
    path("notifications/", include("joydigi_api.api_urls.notifications.urls")),
    path("attendance/", include("joydigi_api.api_urls.attendance.urls")),
    path("leave/", include("joydigi_api.api_urls.leave.urls")),
]
