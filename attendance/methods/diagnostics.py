"""
Phase 6.3A.3 — TEMPORARY clock-out diagnostic instrumentation.

This module exists ONLY to identify the real PostgreSQL exception behind
the production `DataError` on `POST /api/attendance/clock-out/`. It must
be removed (along with every `set_stage(...)` call site and the
`CLOCK_OUT_DATA_ERROR` handling in `ClockOutAPIView`) once the root cause
is confirmed and fixed. Do not build on top of this for anything else.

`_stage` is a `ContextVar` rather than a module global so it is safe under
concurrent requests (each request gets its own copy), including under
async/greenlet workers where a module global would leak between requests.
"""

import re
from contextvars import ContextVar

_stage: ContextVar[str] = ContextVar("clock_out_diagnostic_stage", default="unknown")

_REDACT_PATTERNS = [
    re.compile(r"postgresql://[^\s]+", re.IGNORECASE),
    re.compile(r"password\s*=\s*\S+", re.IGNORECASE),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"Authorization\S*\s*:\s*\S+", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-.]+"),  # JWT-shaped tokens
    re.compile(r"/[\w./-]*\.(py|env)\b"),  # source/env file paths
]


def set_stage(name):
    """Record the current checkout stage for this request/task context."""
    _stage.set(name)


def get_stage():
    return _stage.get()


def reset_stage():
    _stage.set("unknown")


def sanitize_db_message(raw_message, max_length=200):
    """
    Best-effort redaction of a raw DB exception message before it is ever
    returned to a client. Keeps only the first line (Postgres appends
    DETAIL/HINT/CONTEXT lines that can echo back bind values or SQL), then
    strips anything matching a known-sensitive pattern, then truncates.
    """
    if not raw_message:
        return ""
    first_line = str(raw_message).strip().splitlines()[0]
    for pattern in _REDACT_PATTERNS:
        first_line = pattern.sub("[redacted]", first_line)
    if len(first_line) > max_length:
        first_line = first_line[:max_length] + "…"
    return first_line
