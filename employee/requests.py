"""
employee/requests.py

Requests landing page with tabbed shift, inbox, and work type sections.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def requests_view(request):
    """
    Requests landing page with tabbed shift, inbox, and work type sections.
    """
    return render(request, "requests/requests.html")


@login_required
def requests_shift_request_tab(request):
    """
    HTMX tab body for shift requests under requests.
    """
    return render(request, "requests/requests_shift_request_tab.html")


@login_required
def requests_shift_inbox_tab(request):
    """
    HTMX tab body for shift inbox (allocated shifts) under requests.
    """
    return render(request, "requests/requests_shift_inbox_tab.html")


@login_required
def requests_work_type_tab(request):
    """
    HTMX tab body for work type requests under requests.
    """
    return render(request, "requests/requests_work_type_tab.html")
