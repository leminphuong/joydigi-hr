"""Short-lived, server-signed proof that an attendance-source check
already passed (Phase 6.1) — the TOCTOU fix for `POST
/attendance/verify-source/` followed later by a plain check-in/out.

The mobile app is never trusted to just claim `"method_verified":
true`; it must present this opaque, signed, single-use, employee-bound
token, which the server independently re-verifies (signature, expiry,
employee match, not-already-consumed) before honoring it.
"""

import secrets
import time

from django.core import signing
from django.core.cache import cache

PROOF_SALT = "joydigi-attendance-verification-proof"
PROOF_TTL = 120  # seconds the proof stays valid and consumable
_USED_PREFIX = "attendance-proof-used"


def issue_verification_proof(employee_id, method):
    """Mints a proof token bound to `employee_id` and the verified
    `method` label. Returns the opaque token string."""
    payload = {
        "v": 1,
        "employee_id": employee_id,
        "method": method,
        "nonce": secrets.token_urlsafe(16),
        "iat": time.time(),
    }
    return signing.dumps(payload, salt=PROOF_SALT, compress=True)


def consume_verification_proof(token, employee_id):
    """Verifies signature, expiry, employee binding, and single-use,
    then marks the proof consumed. Returns the verified method string
    on success, or `None` on any failure (missing, tampered, expired,
    wrong employee, or already used)."""
    if not token:
        return None
    try:
        payload = signing.loads(token, salt=PROOF_SALT, max_age=PROOF_TTL)
    except (signing.BadSignature, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    if payload.get("employee_id") != employee_id:
        return None
    nonce = payload.get("nonce")
    if not nonce:
        return None
    cache_key = f"{_USED_PREFIX}:{nonce}"
    if cache.get(cache_key):
        return None
    cache.set(cache_key, True, PROOF_TTL)
    return payload.get("method")
