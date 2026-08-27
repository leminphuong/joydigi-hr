"""
Phase AUTH-6B: shared token-minting/validation helpers for the mobile
login + refresh endpoints.

Deliberately kept separate from `joydigi_api/auth.py`'s
`SessionVersionJWTAuthentication` (AUTH-6A.2, already working in
production) — that class is not touched this phase.
"""

from django.contrib.auth import get_user_model
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken


def mint_token_pair(user):
    """
    Returns `(access, refresh)` Token objects, both carrying the
    user's *current* `session_version` claim.

    `RefreshToken.access_token` is a property — it mints a *new*
    `AccessToken` on every access, so it must be captured into a local
    variable exactly once (the same footgun AUTH-6A.2's login-view fix
    already documented) rather than read/stringified twice.
    """
    refresh = RefreshToken.for_user(user)
    refresh["session_version"] = user.session_version
    access = refresh.access_token
    access["session_version"] = user.session_version
    return access, refresh


def resolve_refresh_subject(refresh):
    """
    Given an already signature/expiry/type-validated `RefreshToken`
    (verification happens inside `RefreshToken(raw_token)` itself),
    resolves and returns the `JoydigiUser` it still authorizes — or
    `None` if the refresh is no longer usable: user not found,
    inactive, or its `session_version` claim no longer matches the
    user's current value (revoked by an admin's "Đăng xuất khỏi thiết
    bị", or superseded by a newer login on another device under
    AUTH-6B's single-device rule).

    Stock SimpleJWT (`TokenRefreshSerializer`/`JWTAuthentication`) only
    checks a token's signature and expiry — this is the extra check
    that actually extends AUTH-6A.2/6B's revocation model onto the
    refresh path too, not just the access-token path.
    """
    User = get_user_model()
    try:
        user_id = refresh[api_settings.USER_ID_CLAIM]
    except KeyError:
        return None
    try:
        user = User.objects.get(**{api_settings.USER_ID_FIELD: user_id})
    except User.DoesNotExist:
        return None
    if not user.is_active:
        return None
    token_version = refresh.get("session_version", 0)
    if token_version != user.session_version:
        return None
    return user
