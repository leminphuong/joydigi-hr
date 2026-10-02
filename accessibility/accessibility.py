"""
accessibility/accessibility.py
"""

from django.utils.translation import gettext_lazy as _

ACCESSBILITY_FEATURE = [
    ("employee_view", _("Default Employee View")),
    ("employee_detailed_view", _("Default Employee Detailed View")),
]

#: Features an operator may toggle for ONE employee at a time, via
#: `employee.views.profile_edit_access`.
#:
#: Phase ADMIN-P0-PERMISSION-HARDENING (C-3). That view took the feature name
#: straight from the query string and looked it up, so any string a
#: `DefaultAccessibility` row happened to exist for could be toggled — and
#: `accessibility.views.user_accessibility` creates those rows from unvalidated
#: POST data, so the set was not bounded by anything.
#:
#: `profile_edit` is listed although it is deliberately NOT in
#: `ACCESSBILITY_FEATURE`: the per-employee toggle is the only thing that drives
#: it (`employee/cbv/employees.py` builds the action URL with
#: `?feature=profile_edit`), while ACCESSBILITY_FEATURE is the set offered on
#: the bulk accessibility settings screen. Keeping them separate is what lets
#: this allowlist stay exactly as wide as the live UI.
TOGGLEABLE_FEATURES = tuple(name for name, _label in ACCESSBILITY_FEATURE) + (
    "profile_edit",
)
