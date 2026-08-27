from django.contrib.auth import authenticate
from django.db.models import F
from django.utils.translation import gettext_lazy as _
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from ...api_serializers.auth.serializers import (
    GetEmployeeSerializer,
    PasswordResetSerializer,
)
from ..auth_tokens import mint_token_pair, resolve_refresh_subject


class LoginAPIView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if "username" and "password" in request.data.keys():
            username = request.data.get("username")
            password = request.data.get("password")
            user = authenticate(username=username, password=password)
            if user:
                # Phase AUTH-6B: single-device login — every successful
                # mobile login bumps session_version, which immediately
                # invalidates whatever access/refresh token pair any
                # *other* device was holding (checked by
                # SessionVersionJWTAuthentication and the new refresh
                # endpoint). The atomic `F()` update (same technique as
                # the AUTH-6A.2 force-logout mutation) avoids a
                # lost-update if two logins for the same user land at
                # the same time; `refresh_from_db` then reads back
                # whichever value actually won that race, so the
                # minted tokens always carry the true post-bump number
                # rather than a stale in-memory guess.
                type(user).objects.filter(pk=user.pk).update(
                    session_version=F("session_version") + 1
                )
                user.refresh_from_db(fields=["session_version"])
                access, refresh = mint_token_pair(user)
                employee = user.employee_get
                geo_fencing = False
                company_id = None
                try:
                    geo_fencing = employee.get_company().geo_fencing.start
                except:
                    pass
                try:
                    company_id = employee.get_company().id
                except:
                    pass
                result = {
                    "employee": GetEmployeeSerializer(employee).data,
                    "access": str(access),
                    "refresh": str(refresh),
                    "geo_fencing": geo_fencing,
                    "company_id": company_id,
                }
                return Response(result, status=200)
            else:
                return Response({"error": _("Invalid credentials")}, status=401)
        else:
            return Response({"error": _("Please provide Username and Password")})


class TokenRefreshAPIView(APIView):
    """
    Phase AUTH-6B: mints a fresh access/refresh pair from a still-valid
    refresh token, without requiring the user to log in again.

    Deliberately does its own `session_version`/`is_active` check
    (via `resolve_refresh_subject`) on top of the stock SimpleJWT
    signature/expiry verification that already happens inside
    `RefreshToken(raw_refresh)` — otherwise a refresh token minted
    before an admin's "Đăng xuất khỏi thiết bị", or before a newer
    login on another device, could silently outlive the access token
    it's supposed to be bound to.

    No rotation/blacklist bookkeeping: `rest_framework_simplejwt.
    token_blacklist` is not installed (see AUTH-6A.1's audit), and
    session_version — not any individual refresh token's identity —
    is the sole revocation authority. Every successful call still
    mints a brand-new refresh token (a "renewal", not single-use
    rotation) so a regularly-used session's effective lifetime keeps
    sliding forward; the previous refresh token remains cryptographically
    valid until its own natural expiry or the next session_version
    change, exactly like any other un-rotated token in this backend.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        raw_refresh = request.data.get("refresh")
        if not raw_refresh or not isinstance(raw_refresh, str):
            return Response(
                {"error": _("Refresh token is required.")}, status=400
            )

        try:
            refresh = RefreshToken(raw_refresh)
        except TokenError:
            return Response(
                {"error": _("Session has ended. Please log in again.")},
                status=401,
            )

        user = resolve_refresh_subject(refresh)
        if user is None:
            return Response(
                {"error": _("Session has ended. Please log in again.")},
                status=401,
            )

        access, new_refresh = mint_token_pair(user)
        return Response(
            {"access": str(access), "refresh": str(new_refresh)}, status=200
        )


class PasswordResetAPIView(APIView):
    """
    Allows an authenticated employee to change their own password.

    GET  — returns the fields required for the reset form.
    POST — verifies the old password and saves the new one.
    """

    permission_classes = [IsAuthenticated]

    def get(self, _request):
        return Response(
            {"fields": ["old_password", "new_password", "confirm_password"]},
            status=200,
        )

    def post(self, request):
        serializer = PasswordResetSerializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=400)

        user = request.user
        user.set_password(serializer.validated_data["new_password"])
        user.save()
        return Response({"message": _("Password updated successfully.")}, status=200)
