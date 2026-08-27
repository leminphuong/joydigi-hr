"""
Authentication utilities for the API
"""

from rest_framework import authentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication


class SessionVersionJWTAuthentication(JWTAuthentication):
    """
    Phase AUTH-6A.2: adds one check on top of the stock SimpleJWT
    validation so an admin's "Đăng xuất khỏi thiết bị" action can
    actually revoke an already-issued mobile access token before it
    naturally expires (audited in AUTH-6A.1: this backend has no
    refresh endpoint, no blacklist app, and no other revocation seam).

    Does NOT reimplement token validation — `super().get_user()`
    already does a real DB lookup for every authenticated request (see
    the audit), so comparing `session_version` on that same fetched
    row costs zero extra queries. All existing SimpleJWT behavior
    (`CHECK_USER_IS_ACTIVE`, user-not-found, etc.) is preserved
    unchanged because it happens inside that same `super()` call.

    Legacy-token compatibility (AUTH-6A.1 §C): a token minted before
    this phase carries no `session_version` claim at all. Such a token
    is treated as if it claimed version 0 — since every existing
    `JoydigiUser.session_version` also defaults to 0, a plain deploy of
    this migration never logs anyone out. An admin's force-logout
    (`session_version += 1`) still immediately revokes that legacy
    token on its next use, because 0 no longer matches the bumped
    value.

    The rejection message here is intentionally generic/internal —
    Flutter never renders it (see `error_mapper.dart`'s 401 handling,
    which always shows a fixed friendly Vietnamese string instead of
    any backend-provided `detail` text, exactly to prevent messages
    like this — or SimpleJWT's own raw ones — from ever reaching a
    user).
    """

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        token_version = validated_token.get("session_version", 0)
        if token_version != user.session_version:
            raise AuthenticationFailed(
                "Session has been ended.", code="session_revoked"
            )
        return user


class SwaggerAuthentication(authentication.BaseAuthentication):
    """
    Custom authentication class for Swagger UI
    """

    def authenticate(self, request):
        # Get the authentication header
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")

        # Try JWT authentication first
        if auth_header.startswith("Bearer "):
            jwt_auth = JWTAuthentication()
            try:
                return jwt_auth.authenticate(request)
            except:
                pass

        # Fall back to session authentication
        if request.user and request.user.is_authenticated:
            return (request.user, None)

        return None


class RejectBasicAuthentication(authentication.BaseAuthentication):
    """
    Explicitly reject HTTP Basic Auth across the API with a clear error message.
    """

    def authenticate(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Basic "):
            raise AuthenticationFailed(
                "Basic authentication is disabled. Use Bearer token (JWT) in the Authorization header."
            )
        return None
