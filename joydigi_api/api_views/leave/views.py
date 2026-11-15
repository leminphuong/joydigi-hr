import contextlib

from django.contrib.auth.decorators import permission_required
from django.contrib.auth.models import AnonymousUser
from django.db.models import Count, Q
from django.http import Http404, QueryDict
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.exceptions import NotFound
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.methods import filtersubordinates
from joydigi_api.api_serializers.leave.serializers import *
from leave.filters import *
from leave.methods import filter_conditional_leave_request
from leave.models import AvailableLeave, LeaveAllocationRequest, LeaveRequest, LeaveType, balance_leave_type
from notifications.signals import notify

from ...api_decorators.base.decorators import (
    actor_of,
    can_manage_employee,
    employee_from_data,
    forbidden,
    is_direct_manager,
    manager_permission_required,
)


def _leave_review_problem(user, leave_request):
    """None when the caller may decide `leave_request` now, else the reason.

    SEC-02 (2026-11-13): the same rule as the web approval (leave/views.py):
    a superuser; otherwise never one's own request; with several approval
    steps, the approver of the current step once the earlier steps are done;
    otherwise a check-in admin *of the employee's company* (2026-11-14: a JWT
    request has no selected company, so an admin role in any company used to
    count for every company) or the employee's own reporting manager.
    """
    from leave.request_access import is_admin_for

    if getattr(user, "is_superuser", False):
        return None
    actor = actor_of(user)
    if actor is None:
        return _("You do not have permission to perform this action.")
    if leave_request.employee_id_id == actor.pk:
        return _("You cannot approve your own leave request.")
    if leave_request.multiple_approvals():
        step = (
            LeaveRequestConditionApproval.objects.filter(
                manager_id=actor, leave_request_id=leave_request, is_approved=False, is_rejected=False
            )
            .order_by("sequence")
            .first()
        )
        if step is None:
            return "Bạn không phải người duyệt ở bước hiện tại."
        if LeaveRequestConditionApproval.objects.filter(
            leave_request_id=leave_request, sequence__lt=step.sequence, is_approved=False
        ).exists():
            return "Đơn này chưa được duyệt ở bước trước."
        return None
    if is_admin_for(user, leave_request.employee_id) or is_direct_manager(user, leave_request.employee_id):
        return None
    return "Bạn chỉ được duyệt đơn của thành viên trong nhóm mình."


def _may_handle(user, obj, perm):
    """Read/edit/delete a request: its own employee, or someone who may manage
    them — HR/Admin only inside their own companies (2026-11-14): the
    company-scoped managers do not filter JWT requests, so a permission held
    in another company used to reach every company's requests."""
    from leave.request_access import may_manage_leave

    return may_manage_leave(user, obj, perm)
from ...api_methods.base.methods import groupby_queryset


class AvailableLeavePagination(PageNumberPagination):
    """20 per page by default; a client may ask up to 100 (`page_size`)."""

    page_size_query_param = "page_size"
    max_page_size = 100


class EmployeeAvailableLeaveGetAPIView(APIView):
    """The leave types the employee may request, with their balances.

    Active types only (an inactive type cannot be requested), in name order
    so pages are stable, read without the HR session's company filter.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.core.exceptions import ObjectDoesNotExist

        from joydigi_api.api_serializers.leave.serializers import (
            EmployeeLeaveOptionSerializer,
            UnassignedLeaveTypeSerializer,
        )
        from leave.services import (
            NO_COMPANY_MESSAGE,
            NO_EMPLOYEE_MESSAGE,
            NO_WORK_INFO_MESSAGE,
            employee_company,
            employee_has_work_info,
            requestable_leave_options,
            unassigned_eligible_leave_types,
        )

        def empty(message):
            # 200 with a message, not a 500: the app shows it as is.
            return Response(
                {"count": 0, "next": None, "previous": None, "results": [], "unassigned": [], "message": message}
            )

        try:
            employee = request.user.employee_get
        except ObjectDoesNotExist:
            return empty(NO_EMPLOYEE_MESSAGE)
        if not employee_has_work_info(employee):
            return empty(NO_WORK_INFO_MESSAGE)
        # One entry per request type; types sharing a fund all carry that
        # fund's days (2026-11-11).
        available_leave = requestable_leave_options(employee)
        paginator = AvailableLeavePagination()
        page = paginator.paginate_queryset(available_leave, request)
        serializer = EmployeeLeaveOptionSerializer(page, many=True)
        response = paginator.get_paginated_response(serializer.data)
        # Eligible types HR has not opened yet: listed apart (old apps read
        # only `results`), so the new app can say "contact HR".
        response.data["unassigned"] = UnassignedLeaveTypeSerializer(
            unassigned_eligible_leave_types(employee), many=True
        ).data
        # No company on the work information: shared types still work, and
        # the app shows this only when nothing at all is listed.
        response.data["message"] = NO_COMPANY_MESSAGE if employee_company(employee) is None else None
        return response


class EmployeeLeaveRequestGetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = UserLeaveRequestFilter
    queryset = LeaveRequest.objects.none()  # For drf-yasg schema generation

    def get_queryset(self):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False):
            return LeaveRequest.objects.none()
        if not self.request.user.is_authenticated:
            return LeaveRequest.objects.none()
        employee = self.request.user.employee_get
        return employee.leaverequest_set.all().order_by("-id")

    def get(self, request):
        employee = request.user.employee_get
        leave_request = (
            employee.leaverequest_set.all().prefetch_related("cancellation_requests").order_by("-id")
        )
        filterset = self.filterset_class(request.GET, queryset=leave_request)
        paginator = PageNumberPagination()
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = userLeaveRequestGetAllSerilaizer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        from django.core.exceptions import ObjectDoesNotExist

        from leave.services import NO_EMPLOYEE_MESSAGE

        try:
            employee_id = request.user.employee_get.id
        except ObjectDoesNotExist:
            return Response({"non_field_errors": [NO_EMPLOYEE_MESSAGE]}, status=400)
        data = request.data
        if isinstance(data, QueryDict):
            data = data.dict()
        data["employee_id"] = employee_id
        data["end_date"] = (
            data.get("start_date") if not data.get("end_date") else data.get("end_date")
        )
        serializer = LeaveRequestCreateUpdateSerializer(data=data)
        from django.db import transaction

        # Validate and save under one transaction: the balance row is locked
        # while it is checked, so two requests sent together can not both
        # pass against the same days (2026-11-11).
        from leave.monthly_limit import LeaveRuleError

        try:
            with transaction.atomic():
                valid = serializer.is_valid()
                leave_request = serializer.save() if valid else None
        except LeaveRuleError as refused:  # a rule checked again under the lock (2026-11-15)
            return Response({"non_field_errors": refused.messages}, status=400)
        if valid:
            with contextlib.suppress(Exception):
                notify.send(
                    request.user.employee_get,
                    recipient=leave_request.employee_id.employee_work_info.reporting_manager_id.employee_user_id,
                    verb="You have a new leave request to validate.",
                    verb_ar="لديك طلب إجازة جديد يجب التحقق منه.",
                    verb_de="Sie haben eine neue Urlaubsanfrage zur Validierung.",
                    verb_es="Tiene una nueva solicitud de permiso que debe validar.",
                    verb_fr="Vous avez une nouvelle demande de congé à valider.",
                    icon="people-circle",
                    redirect=f"/leave/request-view?id={leave_request.id}",
                    api_redirect=f"/api/leave/request/{leave_request.id}/",
                )
            return Response(
                userLeaveRequestGetAllSerilaizer(leave_request).data, status=201
            )
        return Response(serializer.errors, status=400)


class EmployeeLeaveRequestUpdateDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_request(self, request, pk):
        # The employee's OWN leave only; anything else — a colleague's, another
        # company's, a missing id — is not found: 404 (2026-11-14; was 400).
        try:
            return LeaveRequest.objects.get(
                pk=pk, employee_id=request.user.employee_get
            )
        except LeaveRequest.DoesNotExist:
            raise NotFound({"error": "Không tìm thấy đơn nghỉ phép.", "code": "not_found"})

    def get(self, request, pk):
        leave_request = self.get_leave_request(request, pk)
        serializer = UserLeaveRequestGetSerilaizer(leave_request)
        return Response(serializer.data, status=200)

    def put(self, request, pk):
        from django.db import transaction

        from leave.balance_ops import lock_leave_request

        leave_request = self.get_leave_request(request, pk)
        employee_id = request.user.employee_get
        with transaction.atomic():
            # Locked (2026-11-13): an approval at the same moment waits, or
            # this edit sees it and stops.
            locked = lock_leave_request(leave_request.pk)
            if locked is None or locked.status != "requested":
                return Response(
                    {"error": "Đơn đã được xử lý, không thể sửa.", "code": "not_pending"}, status=409
                )
            if locked.employee_id_id != employee_id.pk:  # reassigned meanwhile
                raise NotFound({"error": "Không tìm thấy đơn nghỉ phép.", "code": "not_found"})
            return self._update(request, locked, employee_id)

    def _update(self, request, leave_request, employee_id):
        if (
            leave_request.status == "requested"
            and leave_request.employee_id == employee_id
        ):
            data = request.data
            if isinstance(data, QueryDict):
                data = data.dict()
            data["employee_id"] = employee_id.id
            data["end_date"] = (
                data.get("start_date")
                if not data.get("end_date")
                else data.get("end_date")
            )
            serializer = LeaveRequestCreateUpdateSerializer(leave_request, data=data)
            if serializer.is_valid():
                leave_request = serializer.save()
                return Response(
                    UserLeaveRequestGetSerilaizer(leave_request).data, status=201
                )
            return Response(serializer.errors, status=400)
        raise serializers.ValidationError({"error": _("Access Denied..")})

    def delete(self, request, pk):
        from django.db import transaction

        from leave.balance_ops import lock_leave_request

        leave_request = self.get_leave_request(request, pk)
        employee_id = request.user.employee_get
        if leave_request.employee_id != employee_id:
            raise serializers.ValidationError({"error": _("Access Denied..")})
        with transaction.atomic():
            # Withdrawing a pending request (2026-11-13: locked, checked again).
            # Nothing was taken from the balance, so nothing is given back. An
            # approved one is never deleted here: its days would be lost —
            # use PUT /api/leave/cancel/<id>/ to ask for its cancellation.
            locked = lock_leave_request(leave_request.pk)
            if locked is None or locked.status != "requested":
                return Response(
                    {
                        "error": "Đơn đã được xử lý. Đơn đã duyệt cần gửi yêu cầu hủy.",
                        "code": "not_pending",
                    },
                    status=409,
                )
            locked.delete()
        return Response({"message": "Leave request deleted successfully.."}, status=200)


class LeaveTypeGetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = LeaveTypeFilter
    queryset = LeaveType.objects.none()  # For drf-yasg schema generation

    def get_queryset(self):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False):
            return LeaveType.objects.none()
        return LeaveType.objects.all()

    # @method_decorator(permission_required('leave.view_leavetype', raise_exception=True), name='dispatch')
    def get(self, request):
        # Phase LEAVE-7A.4: `LeaveType.objects` is a
        # `JoydigiCompanyManager`, but its scoping only kicks in when
        # `get_selected_company()` (a ContextVar set by the *web*
        # session's company switcher) has a value — a JWT-authenticated
        # mobile request has no such session, so that manager silently
        # returns every company's types unfiltered. Scoped here
        # explicitly to the requesting employee's own company instead
        # (plus any genuinely global, company-less type), matching the
        # same `Q(company) | Q(isnull=True)` semantics the manager
        # itself uses for the web case.
        leave_type = LeaveType.objects.all()
        employee = getattr(request.user, "employee_get", None)
        company = employee.get_company() if employee else None
        if company:
            leave_type = leave_type.filter(
                Q(company_id=company) | Q(company_id__isnull=True)
            )
        filterset = self.filterset_class(request.GET, queryset=leave_type)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = LeaveTypeAllGetSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @method_decorator(
        permission_required("leave.add_leavetype", raise_exception=True),
        name="dispatch",
    )
    def post(self, request):
        serializer = LeaveTypeGetCreateSerilaizer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)


class LeaveTypeGetUpdateDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_type(self, pk):
        try:
            return LeaveType.objects.get(pk=pk)
        except LeaveType.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @method_decorator(
        permission_required("leave.view_leavetype", raise_exception=True),
        name="dispatch",
    )
    def get(self, request, pk):
        leave_type = self.get_leave_type(pk)
        serializer = LeaveTypeGetCreateSerilaizer(leave_type)
        return Response(serializer.data, status=200)

    @method_decorator(
        permission_required("leave.change_leavetype", raise_exception=True),
        name="dispatch",
    )
    def put(self, request, pk):
        leave_type = self.get_leave_type(pk)
        serializer = LeaveTypeGetCreateSerilaizer(leave_type, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)

    @method_decorator(
        permission_required("leave.delete_leavetype", raise_exception=True),
        name="dispatch",
    )
    def delete(self, request, pk):
        leave_type = self.get_leave_type(pk)
        leave_type.delete()
        return Response(status=201)


class LeaveAllocationRequestGetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = LeaveAllocationRequestFilter
    queryset = LeaveAllocationRequest.objects.none()  # For drf-yasg schema generation

    def get_queryset(self):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False):
            return LeaveAllocationRequest.objects.none()
        if not self.request.user.is_authenticated:
            return LeaveAllocationRequest.objects.none()
        from leave.request_access import within_reach

        allocation_requests = LeaveAllocationRequest.objects.all().order_by("-id")
        return within_reach(
            self.request.user,
            filtersubordinates(self.request, allocation_requests, "leave.view_leaveallocationrequest"),
            "leave.view_leaveallocationrequest",
        )

    def get_user(self, request):
        user = request.user
        if isinstance(user, AnonymousUser):
            raise Http404("AnonymousUser")
        return user

    @manager_permission_required("leave.view_leaveallocationrequest")
    def get(self, request):
        # Limited to what the caller may see (2026-11-14): JWT requests are
        # not filtered by company, so a permission used to list every
        # company's requests.
        queryset = self.get_queryset()
        filterset = self.filterset_class(request.GET, queryset=queryset)
        paginator = PageNumberPagination()
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = LeaveAllocationRequestGetSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        data = request.data
        employee_id = self.get_user(request).employee_get.id
        target = employee_from_data(request)
        if target is None:
            return Response({"employee_id": [_("This field is required.")]}, status=400)
        from leave.request_access import may_manage_employee

        # HR rights count in the employee's company only (2026-11-14).
        if target.pk != employee_id and not may_manage_employee(
            request.user, target, "leave.add_leaveallocationrequest"
        ):
            return forbidden()
        if isinstance(data, QueryDict):
            data = data.dict()
        data["created_by"] = employee_id
        serializer = LeaveAllocationRequestCreateSerializer(data=data)
        if serializer.is_valid():
            allocation_request = serializer.save()
            with contextlib.suppress(Exception):
                notify.send(
                    request.user.employee_get,
                    recipient=allocation_request.employee_id.employee_work_info.reporting_manager_id.employee_user_id,
                    verb=f"New leave allocation request created for {allocation_request.employee_id}.",
                    verb_ar=f"تم إنشاء طلب تخصيص إجازة جديد لـ {allocation_request.employee_id}.",
                    verb_de=f"Neue Anfrage zur Urlaubszuweisung erstellt für {allocation_request.employee_id}.",
                    verb_es=f"Nueva solicitud de asignación de permisos creada para {allocation_request.employee_id}.",
                    verb_fr=f"Nouvelle demande d'allocation de congé créée pour {allocation_request.employee_id}.",
                    icon="people-cicle",
                    redirect=f"/leave/leave-allocation-request-view?id={allocation_request.id}",
                    api_redirect=f"/api/leave/allocation-request/{allocation_request.id}/",
                )
            return Response(
                LeaveAllocationRequestGetSerializer(allocation_request).data, status=201
            )
        return Response(serializer.errors, status=400)


class LeaveAllocationRequestGetUpdateDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_allocation_request(self, pk):
        try:
            return LeaveAllocationRequest.objects.get(pk=pk)
        except LeaveAllocationRequest.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @manager_permission_required("leave.view_leaveallocationrequest")
    def get(self, request, pk):
        allocation_request = self.get_leave_allocation_request(pk)
        if not _may_handle(request.user, allocation_request, "leave.view_leaveallocationrequest"):
            return forbidden()
        serializer = LeaveAllocationRequestGetSerializer(allocation_request)
        return Response(serializer.data, status=200)

    @manager_permission_required("leave.change_leaveallocationrequest")
    def put(self, request, pk):
        allocation_request = self.get_leave_allocation_request(pk)
        if not _may_handle(request.user, allocation_request, "leave.change_leaveallocationrequest"):
            return forbidden()
        if allocation_request.status == "requested":
            serializer = LeaveAllocationRequestSerilaizer(
                allocation_request, data=request.data
            )
            if serializer.is_valid():
                allocation_request = serializer.save()
                return Response(
                    LeaveAllocationRequestGetSerializer(allocation_request).data,
                    status=201,
                )
            return Response(serializer.errors, status=400)
        raise serializers.ValidationError({"error": _("Access Denied..")})

    @manager_permission_required("leave.delete_leaveallocationrequest")
    def delete(self, request, pk):
        allocation_request = self.get_leave_allocation_request(pk)
        if not _may_handle(request.user, allocation_request, "leave.delete_leaveallocationrequest"):
            return forbidden()
        if allocation_request.status == "requested":
            allocation_request.delete()
            return Response(status=200)
        raise serializers.ValidationError({"error": _("Access Denied..")})


class AssignLeaveGetCreateAPIView(APIView):

    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = AssignedLeaveFilter
    queryset = AvailableLeave.objects.none()  # For drf-yasg schema generation

    def get_queryset(self):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False):
            return AvailableLeave.objects.none()
        if not self.request.user.is_authenticated:
            return AvailableLeave.objects.none()
        available_leave = AvailableLeave.objects.all().order_by("-id")
        return filtersubordinates(
            self.request, available_leave, "leave.view_availableleave"
        )

    @method_decorator(
        permission_required("leave.view_availableleave", raise_exception=True),
        name="dispatch",
    )
    def get(self, request):
        available_leave = AvailableLeave.objects.all().order_by("-id")
        queryset = filtersubordinates(
            request, available_leave, "leave.view_availableleave"
        )
        filterset = self.filterset_class(request.GET, queryset=queryset)
        paginator = PageNumberPagination()
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = AssignLeaveGetSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @method_decorator(
        permission_required("leave.add_availableleave", raise_exception=True),
        name="dispatch",
    )
    def post(self, request):
        serializer = AssignLeaveCreateSerializer(data=request.data)
        if serializer.is_valid():
            employee_ids = serializer.validated_data.get("employee_ids")
            leave_type_ids = serializer.validated_data.get("leave_type_ids")
            for employee_id in employee_ids:
                for leave_type_id in leave_type_ids:
                    if leave_type_id.shared_balance_with_id:
                        continue  # shares another type's fund: no balance of its own
                    if not AvailableLeave.objects.filter(
                        employee_id=employee_id, leave_type_id=leave_type_id
                    ).exists():
                        AvailableLeave.objects.create(
                            employee_id=employee_id,
                            leave_type_id=leave_type_id,
                            available_days=leave_type_id.total_days,
                        )
                        with contextlib.suppress(Exception):
                            notify.send(
                                request.user.employee_get,
                                recipient=employee_id.employee_user_id,
                                verb="New leave type is assigned to you",
                                verb_ar="تم تعيين نوع إجازة جديد لك",
                                verb_de="Dir wurde ein neuer Urlaubstyp zugewiesen",
                                verb_es="Se te ha asignado un nuevo tipo de permiso",
                                verb_fr="Un nouveau type de congé vous a été attribué",
                                icon="people-circle",
                                redirect="/leave/user-request-view",
                                api_redirect="/api/leave/user-request/",
                            )
            return Response(status=201)
        return Response(serializer.errors, status=400)


class AssignLeaveGetUpdateDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_available_leave(self, pk):
        try:
            return AvailableLeave.objects.get(pk=pk)
        except AvailableLeave.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @method_decorator(
        permission_required("leave.view_availableleave", raise_exception=True),
        name="dispatch",
    )
    def get(self, request, pk):
        available_leave = self.get_available_leave(pk)
        serializer = AssignLeaveGetSerializer(available_leave)
        return Response(serializer.data, status=200)

    @method_decorator(
        permission_required("leave.change_availableleave", raise_exception=True),
        name="dispatch",
    )
    def put(self, request, pk):
        available_leave = self.get_available_leave(pk)
        serializer = AvailableLeaveUpdateSerializer(available_leave, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(status=201)
        return Response(serializer.errors, status=400)

    @method_decorator(
        permission_required("leave.delete_availableleave", raise_exception=True),
        name="dispatch",
    )
    def delete(self, request, pk):
        available_leave = self.get_available_leave(pk)
        available_leave.delete()
        return Response(status=200)


class LeaveRequestGetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = LeaveRequestFilter
    queryset = LeaveRequest.objects.none()  # For drf-yasg schema generation

    def get_queryset(self):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False):
            return LeaveRequest.objects.none()
        if not self.request.user.is_authenticated:
            return LeaveRequest.objects.none()
        from leave.request_access import VIEW_PERM, within_reach

        leave_request = LeaveRequest.objects.all().order_by("-id")
        multiple_approvals = filter_conditional_leave_request(self.request)
        # Limited to what the caller may see (2026-11-14): JWT requests are
        # not filtered by company, and filtersubordinates gives a holder of
        # the permission every company's leaves.
        return within_reach(
            self.request.user,
            filtersubordinates(self.request, leave_request, VIEW_PERM) | multiple_approvals,
            VIEW_PERM,
            extra_ids=multiple_approvals.values_list("id", flat=True),
        )

    @manager_permission_required("leave.view_leaverequest")
    def get(self, request):
        queryset = self.get_queryset()
        filterset = self.filterset_class(request.GET, queryset=queryset)
        paginator = PageNumberPagination()
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = LeaveRequestGetAllSerilaizer(
            page, context={"request": request}, many=True
        )
        return paginator.get_paginated_response(serializer.data)

    @manager_permission_required("leave.add_leaverequest")
    def post(self, request):
        target = employee_from_data(request)
        actor = actor_of(request.user)
        if target is None:
            return Response({"employee_id": [_("This field is required.")]}, status=400)
        from leave.request_access import may_manage_employee

        if not ((actor is not None and target.pk == actor.pk)
                or may_manage_employee(request.user, target, "leave.add_leaverequest")):
            return forbidden()
        data = request.data
        if isinstance(data, QueryDict):
            data = data.dict()
        data["end_date"] = (
            data.get("start_date") if not data.get("end_date") else data.get("end_date")
        )
        serializer = LeaveRequestCreateUpdateSerializer(data=data)
        from django.db import transaction

        from leave.monthly_limit import LeaveRuleError

        # Validate and save under one transaction, as the employee's create:
        # the fund's balance row is locked while checked (2026-11-15).
        try:
            with transaction.atomic():
                valid = serializer.is_valid()
                leave_request = serializer.save() if valid else None
        except LeaveRuleError as refused:
            return Response({"non_field_errors": refused.messages}, status=400)
        if valid:
            with contextlib.suppress(Exception):
                notify.send(
                    request.user.employee_get,
                    recipient=leave_request.employee_id.employee_work_info.reporting_manager_id.employee_user_id,
                    verb=f"New leave request created for {leave_request.employee_id}.",
                    verb_ar=f"تم إنشاء طلب إجازة جديد لـ {leave_request.employee_id}.",
                    verb_de=f"Neuer Urlaubsantrag erstellt für {leave_request.employee_id}.",
                    verb_es=f"Nueva solicitud de permiso creada para {leave_request.employee_id}.",
                    verb_fr=f"Nouvelle demande de congé créée pour {leave_request.employee_id}.",
                    icon="people-circle",
                    redirect=f"/leave/request-view?id={leave_request.id}",
                    api_redirect=f"/api/leave/request/{leave_request.id}/",
                )
            return Response(
                LeaveRequestGetSerilaizer(
                    leave_request, context={"request": request}
                ).data,
                status=201,
            )
        return Response(serializer.errors, status=400)


class LeaveRequestGetUpdateDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_request(self, pk):
        try:
            return LeaveRequest.objects.get(pk=pk)
        except LeaveRequest.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @manager_permission_required("leave.view_leaverequest")
    def get(self, request, pk):
        from leave.request_access import is_step_manager

        leave_request = self.get_leave_request(pk)
        # A step approver sees in the list the leave they approve; its detail
        # used to answer 403 (2026-11-14).
        if not (_may_handle(request.user, leave_request, "leave.view_leaverequest")
                or is_step_manager(request.user, leave_request)):
            return forbidden()
        serializer = LeaveRequestGetSerilaizer(
            leave_request, context={"request": request}
        )
        return Response(serializer.data, status=200)

    @manager_permission_required("leave.change_leaverequest")
    def put(self, request, pk):
        from django.db import transaction

        from leave.balance_ops import lock_leave_request

        leave_request = self.get_leave_request(pk)
        if not _may_handle(request.user, leave_request, "leave.change_leaverequest"):
            return forbidden()
        if leave_request.status == "requested":
            data = request.data
            if isinstance(data, QueryDict):
                data = data.dict()
            data["end_date"] = (
                data.get("start_date")
                if not data.get("end_date")
                else data.get("end_date")
            )
            with transaction.atomic():
                # Locked and checked again (2026-11-13, P1-2): an approval
                # committed since the read above must not be overwritten by
                # this edit (status back to requested, days still taken).
                locked = lock_leave_request(leave_request.pk)
                if locked is None or locked.status != "requested":
                    return Response(
                        {"error": "Đơn vừa được xử lý, không thể sửa.", "code": "not_pending"}, status=409
                    )
                # Checked again on the locked row, and for the employee the
                # leave would be moved to (2026-11-14): never outside the team.
                if not _may_handle(request.user, locked, "leave.change_leaverequest"):
                    return forbidden()
                target_id = data.get("employee_id")
                if target_id not in (None, "") and str(target_id) != str(locked.employee_id_id):
                    from leave.request_access import may_manage_employee, whole_number

                    if whole_number(target_id) is None:
                        return Response({"employee_id": ["Mã nhân viên không hợp lệ."]}, status=400)
                    target = Employee.objects.filter(pk=whole_number(target_id)).first()
                    if target is None or not may_manage_employee(
                        request.user, target, "leave.change_leaverequest", allow_self=True
                    ):
                        return forbidden("Bạn không có quyền chuyển đơn cho nhân viên này.")
                serializer = LeaveRequestCreateUpdateSerializer(locked, data=data)
                saved = serializer.save() if serializer.is_valid() else None
            if saved is not None:
                leave_request = saved
                with contextlib.suppress(Exception):
                    notify.send(
                        request.user.employee_get,
                        recipient=leave_request.employee_id.employee_work_info.reporting_manager_id.employee_user_id,
                        verb=f"Leave request updated for {leave_request.employee_id}.",
                        verb_ar=f"تم تحديث طلب الإجازة لـ {leave_request.employee_id}.",
                        verb_de=f"Urlaubsantrag aktualisiert für {leave_request.employee_id}.",
                        verb_es=f"Solicitud de permiso actualizada para {leave_request.employee_id}.",
                        verb_fr=f"Demande de congé mise à jour pour {leave_request.employee_id}.",
                        icon="people-circle",
                        redirect=f"/leave/request-view?id={leave_request.id}",
                        api_redirect=f"/api/leave/request/{leave_request.id}/",
                    )
                return Response(
                    UserLeaveRequestGetSerilaizer(
                        leave_request, context={"request": request}
                    ).data,
                    status=201,
                )
            return Response(serializer.errors, status=400)
        if leave_request.status == "approved":
            # An approved leave keeps its days, type and employee
            # (2026-11-15): the fund was charged on them.
            # Same status as before (400), clearer words.
            raise serializers.ValidationError({"error": (
                "Đơn đã được duyệt nên không thể sửa qua ứng dụng. Để điều chỉnh, hãy gửi yêu cầu "
                "hủy đơn (hoàn phép có ghi lịch sử) rồi tạo đơn mới; ghi chú sửa được trên web."
            )})
        raise serializers.ValidationError({"error": _("Access Denied..")})

    @manager_permission_required("leave.delete_leaverequest")
    def delete(self, request, pk):
        from django.db.models import ProtectedError

        from leave.request_access import DELETED, REFUSED, delete_waiting_leave

        leave_request = self.get_leave_request(pk)
        if not _may_handle(request.user, leave_request, "leave.delete_leaverequest"):
            return forbidden()
        # Right and status checked again on the locked row (2026-11-14): an
        # approval committed meanwhile is never deleted.
        try:
            outcome = delete_waiting_leave(
                leave_request, lambda row: _may_handle(request.user, row, "leave.delete_leaverequest")
            )
        except ProtectedError:
            return Response({"error": "Đơn đang được dữ liệu khác tham chiếu, không thể xóa.", "code": "protected"},
                            status=409)
        if outcome == DELETED:
            return Response(status=200)
        if outcome == REFUSED:
            return forbidden()
        raise serializers.ValidationError({"error": _("Access Denied..")})


class CompanyLeaveGetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(
        permission_required("base.view_companyleaves", raise_exception=True),
        name="dispatch",
    )
    def get(self, request):
        company_leave = CompanyLeaves.objects.all().order_by("-id")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(company_leave, request)
        serializer = CompanyLeaveSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @method_decorator(
        permission_required("leave.add_companyleave", raise_exception=True),
        name="dispatch",
    )
    def post(self, request):
        serializer = CompanyLeaveSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)


class CompanyLeaveGetUpdateDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_company_leave(self, pk):
        try:
            return CompanyLeaves.objects.get(pk=pk)
        except CompanyLeaves.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @method_decorator(
        permission_required("base.view_companyleaves", raise_exception=True),
        name="dispatch",
    )
    def get(self, request, pk):
        company_leave = self.get_company_leave(pk)
        serializer = CompanyLeaveSerializer(company_leave)
        return Response(serializer.data, status=200)

    @method_decorator(
        permission_required("base.change_companyleaves", raise_exception=True),
        name="dispatch",
    )
    def put(self, request, pk):
        company_leave = self.get_company_leave(pk)
        serializer = CompanyLeaveSerializer(company_leave, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=200)
        return Response(serializer.errors, status=400)

    @method_decorator(
        permission_required("base.delete_companyleaves", raise_exception=True),
        name="dispatch",
    )
    def delete(self, request, pk):
        company_leave = self.get_company_leave(pk)
        company_leave.delete()
        return Response(status=200)


class HolidayGetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(
        permission_required("base.view_holidays", raise_exception=True), name="dispatch"
    )
    def get(self, request):
        holiday = Holidays.objects.all().order_by("-id")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(holiday, request)
        serializer = HoildaySerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @method_decorator(
        permission_required("base.add_holidays", raise_exception=True), name="dispatch"
    )
    def post(self, request):
        serializer = HoildaySerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)


class HolidayGetUpdateDeleteAPIView(APIView):

    def get_holiday(self, pk):
        try:
            return Holidays.objects.get(pk=pk)
        except Holidays.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @method_decorator(
        permission_required("base.view_holidays", raise_exception=True), name="dispatch"
    )
    def get(self, request, pk):
        holiday = self.get_holiday(pk)
        serializer = HoildaySerializer(holiday)
        return Response(serializer.data, status=200)

    @method_decorator(
        permission_required("base.change_holidays", raise_exception=True),
        name="dispatch",
    )
    def put(self, request, pk):
        holiday = self.get_holiday(pk)
        serializer = HoildaySerializer(holiday, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=201)
        return Response(serializer.errors, status=400)

    @method_decorator(
        permission_required("base.delete_holidays", raise_exception=True),
        name="dispatch",
    )
    def delete(self, request, pk):
        holiday = self.get_holiday(pk)
        holiday.delete()
        return Response(status=200)


class LeaveRequestApproveAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_request(self, pk):
        try:
            return LeaveRequest.objects.get(pk=pk)
        except LeaveRequest.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @manager_permission_required("leave.change_leaverequest")
    def put(self, request, pk):
        from django.db import transaction

        from leave.balance_ops import (
            LeaveBalanceError,
            deduct_for_request,
            ensure_sufficient,
            lock_balance,
            lock_leave_request,
        )

        leave_request = self.get_leave_request(pk)
        problem = _leave_review_problem(request.user, leave_request)
        if problem:
            return forbidden(problem)
        final = True
        try:
            with transaction.atomic():
                # Locked, then checked again (2026-11-13): a second approval of
                # the same request waits for the first and stops here instead
                # of deducting the days a second time.
                leave_request = lock_leave_request(pk)
                # Same answers as the serializer this view used before
                # (400, non_field_errors), so existing clients read them.
                if leave_request is None or leave_request.status != "requested":
                    return Response({"non_field_errors": [_("Nothing to approve.")]}, status=400)
                balance = lock_balance(leave_request.employee_id, leave_request.leave_type_id)
                if balance is None:
                    return Response(
                        {"non_field_errors": ["Nhân viên chưa có quỹ phép cho loại phép này."]}, status=400
                    )
                ensure_sufficient(leave_request, balance)  # at every step, as before
                if leave_request.multiple_approvals():
                    if request.user.is_superuser:
                        LeaveRequestConditionApproval.objects.filter(
                            leave_request_id=leave_request
                        ).update(is_approved=True)
                    else:
                        step = (
                            LeaveRequestConditionApproval._base_manager.select_for_update()
                            .filter(
                                manager_id=actor_of(request.user),
                                leave_request_id=leave_request,
                                is_approved=False,
                                is_rejected=False,
                            )
                            .order_by("sequence")
                            .first()
                        )
                        if step is None:
                            return forbidden("Bạn không phải người duyệt ở bước hiện tại.")
                        step.is_approved = True
                        step.save()
                        final = not LeaveRequestConditionApproval.objects.filter(
                            leave_request_id=leave_request, is_approved=False
                        ).exists()
                if final:
                    deduct_for_request(leave_request, balance)
                    leave_request.status = "approved"
                    leave_request.save()
        except LeaveBalanceError as error:
            return Response({"non_field_errors": [error.message]}, status=400)
        if final:
            with contextlib.suppress(Exception):
                notify.send(
                    request.user.employee_get,
                    recipient=leave_request.employee_id.employee_user_id,
                    verb="Your Leave request has been approved",
                    verb_ar="تمت الموافقة على طلب الإجازة الخاص بك",
                    verb_de="Ihr Urlaubsantrag wurde genehmigt",
                    verb_es="Se ha aprobado su solicitud de permiso",
                    verb_fr="Votre demande de congé a été approuvée",
                    icon="people-circle",
                    redirect=f"/leave/user-request-view?id={leave_request.id}",
                    api_redirect=f"/api/leave/user-request/{leave_request.id}",
                )
        return Response(status=200)


class LeaveRequestRejectAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_request(self, pk):
        try:
            return LeaveRequest.objects.get(pk=pk)
        except LeaveRequest.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @manager_permission_required("leave.change_leaverequest")
    def put(self, request, pk):
        from django.db import transaction

        from leave.balance_ops import lock_balance, lock_leave_request, refund_for_request

        leave_request = self.get_leave_request(pk)
        problem = _leave_review_problem(request.user, leave_request)
        if problem:
            return forbidden(problem)
        with transaction.atomic():
            leave_request = lock_leave_request(pk)
            if leave_request is None or leave_request.status == "rejected":
                raise serializers.ValidationError(_("Nothing to reject."))
            # Nobody — a superuser included — rejects their own leave to get
            # its days back (2026-11-13, P1-4): that is a cancellation, which
            # needs someone else's approval.
            actor = actor_of(request.user)
            if (
                actor is not None
                and leave_request.employee_id_id == actor.pk
                and (leave_request.approved_available_days or leave_request.approved_carryforward_days)
            ):
                return forbidden("Bạn không thể tự từ chối đơn đã duyệt của chính mình để lấy lại ngày phép; hãy gửi yêu cầu hủy để quản lý hoặc HR duyệt.")
            # The days go back to the employee who asked for them (2026-11-11),
            # exactly once (2026-11-13): the refund zeroes what was recorded.
            balance = lock_balance(leave_request.employee_id, leave_request.leave_type_id)
            refund_for_request(leave_request, balance)
            leave_request.status = "rejected"
            leave_request.save()
        with contextlib.suppress(Exception):
            notify.send(
                request.user.employee_get,
                recipient=leave_request.employee_id.employee_user_id,
                verb="Your Leave request has been rejected",
                verb_ar="تم رفض طلب الإجازة الخاص بك",
                verb_de="Ihr Urlaubsantrag wurde abgelehnt",
                verb_es="Tu solicitud de permiso ha sido rechazada",
                verb_fr="Votre demande de congé a été rejetée",
                icon="people-circle",
                redirect=f"/leave/user-request-view?id={leave_request.id}",
                api_redirect=f"/api/leave/user-request/{leave_request.id}/",
            )
        return Response(status=200)


def _cancellation_error(error):
    return Response({"error": error.message, "code": error.code}, status=error.status)


class LeaveRequestCancelAPIView(APIView):
    """The employee cancels their pending leave, or asks to cancel an approved
    one (2026-11-13 policy: the leave stays approved until a manager/HR
    approves the request; only then are its days given back)."""

    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        from leave.cancellation import CancellationError, cancellation_json, request_cancellation

        reason = request.data.get("reason", "") if hasattr(request.data, "get") else ""
        try:
            outcome, cancellation = request_cancellation(request.user, pk, str(reason or ""))
        except CancellationError as error:
            return _cancellation_error(error)
        if outcome == "cancelled":
            return Response({"status": "cancelled", "message": "Đã hủy đơn nghỉ phép."}, status=200)
        return Response(
            {
                "status": "cancellation_pending",
                "message": "Đã gửi yêu cầu hủy. Đơn vẫn có hiệu lực cho tới khi được duyệt.",
                "cancellation": cancellation_json(cancellation),
            },
            status=200,
        )


class LeaveCancellationRequestListAPIView(APIView):
    """Waiting cancellation requests the caller may decide (manager/HR)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from leave.cancellation import cancellation_json, visible_waiting_requests

        rows = visible_waiting_requests(request.user)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(rows, request)
        data = []
        for row in page:
            leave = row.leave_request
            item = cancellation_json(row)
            item.update(
                {
                    "leave_request_id": leave.pk,
                    "employee": {"id": leave.employee_id_id, "name": leave.employee_id.get_full_name()},
                    "leave_type": leave.leave_type_id.name if leave.leave_type_id_id else None,
                    "start_date": str(leave.start_date),
                    "end_date": str(leave.end_date),
                    "requested_days": leave.requested_days,
                }
            )
            data.append(item)
        return paginator.get_paginated_response(data)


class LeaveCancellationDecisionAPIView(APIView):
    """PUT .../<pk>/approve/ or .../<pk>/reject/ with an optional "reason"."""

    permission_classes = [IsAuthenticated]
    approve = True

    def put(self, request, pk):
        from leave.cancellation import CancellationError, cancellation_json, decide_cancellation

        reason = request.data.get("reason", "") if hasattr(request.data, "get") else ""
        try:
            cancellation = decide_cancellation(request.user, pk, approve=self.approve, reason=str(reason or ""))
        except CancellationError as error:
            return _cancellation_error(error)
        return Response({"cancellation": cancellation_json(cancellation)}, status=200)


class LeaveCancellationApproveAPIView(LeaveCancellationDecisionAPIView):
    approve = True


class LeaveCancellationRejectAPIView(LeaveCancellationDecisionAPIView):
    approve = False


class LeaveAllocationApproveAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_allocation_request(self, pk):
        try:
            return LeaveAllocationRequest.objects.get(pk=pk)
        except LeaveAllocationRequest.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @manager_permission_required("leave.change_leaveallocationrequest")
    def put(self, request, pk):
        from django.db import transaction

        from leave.balance_ops import grant_allocation, lock_allocation_request, lock_balance

        leave_allocation_request = self.get_leave_allocation_request(pk)
        # Never one's own allocation; only someone who may manage that
        # employee (web: leave/decorators.py) — 2026-11-13.
        # HR rights count in the employee's company only (2026-11-14).
        from leave.request_access import may_manage_employee

        if not may_manage_employee(
            request.user, leave_allocation_request.employee_id, "leave.change_leaveallocationrequest"
        ):
            return forbidden()
        with transaction.atomic():
            allocation = lock_allocation_request(pk)
            if allocation is None or allocation.status != "requested":
                raise serializers.ValidationError(_("Access Denied."))
            if not may_manage_employee(request.user, allocation.employee_id, "leave.change_leaveallocationrequest"):
                return forbidden()  # checked again on the locked row
            balance = lock_balance(allocation.employee_id, allocation.leave_type_id, create=True)
            grant_allocation(allocation, balance)
            allocation.status = "approved"
            allocation.save()
        return Response(status=200)


class LeaveAllocationRequestRejectAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_allocation_request(self, pk):
        try:
            return LeaveAllocationRequest.objects.get(pk=pk)
        except LeaveAllocationRequest.DoesNotExist as e:
            raise serializers.ValidationError(e)

    @manager_permission_required("leave.change_leaveallocationrequest")
    def put(self, request, pk):
        from django.db import transaction

        from leave.balance_ops import lock_allocation_request, lock_balance, withdraw_allocation

        leave_allocation_request = self.get_leave_allocation_request(pk)
        # HR rights count in the employee's company only (2026-11-14).
        from leave.request_access import may_manage_employee

        if not may_manage_employee(
            request.user, leave_allocation_request.employee_id, "leave.change_leaveallocationrequest"
        ):
            return forbidden()
        with transaction.atomic():
            allocation = lock_allocation_request(pk)
            if allocation is None or allocation.status == "rejected":
                raise serializers.ValidationError(_("Access Denied."))
            if not may_manage_employee(request.user, allocation.employee_id, "leave.change_leaveallocationrequest"):
                return forbidden()  # checked again on the locked row
            if allocation.status == "approved":
                withdraw_allocation(allocation, lock_balance(allocation.employee_id, allocation.leave_type_id))
            allocation.status = "rejected"
            allocation.save()
        return Response(status=200)


class LeaveRequestBulkApproveDeleteAPIview(APIView):
    permission_classes = [IsAuthenticated]

    def get_leave_requests(self, request):
        from leave.request_access import requested_ids

        try:
            # Whole numbers only (2026-11-14): "abc" used to raise a 500.
            leave_request_ids = requested_ids(request.data.getlist("leave_request_id"))
        except Exception as e:
            raise serializers.ValidationError(
                {"leave_request_id": [_("This field is required")]}
            )
        leave_requests = LeaveRequest.objects.filter(id__in=leave_request_ids).exclude(
            status__in=["rejected", "cancelled", "approved"]
        )
        if leave_requests:
            return leave_requests
        raise serializers.ValidationError(_("Nothing to approve"))

    @manager_permission_required("leave.change_leaverequest")
    def put(self, request):
        from django.db import transaction

        from leave.balance_ops import LeaveBalanceError, deduct_for_request, lock_balance, lock_leave_request

        leave_requests = self.get_leave_requests(request)
        for leave_request in leave_requests:
            problem = _leave_review_problem(request.user, leave_request)
            if problem:
                return forbidden(problem)
            if leave_request.multiple_approvals() and not request.user.is_superuser:
                # Bulk approval would skip the later steps.
                return Response(
                    {"error": "Đơn có nhiều bước duyệt phải duyệt từng đơn."}, status=400
                )
        for pk in [item.pk for item in leave_requests]:
            with transaction.atomic():
                leave_request = lock_leave_request(pk)
                if leave_request is None or leave_request.status != "requested":
                    continue  # decided meanwhile
                balance = lock_balance(leave_request.employee_id, leave_request.leave_type_id)
                if balance is None:
                    continue
                try:
                    deduct_for_request(leave_request, balance)
                except LeaveBalanceError:
                    continue  # not enough days: left for a single decision, as before
                leave_request.status = "approved"
                leave_request.save()
        return Response(status=200)

    @manager_permission_required("leave.delete_leaverequest")
    def delete(self, request):
        from django.db import transaction
        from django.db.models import ProtectedError

        from leave.request_access import REFUSED, delete_waiting_leave

        leave_requests = list(self.get_leave_requests(request))
        if not all(_may_handle(request.user, item, "leave.delete_leaverequest") for item in leave_requests):
            return forbidden()
        # One by one on the locked rows, all or nothing (2026-11-14): the
        # right is checked again, and a leave decided meanwhile is skipped
        # (it used to be deleted by the queryset delete).
        with transaction.atomic():
            for item in leave_requests:
                try:
                    outcome = delete_waiting_leave(
                        item, lambda row: _may_handle(request.user, row, "leave.delete_leaverequest")
                    )
                except ProtectedError:
                    transaction.set_rollback(True)
                    return Response(
                        {"error": "Đơn đang được dữ liệu khác tham chiếu, không thể xóa.", "code": "protected"},
                        status=409,
                    )
                if outcome == REFUSED:
                    transaction.set_rollback(True)
                    return forbidden()
        return Response(status=200)


class EmployeeLeaveAllocationGetCreateAPIView(APIView):
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_class = LeaveAllocationRequestFilter
    queryset = LeaveAllocationRequest.objects.none()  # For drf-yasg schema generation

    def get_queryset(self):
        # Handle schema generation for DRF-YASG
        if getattr(self, "swagger_fake_view", False):
            return LeaveAllocationRequest.objects.none()
        if not self.request.user.is_authenticated:
            return LeaveAllocationRequest.objects.none()
        employee = self.request.user.employee_get
        return employee.leaveallocationrequest_set.all().order_by("-id")

    def get_user(self, request):
        user = request.user
        if isinstance(user, AnonymousUser):
            raise Http404("AnonymousUser")
        return user

    def get(self, request):
        employee = self.get_user(request).employee_get
        allocation_requests = employee.leaveallocationrequest_set.all().order_by("-id")
        filterset = self.filterset_class(request.GET, queryset=allocation_requests)
        paginator = PageNumberPagination()
        field_name = request.GET.get("groupby_field", None)
        if field_name:
            url = request.build_absolute_uri()
            return groupby_queryset(request, url, field_name, filterset.qs)
        page = paginator.paginate_queryset(filterset.qs, request)
        serializer = LeaveAllocationRequestGetSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        data = request.data
        employee_id = self.get_user(request).employee_get.id
        if isinstance(data, QueryDict):
            data = data.dict()
        data["employee_id"] = employee_id
        data["created_by"] = employee_id
        serializer = LeaveAllocationRequestCreateSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(status=200)
        return Response(serializer.errors, status=400)


class EmployeeLeaveAllocationUpdateDeleteAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_allocation_request(self, request, pk):
        user = request.user
        if isinstance(user, AnonymousUser):
            raise Http404("AnonymousUser")
        try:
            allocation_request = LeaveAllocationRequest.objects.get(id=pk)
            if allocation_request.employee_id == user.employee_get:
                return allocation_request
        except LeaveAllocationRequest.DoesNotExist as e:
            raise serializers.ValidationError(e)

    def put(self, request, pk):
        allocation_request = self.get_allocation_request(request, pk)
        if allocation_request.status == "requested":
            data = request.data
            employee_id = request.user.employee_get.id
            if isinstance(data, QueryDict):
                data = data.dict()
            data["employee_id"] = employee_id
            data["created_by"] = employee_id
            serializer = LeaveAllocationRequestSerilaizer(allocation_request, data=data)
            if serializer.is_valid():
                allocation_request = serializer.save()
                return Response(
                    LeaveAllocationRequestGetSerializer(allocation_request).data,
                    status=201,
                )
            return Response(serializer.errors, status=400)
        raise serializers.ValidationError({"error": _("Access Denied..")})
        return Response(status=200)

    def delete(self, request, pk):
        allocation_request = self.get_allocation_request(request, pk)
        if allocation_request.status == "requested":
            allocation_request.delete()
            return Response(status=200)
        raise serializers.ValidationError({"error": _("Access Denied..")})


class LeaveRequestedApprovedCountAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @manager_permission_required("leave.view_leaverequest")
    def get(self, request):
        leave_requests = LeaveRequest.objects.all()
        multiple_approvals = filter_conditional_leave_request(request)
        queryset = (
            filtersubordinates(request, leave_requests, "leave.view_leaverequest")
            | multiple_approvals
        )
        requested = queryset.filter(status="requested").count()
        approved = queryset.filter(status="approved").count()
        data = {"requested": requested, "approved": approved}
        return Response(data, status=200)


class EmployeeAvailableLeaveTypeGetAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get_employee(self, pk):
        try:
            return Employee.objects.get(pk=pk)
        except Employee.DoesNotExist as e:
            raise serializers.ValidationError(e)

    def get(self, request, pk):
        employee = self.get_employee(pk)
        actor = actor_of(request.user)
        from leave.request_access import may_manage_employee

        # HR rights count in the employee's company only (2026-11-14).
        if not ((actor is not None and employee.pk == actor.pk)
                or may_manage_employee(request.user, employee, "leave.view_availableleave")):
            return forbidden()
        available_leave = employee.available_leave.all()
        leave_type_ids = available_leave.values_list("leave_type_id", flat=True)
        leave_types = LeaveType.objects.filter(id__in=leave_type_ids)
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(leave_types, request)
        serializer = LeaveTypeAllGetSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class LeaveTypeGetPermissionCheckAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(
        permission_required("leave.add_leavetype", raise_exception=True),
        name="dispatch",
    )
    def get(self, request):
        return Response(status=200)


class LeaveAllocationGetPermissionCheckAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @manager_permission_required("leave.view_leaveallocationrequest")
    def get(self, request):
        return Response(status=200)


class LeaveRequestGetPermissionCheckAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @manager_permission_required("leave.view_leaverequest")
    def get(self, request):
        return Response(status=200)


class LeaveAssignGetPermissionCheckAPIView(APIView):
    permission_classes = [IsAuthenticated]

    @method_decorator(
        permission_required("leave.view_availableleave", raise_exception=True),
        name="dispatch",
    )
    def get(self, request):
        return Response(status=200)


class LeavePermissionCheckAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        leave_type = LeaveTypeGetPermissionCheckAPIView()
        leave_allocation = LeaveAllocationGetPermissionCheckAPIView()
        leave_request = LeaveRequestGetPermissionCheckAPIView()
        leave_assign = LeaveAssignGetPermissionCheckAPIView()
        perm_list = []
        try:
            if leave_type.get(request).status_code == 200:
                perm_list.append("leave_type")
        except:
            pass
        try:
            if leave_allocation.get(request).status_code == 200:
                perm_list.append("leave_allocation")
        except:
            pass
        try:
            if leave_request.get(request).status_code == 200:
                perm_list.append("leave_request")
                perm_list.append("leave_overview")
        except:
            pass
        try:
            if leave_assign.get(request).status_code == 200:
                perm_list.append("leave_assign")
        except:
            pass
        return Response({"perm_list": perm_list}, status=200)
