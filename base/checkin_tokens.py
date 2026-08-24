"""Kiosk QR / 6-digit check-in sessions (Phase 6.1 redesign).

The old design (see git history) signed only a 30-second time bucket
under a single global salt — the token carried no company or location
identity at all, and `kiosk`/`kiosk-qr`/`kiosk-data` were public,
unauthenticated endpoints. Anyone with network access could mint a
currently-valid QR/code for *any* company from anywhere, with no
physical-presence requirement and no way for the server to tell which
office it was even claiming to represent.

This redesign makes the kiosk display a **server-managed session**:
`create_kiosk_session()` is only reachable by an authenticated
checkin-leader/admin (see `base/checkin_portal.py`) who has already
picked a specific `CheckInLocation`. The QR token and the 6-digit code
are both opaque, unguessable lookup keys into a short-lived cache
record — neither one *encodes* the company/location itself, so a
tampered or guessed token/code has nothing to decode into; it just
fails the cache lookup. This also gives real single-use/expiry
semantics for free (cache TTL), which the old purely-stateless design
could never provide.
"""

import secrets
import time

from django.core import signing
from django.core.cache import cache

KIOSK_TOKEN_SALT = "joydigi-checkin-kiosk-v2"
KIOSK_SESSION_TTL = 90  # seconds a minted QR/code stays valid
_SESSION_PREFIX = "kiosk-session"
_CODE_PREFIX = "kiosk-code"


def create_kiosk_session(company_id, location_id):
    """Mint a new kiosk session bound to (company_id, location_id).

    Returns `{"token": str, "code": "123456", "ttl": int}`. Callers
    must already have authorized the request for this specific
    location before calling this — this function performs no
    authorization itself.
    """
    nonce = secrets.token_urlsafe(24)
    code = f"{secrets.randbelow(1_000_000):06d}"
    session = {
        "company_id": company_id,
        "location_id": location_id,
        "issued_at": time.time(),
    }
    cache.set(f"{_SESSION_PREFIX}:{nonce}", session, KIOSK_SESSION_TTL)
    cache.set(f"{_CODE_PREFIX}:{code}", nonce, KIOSK_SESSION_TTL)
    token = signing.dumps({"v": 1, "n": nonce}, salt=KIOSK_TOKEN_SALT, compress=True)
    return {"token": token, "code": code, "ttl": KIOSK_SESSION_TTL}


def _session_for_nonce(nonce):
    if not nonce:
        return None
    return cache.get(f"{_SESSION_PREFIX}:{nonce}")


def resolve_kiosk_token(token):
    """Verify a QR token's signature/shape, then look up its still-live
    session. Returns `{"company_id", "location_id", "issued_at"}` or
    `None` if the token is missing, tampered, malformed, or its
    session has expired/was never issued."""
    if not token:
        return None
    try:
        payload = signing.loads(
            token, salt=KIOSK_TOKEN_SALT, max_age=KIOSK_SESSION_TTL
        )
    except (signing.BadSignature, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    return _session_for_nonce(payload.get("n"))


def resolve_kiosk_code(code):
    """Look up a 6-digit code's still-live session. Returns the same
    shape as `resolve_kiosk_token`, or `None`. Pure lookup — callers
    are responsible for throttling attempts before calling this."""
    if not code:
        return None
    nonce = cache.get(f"{_CODE_PREFIX}:{code}")
    return _session_for_nonce(nonce)
