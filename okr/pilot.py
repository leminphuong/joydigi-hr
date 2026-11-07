"""OKR pilot gate: who may use OKR while it is being tried out.

Two settings (``joydigi/settings/base.py``, from the environment):

* ``OKR_ACCESS_MODE`` — ``all`` (default when the variable is not set):
  every active employee of a company, the behaviour before the gate existed.
  ``pilot``: only the employees in ``OKR_PILOT_EMPLOYEE_IDS``. Any other
  value, including an empty one, closes OKR to everyone, the listed
  employees included (fail closed), and is logged.
* ``OKR_PILOT_EMPLOYEE_IDS`` — HR employee ids, comma separated (a list
  also works in tests). Empty in ``pilot`` mode: nobody.

To close OKR to everyone in an emergency use ``OKR_ACCESS_MODE=pilot`` with
an empty ``OKR_PILOT_EMPLOYEE_IDS``. Never ``all``: that opens it to every
employee.

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
MODE_CLOSED = "closed"  # what an unrecognised OKR_ACCESS_MODE means; not a value to configure

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
    """``all``, ``pilot``, or ``closed`` for any other configured value."""
    raw = getattr(settings, "OKR_ACCESS_MODE", None)
    if raw is None:
        return MODE_ALL
    value = str(raw).strip().lower()
    if value in (MODE_ALL, MODE_PILOT):
        return value
    logger.warning("OKR_ACCESS_MODE=%r is neither 'all' nor 'pilot': OKR is closed to everyone.", raw)
    return MODE_CLOSED


def _configured_ids() -> frozenset:
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


def pilot_employee_ids() -> frozenset:
    """Employee ids that may use OKR now: the configured list in ``pilot``
    mode, nobody when closed."""
    return _configured_ids() if mode() == MODE_PILOT else frozenset()


def is_active() -> bool:
    """True when access is restricted (``pilot`` or closed)."""
    return mode() != MODE_ALL


def allows(employee_id) -> bool:
    """May this HR employee use OKR right now?"""
    current = mode()
    if current == MODE_ALL:
        return True
    if current != MODE_PILOT:
        return False
    try:
        return int(employee_id) in _configured_ids()
    except (TypeError, ValueError):
        return False
