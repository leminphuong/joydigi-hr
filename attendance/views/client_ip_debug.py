"""Phase 3B.1 — TEMPORARY production client-IP diagnostic.

=====================================================================
 TEMPORARY. DELETE AFTER THE PHASE 3B MEASUREMENT IS CAPTURED.
=====================================================================

Phase 3B established that a company with Allowed IPs enabled rejects
**every** mobile attendance, because the attendance request shim's
`META.get()` always returns its default and the client IP therefore
arrives as `None`. Fixing that means choosing which value to trust, and
that choice depends on the proxy chain in front of production —
Cloudflare, then the VPS's nginx, then gunicorn — which cannot be
proven from the repository and which nobody here can inspect directly.

So this page answers exactly one question, from inside the running
process: *which of the four candidate values does Django actually
receive on production, and what is in them?* Nothing more.

Deliberately narrow, and it must stay that way:

* four fixed keys, named in `_FIELDS` below. This is an allow-list, not
  a filter — a header added to the request later cannot appear here
  without somebody editing this file;
* never the Authorization header, a cookie, a session key, a CSRF
  token, the request body, or any other part of `META`;
* read-only. No query, no write, no side effect;
* GET only;
* the values go to the authenticated caller in the response and are
  never written to the application log, because an access log line is
  a far wider audience than one administrator's browser tab.

Phase GLOBAL-ADMIN-PERF-A extends the same page with a second, equally
narrow measurement, rather than standing up a parallel diagnostic: the
admin UI answers some requests in about twenty seconds, and the browser
says almost all of it is time-to-first-byte. Every heavy admin page
shares one middleware chain, one set of context processors and one
generic list view, so the question is which shared dependency is slow —
not which page.

This endpoint is unusually well suited to answering it, for a reason
worth stating: it renders no template, runs no context processor and
touches no business queryset. It is the same middleware chain and
nothing else. So if opening *this* page is also slow while the numbers
it reports are fast, the time is being spent before or after the view —
in middleware, session, activity logging, or the proxy — and not in the
page's own work. If this page is fast while the admin pages are slow,
the opposite holds. That comparison is the measurement; the individual
numbers are supporting evidence.

Still an allow-list, and still only integers and booleans: backend and
vendor names, elapsed milliseconds, row counts. Never a URL, a host, a
port, a credential, a setting's value, an environment variable, or an
exception's message — only an exception's class name.

This must never become a general header-inspection endpoint. When the
production values have been captured, delete this module, its route in
`attendance/urls.py`, and `attendance/tests/test_client_ip_debug.py`.

The permission is the one that already guards the Allowed IP screen
these values explain (`attendance.add_attendance`), so this page is
visible to exactly the administrators who can already configure the
feature being diagnosed — it widens nobody's access.
"""

import time

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from joydigi.decorators import login_required, permission_required

#: response key -> META key. The only four values this page can report.
_FIELDS = (
    ("remote_addr", "REMOTE_ADDR"),
    ("x_forwarded_for", "HTTP_X_FORWARDED_FOR"),
    ("x_real_ip", "HTTP_X_REAL_IP"),
    ("cf_connecting_ip", "HTTP_CF_CONNECTING_IP"),
)


#: Cache backends whose operations fail fast on their own: everything
#: they touch is in this process or on this disk, so a probe cannot hang
#: waiting on a network that is not there.
_LOCALLY_BOUNDED_BACKENDS = (
    "LocMemCache",
    "DummyCache",
    "FileBasedCache",
    "DatabaseCache",
)

#: A network cache is probed only when its own configuration bounds how
#: long an operation may take. django-redis maps both of these onto the
#: underlying socket; without them a single `cache.get` waits on the
#: operating system's TCP timeout — which is the very shape being
#: investigated here. Probing in that state would not measure the
#: problem, it would reproduce it, inside an administrator's browser tab.
_REQUIRED_TIMEOUT_OPTIONS = ("SOCKET_CONNECT_TIMEOUT", "SOCKET_TIMEOUT")

#: The one key this page may touch. Namespaced so it cannot collide with
#: anything the application stores, and always deleted afterwards.
_PROBE_KEY = "joydigi:phase-perf-a:probe"


def _ms(started):
    """Milliseconds since `started`."""
    return round((time.perf_counter() - started) * 1000, 2)


def _cache_config():
    """The default cache's backend name, without any of its settings.

    Only the last component of the dotted path is returned — "RedisCache",
    not the module path and certainly not `LOCATION`, which carries the
    host and, for a password-protected Redis, the password.
    """
    config = (getattr(settings, "CACHES", {}) or {}).get("default", {}) or {}
    dotted = config.get("BACKEND") or ""
    return config, (dotted.rsplit(".", 1)[-1] if dotted else None)


def _cache_probe_is_bounded(config, backend_name):
    """Whether a cache operation here is guaranteed to end quickly.

    Answered from configuration alone. No connection is opened to decide
    it, because opening one is precisely what this gate exists to guard.
    """
    if not backend_name:
        return False
    if backend_name in _LOCALLY_BOUNDED_BACKENDS:
        return True
    options = config.get("OPTIONS") or {}
    return all(options.get(name) for name in _REQUIRED_TIMEOUT_OPTIONS)


def _probe_cache():
    """Time one set / get / delete on a key of this page's own.

    Application keys are never read, nothing existing is overwritten, and
    `cache.clear()` is never called. The key is deleted on the way out
    whatever happened, so a failed probe leaves nothing behind.
    """
    config, backend_name = _cache_config()
    result = {
        "configured": bool(backend_name),
        "backend": backend_name,
        "probe_safe": _cache_probe_is_bounded(config, backend_name),
        "set_ms": None,
        "get_ms": None,
        "delete_ms": None,
        "ok": False,
        "error_type": None,
    }
    if not result["probe_safe"]:
        return result

    try:
        started = time.perf_counter()
        cache.set(_PROBE_KEY, 1, 30)
        result["set_ms"] = _ms(started)

        started = time.perf_counter()
        cache.get(_PROBE_KEY)
        result["get_ms"] = _ms(started)

        started = time.perf_counter()
        cache.delete(_PROBE_KEY)
        result["delete_ms"] = _ms(started)

        result["ok"] = True
    except Exception as error:
        # The class name only. A connection error's message routinely
        # carries the host, the port, and sometimes the credential.
        result["error_type"] = type(error).__name__
        try:
            cache.delete(_PROBE_KEY)
        except Exception:
            pass
    return result


def _probe_database():
    """One read-only round trip, to separate latency from query cost."""
    result = {
        "vendor": connection.vendor,
        "select_1_ms": None,
        "ok": False,
        "error_type": None,
    }
    try:
        started = time.perf_counter()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        result["select_1_ms"] = _ms(started)
        result["ok"] = True
    except Exception as error:
        result["error_type"] = type(error).__name__
    return result


def _table_counts():
    """Row counts for the four tables the shared admin path reads.

    `_base_manager` on purpose: the question is how big the table is, not
    what one administrator may see. The company-scoped default manager
    would answer a different question and add a DISTINCT that distorts
    the timing. Integers only — never a row, an id, a name, a date, or
    anything identifying an employee or a company.

    Each count is timed too, because a count that is itself slow is the
    finding rather than a detail: it says the table has outgrown the
    indexes the shared admin queries rely on.
    """
    from attendance.models import Attendance
    from joydigi_audit.models import UserActivityLog
    from leave.models import LeaveRequest
    from notifications.models import Notification

    targets = (
        ("attendance_count", Attendance),
        ("leave_request_count", LeaveRequest),
        ("notification_count", Notification),
        ("activity_log_count", UserActivityLog),
    )
    counts = {}
    timings = {}
    for name, model in targets:
        try:
            started = time.perf_counter()
            counts[name] = model._base_manager.count()
            timings[f"{name}_ms"] = _ms(started)
        except Exception:
            counts[name] = None
            timings[f"{name}_ms"] = None
    return counts, timings


def _performance_section():
    """Everything this page measures, as one allow-listed block."""
    counts, timings = _table_counts()
    return {
        "cache": _probe_cache(),
        "database": _probe_database(),
        "tables": counts,
        "tables_ms": timings,
        "runtime": {
            "debug": bool(settings.DEBUG),
            # Django is not told how many workers serve it, and this page
            # will not go looking: reading /proc, listing processes or
            # shelling out all exceed what a diagnostic may do. Worker
            # count is an infrastructure question, answered elsewhere.
            "gunicorn_workers": None,
        },
    }


@login_required
@permission_required("attendance.add_attendance")
@require_http_methods(["GET"])
def client_ip_debug_view(request):
    """Report the four candidate client-IP values and the shared-path
    measurements, and nothing else.

    `present` is reported separately from the value so that "the header
    never arrived" and "the header arrived empty" stay distinguishable —
    they imply different things about the proxy chain.
    """
    started = time.perf_counter()

    payload = {}
    for name, meta_key in _FIELDS:
        raw = request.META.get(meta_key)
        payload[f"{name}_present"] = raw is not None
        payload[name] = raw

    payload["performance"] = _performance_section()
    # Measured last so it covers everything above it. Compared against
    # what the browser reports for this same request, the gap between the
    # two is time spent outside the view.
    payload["diagnostic_total_ms"] = _ms(started)
    return JsonResponse(payload)
