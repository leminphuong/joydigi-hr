"""OKR pilot gate: who may use OKR while it is being tried out.

Two settings (``joydigi/settings/base.py``, from the environment):

* ``OKR_ACCESS_MODE`` — ``all`` (default): every active employee of a
  company, the behaviour before the gate existed. ``pilot``: only the
  employees in ``OKR_PILOT_EMPLOYEE_IDS``. Any other value closes the gate
  (fail closed) and is logged.
* ``OKR_PILOT_EMPLOYEE_IDS`` — HR employee ids, comma separated (a list
  also works in tests). Empty in ``pilot`` mode: nobody.

The gate is checked on every request through ``OKRViewer.for_user`` (the
REST API, the /okr/ web and the HR menu entry), so removing an id takes
effect on the next request once the service has reloaded its settings.
Being an HR superuser or holding an OKR role grants nothing here: the gate
only looks at the employee id. It never grants a role either.
"""

from __future__ import annotations

import logging

from django.conf import settings

MODE_ALL = "all"
MODE_PILOT = "pilot"

logger = logging.getLogger(__name__)

NOT_ENABLED_CODE = "okr_not_enabled"
NOT_ENABLED_MESSAGE = (
    "Mục tiêu & OKR đang chạy thử nghiệm và chưa mở cho tài khoản này. Vui lòng liên hệ quản trị viên OKR."
)
NOT_IN_PILOT = "Nhân viên này chưa có trong danh sách Pilot OKR nên không dùng được OKR."
OWNER_NOT_IN_PILOT = (
    "Nhân viên này chưa tham gia thử nghiệm OKR nên sẽ không thấy OKR được giao. "
    "Chỉ giao cho người trong danh sách Pilot."
)


def mode() -> str:
    value = str(getattr(settings, "OKR_ACCESS_MODE", MODE_ALL) or MODE_ALL).strip().lower()
    if value not in (MODE_ALL, MODE_PILOT):
        logger.warning("OKR_ACCESS_MODE=%r is neither 'all' nor 'pilot': OKR is closed to everyone.", value)
        return MODE_PILOT
    return value


def pilot_employee_ids() -> frozenset:
    raw = getattr(settings, "OKR_PILOT_EMPLOYEE_IDS", "") or ""
    parts = raw.replace(";", ",").split(",") if isinstance(raw, str) else list(raw)
    ids = set()
    for part in parts:
        text = str(part).strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            logger.warning("OKR_PILOT_EMPLOYEE_IDS has a value that is not an employee id: %r (ignored).", text)
            continue
        if value > 0:
            ids.add(value)
    return frozenset(ids)


def is_active() -> bool:
    """True when the pilot restriction applies."""
    return mode() == MODE_PILOT


def allows(employee_id) -> bool:
    """May this HR employee use OKR right now?"""
    if not is_active():
        return True
    try:
        return int(employee_id) in pilot_employee_ids()
    except (TypeError, ValueError):
        return False
