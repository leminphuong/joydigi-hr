"""
joydigi_automations/filters.py
"""

from joydigi.filters import JoydigiFilterSet, django_filters
from joydigi_automations.models import MailAutomation


class AutomationFilter(JoydigiFilterSet):
    """
    AutomationFilter
    """

    search = django_filters.CharFilter(field_name="title", lookup_expr="icontains")

    class Meta:
        model = MailAutomation
        fields = "__all__"
