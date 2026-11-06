from django import template

from okr.services.access import OKRViewer
from okr.services.errors import OKRError

register = template.Library()


@register.filter
def okr_access(user) -> bool:
    """True when the user is an active employee of a company, i.e. may open
    the OKR web (used to show its entry in the HR menu)."""
    try:
        return OKRViewer.for_user(user).is_active
    except OKRError:
        return False
