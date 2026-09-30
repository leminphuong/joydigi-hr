"""Internal, machine-to-machine routes. No user credential opens these.

Phase AUTO-OFFICE-PUBLIC-IP-UPDATER. Kept in their own `internal/` namespace
so that "which endpoints does the mobile app reach?" stays answerable by
reading the other url modules, and so nothing here can be mistaken for part
of the app's surface.
"""

from django.urls import path

from joydigi_api.api_views.attendance.office_ip_views import UpdateOfficeIPView

urlpatterns = [
    path(
        "attendance/update-office-ip/",
        UpdateOfficeIPView.as_view(),
        name="api-internal-update-office-ip",
    ),
]
