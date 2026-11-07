"""The JOYOKR web app served by Django at ``/okr/``.

The page is JOYOKR v1.6.45's single-page shell. Its JavaScript talks to
``/api/okr/`` through the HR web login session (CSRF on writes), so there
is no second login and no token in browser storage.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.middleware.csrf import get_token
from django.shortcuts import render

from okr import pilot
from okr.services import organization
from okr.services.access import OKRViewer
from okr.services.errors import OKRError

# The SPA's own routes (JOYOKR ``app.js``); anything else under /okr/ is a 404.
ROUTES = re.compile(
    r"^(dashboard|my-okrs|company-okrs|tree|teams|check-ins|reports|notifications"
    r"|objectives/\d+|key-results/\d+"
    r"|admin/(approvals|roles|departments|teams|cycles|settings))?$"
)

# Strict policy for the OKR page: scripts only from this site, nothing
# inline, no plugins, no framing by other sites. Inline style attributes stay
# allowed because the progress bars set their width that way.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static" / "okr_web"


def asset_version() -> str:
    """Short fingerprint of the OKR web's CSS and JS, appended as ``?v=``.

    HR stores static files without hashed names (CompressedStaticFilesStorage),
    so without it a browser can keep running an old ``forms.js`` next to a new
    ``views.js`` after an update."""
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.suffix in (".js", ".css") and path.is_file():
            stat = path.stat()
            digest.update(f"{path.relative_to(STATIC_DIR).as_posix()}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()[:12]


def _config(request, viewer: OKRViewer, route: str) -> dict:
    capabilities = [f"joyokr_{name}" for name in viewer.capabilities()]
    roles = ["administrator"] if viewer.is_admin else ["joyokr_manager"] if viewer.is_manager else ["joyokr_employee"]
    return {
        "root": "/api/okr/",
        "appUrl": "/okr/",
        "hrUrl": "/",
        "logoutUrl": "/logout/",
        "initialRoute": route or "dashboard",
        "loggedIn": True,
        "csrfToken": get_token(request),
        "currentUser": {
            "id": viewer.employee_id,
            "display_name": viewer.employee.get_full_name(),
            "roles": roles,
            "capabilities": capabilities,
        },
        "capabilities": capabilities,
        # The company's at-risk threshold (OKRSettings), for statuses the
        # browser works out itself; JOYOKR's web assumed 15.
        "warningThreshold": float(organization.get_settings(viewer.company_id).warning_threshold),
        "strings": {
            "loading": "Đang tải dữ liệu…",
            "error": "Đã có lỗi xảy ra. Vui lòng thử lại.",
            "confirm": "Xác nhận",
            "cancel": "Hủy",
            "saved": "Đã lưu thay đổi.",
            "empty": "Chưa có dữ liệu.",
        },
    }


# By URL name, as HR's own decorator does: settings.LOGIN_URL ("/login", no
# trailing slash) lands on HR's custom 404 page.
@login_required(login_url="login")
def app_shell(request, route: str = ""):
    route = route.strip("/")
    if not ROUTES.match(route):
        raise Http404
    reason = None
    try:
        viewer = OKRViewer.for_user(request.user)
    except OKRError as error:
        viewer = None
        if error.code == pilot.NOT_ENABLED_CODE:
            reason = error.message
    if viewer is None or not viewer.is_active:
        response = render(request, "okr_web/access_denied.html", {"reason": reason}, status=403)
    else:
        caps = set(viewer.capabilities())
        response = render(
            request,
            "okr_web/shell.html",
            {
                "config": _config(request, viewer, route),
                "asset_version": asset_version(),
                "viewer_name": viewer.employee.get_full_name(),
                "can_approve": "approve" in caps,
                "can_manage_organization": "manage_organization" in caps,
                "can_manage_cycles": "manage_cycles" in caps,
                "can_manage_settings": "manage_settings" in caps,
            },
        )
    response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
