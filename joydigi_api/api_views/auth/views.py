import logging

from django.contrib.auth import authenticate
from django.db.models import F
from django.utils.translation import gettext_lazy as _
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import ExpiredTokenError, TokenError
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from ...api_serializers.auth.serializers import (
    GetEmployeeSerializer,
    PasswordResetSerializer,
)
from ..auth_tokens import (
    REJECT_TOKEN_EXPIRED,
    REJECT_TOKEN_INVALID,
    classify_refresh_subject,
    mint_token_pair,
)
from ..refresh_diagnostics import record_rejection

logger = logging.getLogger(__name__)


def _log_refresh_rejected(reason, user_id=None, detail=None):
    """Phase AUTH-6G.2: one structured line per *rejected* refresh, plus
    (AUTH-6G.3) one bounded entry an admin page can read back.

    Failures only — a successful refresh is unremarkable and logging it
    would only add noise. Never touches the raw token: the string is
    not decoded, echoed, hashed or stored anywhere, and `user_id` is
    passed only when it came out of a token whose signature already
    verified.
    """
    detail = detail or {}
    token_version = detail.get("token_session_version")
    current_version = detail.get("current_session_version")

    logger.warning(
        "AUTH_REFRESH_REJECTED reason=%s user_id=%s status=401 "
        "token_session_version=%s current_session_version=%s",
        reason,
        user_id if user_id is not None else "-",
        token_version if token_version is not None else "-",
        current_version if current_version is not None else "-",
    )
    record_rejection(
        reason=reason,
        user_id=user_id,
        status=401,
        token_session_version=token_version,
        current_session_version=current_version,
    )


class LoginAPIView(APIView):
    # Phase AUTH-6H: an endpoint whose whole job is to *issue*
    # credentials must never require credentials to reach it.
    # `DEFAULT_AUTHENTICATION_CLASSES` applies `SessionVersionJWTAuthentication`
    # globally, and DRF authenticates during `dispatch()` — before the
    # view body — so a caller arriving with a stale `Authorization`
    # header would be refused 401 without ever being allowed to log in.
    # `AllowAny` does not help: it governs permission, which runs after
    # authentication has already raised.
    authentication_classes = ()
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

    # Phase AUTH-6H — the production bug, fixed at its root.
    #
    # This endpoint authenticates its caller by the refresh token in the
    # request body, and by nothing else. It must not also authenticate
    # an `Authorization` header, because the header a client holds when
    # it needs to refresh is, by definition, the expired access token —
    # requiring a valid one to obtain a new one is circular, and makes
    # refresh work only while it is not needed.
    #
    # `permission_classes = [AllowAny]` alone was not enough: DRF's
    # `dispatch()` runs `perform_authentication()` *before*
    # `check_permissions()`, so the global
    # `SessionVersionJWTAuthentication` raised 401 on the stale header
    # and this view's body never ran. That is why the AUTH-6G.3
    # rejection recorder — which lives inside `post()` — stayed silent
    # while production kept returning 401.
    #
    # Scoped deliberately to this view: every protected endpoint keeps
    # authenticating exactly as before.
    authentication_classes = ()
    permission_classes = [AllowAny]

    def post(self, request):
        raw_refresh = request.data.get("refresh")
        if not raw_refresh or not isinstance(raw_refresh, str):
            return Response(
                {"error": _("Refresh token is required.")}, status=400
            )

        # Phase AUTH-6G.2: the two 401 branches below are classified for
        # the server log only. The status code and the message are
        # deliberately identical in both cases — an unauthenticated
        # caller must not be able to tell a disabled account from a
        # revoked session from a bad signature.
        try:
            refresh = RefreshToken(raw_refresh)
        except ExpiredTokenError:
            _log_refresh_rejected(REJECT_TOKEN_EXPIRED)
            return Response(
                {"error": _("Session has ended. Please log in again.")},
                status=401,
            )
        except TokenError:
            # Signature/format/type failure. No user id is logged: the
            # token never verified, so anything inside it is untrusted
            # and must not be decoded just to name someone.
            _log_refresh_rejected(REJECT_TOKEN_INVALID)
            return Response(
                {"error": _("Session has ended. Please log in again.")},
                status=401,
            )

        user, reject_reason, reject_detail = classify_refresh_subject(refresh)
        if user is None:
            # Safe to name the subject here: the signature verified, so
            # the claim is the server's own.
            _log_refresh_rejected(
                reject_reason,
                reject_detail.get("user_id")
                or refresh.get(api_settings.USER_ID_CLAIM),
                reject_detail,
            )
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
