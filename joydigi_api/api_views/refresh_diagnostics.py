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

#: Phase AUTH-6G.3A: a synthetic reason the admin page can write on
#: demand, to prove the write->read->display chain works on production
#: without waiting an hour for a real rejection. Deliberately defined
#: here and NOT in `auth_tokens`' classifier: the refresh endpoint can
#: never produce it, and no client can ever receive it.
REASON_DIAGNOSTIC_TEST = "DIAGNOSTIC_TEST"

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


# ---------------------------------------------------------------------
# Phase AUTH-6G.3A — why is the store empty?
#
# AUTH-6G.5 reproduced a real production rejection, yet the admin page
# showed nothing. The write path swallows every error by design (a
# diagnostic must never fail an employee's login), and the operator has
# no SSH to read the traceback — so the failure is invisible.
#
# These helpers surface the file's runtime state on the page itself.
# Everything is read-only: writability is probed with `os.access`, never
# by creating a file.
# ---------------------------------------------------------------------


def file_status():
    """Runtime facts about the store. Never raises."""
    status = {
        "path": None,
        "parent_exists": False,
        "parent_writable": False,
        "file_exists": False,
        "file_readable": False,
        "file_writable": False,
        "file_size_bytes": None,
        "last_modified": None,
        "events_read_count": 0,
        "error": None,
    }
    try:
        path = _path()
        status["path"] = path
        parent = os.path.dirname(path)
        status["parent_exists"] = os.path.isdir(parent)
        status["parent_writable"] = os.access(parent, os.W_OK)
        status["file_exists"] = os.path.exists(path)
        if status["file_exists"]:
            status["file_readable"] = os.access(path, os.R_OK)
            status["file_writable"] = os.access(path, os.W_OK)
            status["file_size_bytes"] = os.path.getsize(path)
            status["last_modified"] = datetime.fromtimestamp(
                os.path.getmtime(path), timezone.utc
            ).isoformat()
        status["events_read_count"] = len(recent_rejections(limit=MAX_EVENTS))
    except Exception as error:  # pragma: no cover - status must always render
        status["error"] = type(error).__name__
        logger.exception("AUTH_REFRESH_DIAGNOSTIC status failed")
    return status


def record_test_event():
    """Writes one clearly-marked synthetic event and reports whether the
    write actually landed.

    Returns `(ok, detail)`. Unlike `record_rejection`, this one *does*
    tell the caller what went wrong — the whole point is to make a silent
    write failure visible to the admin who pressed the button.
    """
    before = file_status()
    try:
        path = _path()
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "reason": REASON_DIAGNOSTIC_TEST,
                        "user_id": None,
                        "status": 0,
                        "token_session_version": None,
                        "current_session_version": None,
                    }
                )
                + "\n"
            )
    except Exception as error:
        logger.exception("AUTH_REFRESH_DIAGNOSTIC test write failed")
        return False, f"{type(error).__name__}: {error}"

    after = file_status()
    if after["events_read_count"] > before["events_read_count"]:
        return True, None
    return False, "write reported success but the event did not read back"
