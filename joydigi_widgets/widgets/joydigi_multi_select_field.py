"""
joydigi_multi_select_field.py
This module is used to write cutom multiple select field
"""

from django import forms


class JoydigiMultiSelectField(forms.ModelMultipleChoiceField):
    """
    JoydigiMultiSelectField
    """
