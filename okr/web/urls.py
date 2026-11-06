from django.urls import path, re_path

from okr.web import views

app_name = "okr-web"

urlpatterns = [
    path("", views.app_shell, name="app"),
    # Deep links (e.g. /okr/objectives/12) load the same page; the SPA routes.
    re_path(r"^(?P<route>[a-z0-9/-]+)/?$", views.app_shell, name="route"),
]
