"""Server-side throttling for the 6-digit kiosk fallback code (Phase 6.1).

The old design had no throttling anywhere in the codebase for this —
a 6-digit code is only 1,000,000 possibilities, so an unthrottled
validation endpoint is a realistic brute-force target. This uses
Django's cache (Redis in production, LocMem in dev/tests) purely as a
per-employee attempt counter — no new model/migration needed.
"""

from django.core.cache import cache

NUMERIC_CODE_MAX_ATTEMPTS = 8
NUMERIC_CODE_WINDOW_SECONDS = 5 * 60
_ATTEMPTS_PREFIX = "kiosk-code-attempts"


def check_and_record_numeric_code_attempt(user) -> bool:
    """Records one 6-digit-code validation attempt for `user` and
    returns `True` if that user is currently throttled (has already
    made `NUMERIC_CODE_MAX_ATTEMPTS` attempts within the trailing
    `NUMERIC_CODE_WINDOW_SECONDS`), `False` if the attempt may proceed.

    Every call increments the counter, including throttled ones, so a
    client can't dodge the limit by only checking first.
    """
    user_id = getattr(user, "id", None) or getattr(user, "pk", None)
    key = f"{_ATTEMPTS_PREFIX}:{user_id}"
    try:
        count = cache.incr(key)
    except ValueError:
        # Key doesn't exist yet (first attempt in this window).
        cache.set(key, 1, NUMERIC_CODE_WINDOW_SECONDS)
        count = 1
    return count > NUMERIC_CODE_MAX_ATTEMPTS
