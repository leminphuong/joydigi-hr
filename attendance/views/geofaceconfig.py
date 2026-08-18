from django.shortcuts import redirect

from joydigi.decorators import login_required


@login_required
def geofaceconfig(request):
    """
    The standalone Geo & Face config page has been merged into the "Attendance
    Rule" settings page; direct access now redirects there.
    """
    return redirect("attendance-rule-view")
