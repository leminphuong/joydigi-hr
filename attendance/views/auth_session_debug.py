"""Phase AUTH-6G.2 — TEMPORARY read-only auth/session diagnostic.

AUTH-6G.1 reproduced the production symptom end to end on a real
device: an employee logged in at 22:07:18, Android reclaimed the app in
the background, and on reopening at 23:17:41 the refresh endpoint
answered **401** for a refresh token only 70 minutes 30 seconds old —
one that the repository's settings say should live for 30 days. The
Flutter client behaved correctly throughout (one refresh attempt,
classified `RefreshRevoked`, session cleared only after that
conclusive answer), so the cause is server-side.

It cannot be narrowed further from outside, because every rejection
reason returns the same 401 with the same message — deliberately, so an
unauthenticated caller cannot probe which accounts exist or are
disabled. This page provides the missing view from *inside*, for an
authorised administrator, without weakening that.

Two questions it answers:

1. **Is production's effective `SIMPLE_JWT` what the repository says?**
   Read from `django.conf.settings` at runtime — never by parsing
   source, which is exactly the assumption that made the timezone bug
   take five phases to find. `joydigi/settings/__init__.py` imports
   `local_settings` after `base`, so a file that is not in the
   repository can silently override these values on the server.

2. **Is a specific employee's session revoked?** `session_version` and
   `is_active` are the only two account-side reasons a valid, unexpired
   refresh token is refused.

Strictly read-only. No secret, no key, no token, no environment dump,
and nothing that accepts a token as input. Delete this module, its
template and its URL once the cause is known.
"""

import importlib.util
from pathlib import Path

from django.conf import settings
from django.shortcuts import render

from employee.models import Employee
from joydigi.decorators import login_required, permission_required
from joydigi_api.api_views.refresh_diagnostics import recent_rejections

#: Only these keys are ever shown. `SIGNING_KEY`, `VERIFYING_KEY` and
#: anything else in `SIMPLE_JWT` stay unread — an allow-list, so a new
#: secret added to that dict later cannot leak by default.
JWT_SAFE_KEYS = (
    "ACCESS_TOKEN_LIFETIME",
    "REFRESH_TOKEN_LIFETIME",
    "ROTATE_REFRESH_TOKENS",
    "BLACKLIST_AFTER_ROTATION",
    "ALGORITHM",
    "AUTH_HEADER_TYPES",
    "USER_ID_FIELD",
    "USER_ID_CLAIM",
)

#: What the repository expects, so a mismatch is visible at a glance
#: rather than requiring the reader to remember the intended values.
EXPECTED = {
    "ACCESS_TOKEN_LIFETIME": "1:00:00",
    "REFRESH_TOKEN_LIFETIME": "30 days, 0:00:00",
}


def _effective_jwt():
    """The values the *running process* is actually using.

    Falls back to SimpleJWT's own defaults for keys the project does not
    set, since those defaults are what would apply — showing "not set"
    would hide, for example, a one-day refresh lifetime inherited
    because the project's `SIMPLE_JWT` block never reached this process.
    """
    from rest_framework_simplejwt.settings import (
        api_settings as jwt_api_settings,
    )

    configured = getattr(settings, "SIMPLE_JWT", {}) or {}
    rows = []
    for key in JWT_SAFE_KEYS:
        effective = getattr(jwt_api_settings, key, None)
        rows.append(
            {
                "key": key,
                "value": str(effective),
                "explicit": key in configured,
                "expected": EXPECTED.get(key),
                "mismatch": (
                    key in EXPECTED and str(effective) != EXPECTED[key]
                ),
            }
        )
    return rows


def _settings_origin():
    """Whether an out-of-repository override file can reach settings.

    Reports only booleans and the module path. The contents of
    `local_settings.py` are never read, shown, or logged.
    """
    module = settings.SETTINGS_MODULE
    package_dir = Path(__file__).resolve().parents[2] / "joydigi" / "settings"
    local_path = package_dir / "local_settings.py"

    return {
        "settings_module": module,
        "import_supported": True,  # joydigi/settings/__init__.py imports it
        "file_exists": local_path.exists(),
        "time_zone": settings.TIME_ZONE,
        "use_tz": settings.USE_TZ,
    }


def _employee_row(employee):
    """Only the fields that can explain a refused refresh."""
    user = employee.employee_user_id
    return {
        "employee_id": employee.id,
        "badge_id": employee.badge_id,
        "name": str(employee),
        "user_id": getattr(user, "id", None),
        "is_active": getattr(user, "is_active", None),
        "session_version": getattr(user, "session_version", None),
        "has_user": user is not None,
    }


@login_required
@permission_required("employee.view_employee")
def auth_session_debug_view(request):
    """`Employee.objects` is a `JoydigiCompanyManager`, so the lookup
    below stays inside whatever company scope this admin session already
    has — the page never widens visibility."""
    lookup = (request.GET.get("employee_id") or "").strip()

    employee_row = None
    lookup_error = None
    if lookup:
        try:
            employee = Employee.objects.select_related(
                "employee_user_id"
            ).get(pk=int(lookup))
        except (ValueError, TypeError):
            lookup_error = "Mã nhân viên phải là số."
        except Employee.DoesNotExist:
            # Same answer whether the employee does not exist or is
            # outside this admin's company scope.
            lookup_error = "Không tìm thấy nhân viên trong phạm vi của bạn."
        else:
            employee_row = _employee_row(employee)

    # Phase AUTH-6G.3: when an employee is being inspected, narrow to
    # their verified rejections — but rejections with no trusted subject
    # (expired/malformed token) are kept, since hiding those would hide
    # the very cases the filter cannot attribute.
    filter_user_id = employee_row["user_id"] if employee_row else None

    return render(
        request,
        "attendance/auth_debug/auth_session_debug.html",
        {
            "jwt_rows": _effective_jwt(),
            "origin": _settings_origin(),
            "employee_row": employee_row,
            "lookup": lookup,
            "lookup_error": lookup_error,
            "rejections": recent_rejections(user_id=filter_user_id),
            "rejections_filtered": filter_user_id is not None,
        },
    )
