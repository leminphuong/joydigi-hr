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

    Phase AUTH-6G.2: kept as the single-answer form used by callers that
    only need yes/no; the reasoning lives in `classify_refresh_subject`
    so the two can never disagree.
    """
    user, _reason, _detail = classify_refresh_subject(refresh)
    return user


# ---------------------------------------------------------------------
# Phase AUTH-6G.2 — refresh-rejection classification.
#
# Every way a refresh can fail currently collapses into the same 401
# with the same message, which is deliberate (never leak to an
# unauthenticated caller whether an account exists, is disabled, or was
# revoked). The cost is that a production rejection is undiagnosable
# from outside: AUTH-6G.1 caught a refresh token only 70 minutes old
# being refused, and nothing in the response says whether that was
# expiry, a `session_version` bump, or a disabled account.
#
# These constants name the reason *internally only* — for the server
# log and the admin diagnostic. The HTTP response is untouched.
# ---------------------------------------------------------------------

#: The refresh token's own `exp` had passed.
REJECT_TOKEN_EXPIRED = "TOKEN_EXPIRED"
#: Signature/format/type failure — not a statement about any account.
REJECT_TOKEN_INVALID = "TOKEN_INVALID"
#: Verified token, but it carries no user id claim.
REJECT_USER_NOT_FOUND_CLAIM = "USER_NOT_FOUND"
#: Verified token whose user row no longer exists.
REJECT_USER_NOT_FOUND = "USER_NOT_FOUND"
#: The account is disabled.
REJECT_USER_INACTIVE = "USER_INACTIVE"
#: Admin force logout, or a newer login on another device.
REJECT_SESSION_REVOKED = "SESSION_VERSION_MISMATCH"


def classify_refresh_subject(refresh):
    """Same decision as `resolve_refresh_subject`, but says *why*.

    Returns `(user, reason, detail)`: on success `(user, None, {})`; on
    failure `(None, <REJECT_* constant>, {...})`. The two functions must
    always agree — `resolve_refresh_subject` is a thin wrapper over this
    one, so they cannot drift apart.

    Phase AUTH-6G.3: `detail` carries the two integers that make a
    revocation actionable — the version the token was minted with and
    the one the account holds now. Integers only; the token itself is
    never included.
    """
    User = get_user_model()
    try:
        user_id = refresh[api_settings.USER_ID_CLAIM]
    except KeyError:
        return None, REJECT_USER_NOT_FOUND_CLAIM, {}
    try:
        user = User.objects.get(**{api_settings.USER_ID_FIELD: user_id})
    except User.DoesNotExist:
        return None, REJECT_USER_NOT_FOUND, {"user_id": user_id}
    if not user.is_active:
        return None, REJECT_USER_INACTIVE, {"user_id": user.id}
    token_version = refresh.get("session_version", 0)
    if token_version != user.session_version:
        return (
            None,
            REJECT_SESSION_REVOKED,
            {
                "user_id": user.id,
                "token_session_version": token_version,
                "current_session_version": user.session_version,
            },
        )
    return user, None, {}
