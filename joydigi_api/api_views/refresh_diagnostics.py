"""Phase AUTH-6G.3 — TEMPORARY store for recent refresh rejections.

AUTH-6G.2 named the reason a refresh is refused, but only in the server
log — which the operator cannot read (no SSH). This module keeps the
last few rejections somewhere an admin page can show them.

### Why a file and not process memory
Production runs `gunicorn --workers 3` and declares no `CACHES`, so
Django falls back to `LocMemCache` — per-process. A rejection handled by
one worker would be invisible to an admin page served by either of the
other two: roughly a one-in-three chance of seeing any given event,
which is useless for reproducing a bug once. A small file on the same
host is shared by all three workers.

Changing `CACHES` to a shared backend was rejected deliberately: the
attendance verification-proof single-use check (`attendance.methods.
verification_proof`) uses the same cache, so switching backends would
change attendance behaviour — out of scope, and not a change to make
while chasing an auth bug.

### What is stored
Integers, a reason constant, and a timestamp. Never a token, never a
JWT payload, never a header. Bounded to the most recent
`MAX_EVENTS` entries.

Every operation is best-effort: a diagnostic must never be able to fail
an employee's login, so all I/O is wrapped and errors are swallowed.

Delete this module, its admin section and its tests once the production
rejection is explained.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from django.conf import settings

logger = logging.getLogger(__name__)

#: Keep the tail short — this exists to inspect one reproduction, not to
#: be an audit trail.
MAX_EVENTS = 50

#: Trim only once the file has grown a bit, so the common path stays a
#: plain append rather than a rewrite.
TRIM_THRESHOLD = 200

FILENAME = "auth_refresh_diagnostic.jsonl"

#: Only these keys may ever be written. An allow-list, so a caller
#: cannot accidentally hand this module a token to persist.
ALLOWED_FIELDS = (
    "timestamp",
    "reason",
    "user_id",
    "status",
    "token_session_version",
    "current_session_version",
)


def _path():
    return os.path.join(str(settings.BASE_DIR), FILENAME)


def record_rejection(
    reason,
    user_id=None,
    status=401,
    token_session_version=None,
    current_session_version=None,
):
    """Appends one rejection. Never raises.

    `user_id` is expected to be `None` unless the caller resolved it
    from a token whose signature already verified — this module does not
    and cannot check that, so the responsibility sits with the view.
    """
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "user_id": user_id,
        "status": status,
        "token_session_version": token_session_version,
        "current_session_version": current_session_version,
    }
    # Belt and braces: even if a caller passes something unexpected,
    # only the allow-listed keys are serialised.
    event = {k: v for k, v in event.items() if k in ALLOWED_FIELDS}

    try:
        path = _path()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        _trim_if_needed(path)
    except Exception:  # pragma: no cover - diagnostics never break auth
        logger.exception("AUTH_REFRESH_DIAGNOSTIC write failed")


def _trim_if_needed(path):
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        if len(lines) <= TRIM_THRESHOLD:
            return
        kept = lines[-MAX_EVENTS:]
        # Write via a temp file in the same directory, then replace, so a
        # concurrent reader never sees a half-written file.
        directory = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(kept)
        os.replace(tmp, path)
    except Exception:  # pragma: no cover
        logger.exception("AUTH_REFRESH_DIAGNOSTIC trim failed")


def recent_rejections(limit=MAX_EVENTS, user_id=None):
    """Newest first. Returns `[]` when there is nothing (or on any
    error) — the admin page must render either way.

    `user_id` filters to events whose subject was verified; rejections
    with no trusted subject (an expired or malformed token) carry
    `user_id=None` and are always included, because those are exactly
    the cases the filter would otherwise hide.
    """
    try:
        path = _path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()[-TRIM_THRESHOLD:]
    except Exception:  # pragma: no cover
        logger.exception("AUTH_REFRESH_DIAGNOSTIC read failed")
        return []

    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if user_id is not None and event.get("user_id") not in (None, user_id):
            continue
        events.append({k: event.get(k) for k in ALLOWED_FIELDS})

    events.reverse()
    return events[:limit]
