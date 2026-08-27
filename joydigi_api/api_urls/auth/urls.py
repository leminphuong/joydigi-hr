from django.urls import path

from ...api_views.auth.views import (
    LoginAPIView,
    PasswordResetAPIView,
    TokenRefreshAPIView,
)

urlpatterns = [
    path("login/", LoginAPIView.as_view()),
    path("reset-password/", PasswordResetAPIView.as_view(), name="api-reset-password"),
    path(
        "token/refresh/",
        TokenRefreshAPIView.as_view(),
        name="api-token-refresh",
    ),
]
