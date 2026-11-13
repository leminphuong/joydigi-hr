from functools import wraps

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from base.models import MultipleApprovalManagers
from employee.models import EmployeeWorkInformation
from joydigi.joydigi_middlewares import _thread_locals
from joydigi_views.cbv_methods import decorator_with_arguments


# -- object-level access (security fix 2026-11-13) -----------------------------
#
# `ManagerPermission` below is only a coarse gate: it lets through anybody who
# manages *someone*. On its own it let any manager act on every employee of
# every company, and approve their own requests. Every view that acts on one
# employee's record must also ask `can_manage_employee` for that employee —
# the same rule the hardened web paths use (attendance/views/requests.py
# `_can_review_attendance_request`, leave/views.py approve, leave/decorators).


from base.access import actor_of, can_manage_employee, is_direct_manager  # noqa: E402,F401


def employee_from_data(request, key="employee_id"):
    """The Employee named by `key` in the request body, or None."""
    from employee.models import Employee

    data = request.data
    raw = data.get(key) if hasattr(data, "get") else None
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if hasattr(raw, "pk"):
        raw = raw.pk
    return Employee.objects.filter(pk=raw).first() if str(raw or "").strip().isdigit() else None


def forbidden(message=None):
    return Response(
        {"error": message or _("You do not have permission to perform this action.")},
        status=status.HTTP_403_FORBIDDEN,
    )


class ManagerPermission(BasePermission):
    leave_perm = [
        "leave.view_leaverequest",
        "leave.change_leaverequest",
        "leave.delete_leaverequest",
    ]

    def has_permission(self, request, perm):
        user = request.user
        employee = user.employee_get
        if perm in self.leave_perm:
            is_approval_manager = MultipleApprovalManagers.objects.filter(
                employee_id=employee.id
            ).exists()
            if is_approval_manager:
                return True

        is_manager = EmployeeWorkInformation.objects.filter(
            reporting_manager_id=employee
        ).exists()

        if user.has_perm(perm) or is_manager:
            return True
        return False


def manager_permission_required(perm):
    """
    Decorator for views that checks whether the user has appropriate manager permissions.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(self, request, *args, **kwargs):
            permission = ManagerPermission()
            if permission.has_permission(request, perm):
                return func(self, request, *args, **kwargs)
            else:
                return Response(
                    {"error": _("You do not have permission to perform this action.")},
                    status=status.HTTP_403_FORBIDDEN,
                )

        return wrapper

    return decorator


def manager_or_owner_permission_required(model_class, perm):
    """
    Decorator for views that checks whether the user has either manager or owner permissions and a specific permission for a specific object for a given model class.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(self, request, pk=None, *args, **kwargs):
            actor = actor_of(request.user)
            if pk:
                obj = model_class.objects.filter(pk=pk).first()
                if obj is None:
                    return Response(
                        {"error": f"{model_class.__name__} does not exist"},
                        status=status.HTTP_404_NOT_FOUND,
                    )
                if actor is not None and obj.employee_id == actor:
                    return func(self, request, pk, *args, **kwargs)
                # Not the owner: only someone who may manage *that* employee
                # (2026-11-13; it used to be any manager of anyone).
                if can_manage_employee(request.user, obj.employee_id, perm):
                    return func(self, request, pk, *args, **kwargs)
                return forbidden()
            raw = request.data.get("employee_id", None)
            target_id = int(raw) if str(raw or "").strip().isdigit() else None
            if actor is not None and target_id == actor.pk:
                return func(self, request, *args, **kwargs)
            if target_id is not None:
                from employee.models import Employee

                target = Employee.objects.filter(pk=target_id).first()
                if can_manage_employee(request.user, target, perm):
                    return func(self, request, *args, **kwargs)
                return forbidden()
            # No employee named: only a real permission holder or an admin.
            from base.roles import is_checkin_admin

            if request.user.has_perm(perm) or is_checkin_admin(request.user):
                return func(self, request, *args, **kwargs)
            return forbidden()

        return wrapper

    return decorator


def check_approval_status(model, perm):
    """checking the object approval status"""

    def decorator(func):
        @wraps(func)
        def wrapper(self, request, pk, *args, **kwargs):
            object = model.objects.filter(id=pk).first()
            if object.approved:
                return Response(
                    {
                        "error": _("Approved %(model)s can't preform this action ")
                        % {"model": model.__name__}
                    },
                    status=400,
                )
            if object.canceled:
                return Response(
                    {
                        "error": _("Canceled %(model)s can't preform this action ")
                        % {"model": model.__name__}
                    },
                    status=400,
                )
            return func(self, request, pk, *args, **kwargs)

        return wrapper

    return decorator


@decorator_with_arguments
def permission_required(function, perm):
    """
    Decorator to validate user permissions
    """

    def _function(self, *args, **kwargs):
        request = getattr(_thread_locals, "request")
        if not getattr(self, "request", None):
            self.request = request
        if request.user.has_perm(perm):
            return function(self, *args, **kwargs)
        else:
            return Response({"message": _("No permission")}, status=401)

    return _function
