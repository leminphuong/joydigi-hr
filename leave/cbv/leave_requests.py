"""
This page handles the cbv of leave requests page
"""

import ast
import contextlib
from datetime import date
from typing import Any

from django.contrib import messages
from django.db.models import Q
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404
from django.urls import resolve, reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _

from base.cbv.penalty import ViewPenaltyList
from base.decorators import manager_can_enter
from base.filters import PenaltyFilter
from base.methods import (
    choosesubordinates,
    filtersubordinates,
    has_export_access,
    is_reportingmanager,
)
from base.models import PenaltyAccounts
from joydigi_views.cbv_methods import hx_request_required, login_required
from joydigi_views.generic.cbv.views import (
    JoydigiDetailedView,
    JoydigiFormView,
    JoydigiListView,
    JoydigiNavView,
    TemplateView,
)
from leave.filters import LeaveRequestFilter
from leave.forms import LeaveRequestCreationForm, LeaveRequestExportForm
from leave.methods import filter_conditional_leave_request
from leave.balance_ops import EDIT_REFUSED, approve_without_review, edit_refusal, save_edited_leave
from leave.monthly_limit import LeaveRuleError
from leave.models import AvailableLeave, LeaveRequest, LeaveType, balance_leave_type
from leave.threading import LeaveMailSendThread
from leave.views import multiple_approvals_check
from notifications.signals import notify


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter("leave.view_leaverequest"), name="dispatch")
class LeaveRequestsView(TemplateView):
    """
    for leave requests page view
    """

    template_name = "cbv/leave_requests/leave_requests_home.html"


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter("leave.view_leaverequest"), name="dispatch")
class LeaveRequestsListView(JoydigiListView):
    """
    Lits view of the page
    """

    quick_export = False

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("request-filter")
        self.view_id = "leaveRequest"
        # No generic bulk edit of "status" (2026-11-15): it wrote the column
        # with one UPDATE — no lock, no deduction or refund, no monthly
        # limit. Approving / rejecting several requests goes through the
        # bulk approve / bulk reject actions, which use the real decisions.
        self.bulk_update_fields = []

    def handle_bulk_submission(self, request):
        instance_ids = request.POST.getlist("instance_ids")
        if (
            instance_ids
            and isinstance(instance_ids[0], str)
            and instance_ids[0].startswith("[")
        ):
            instance_ids = ast.literal_eval(instance_ids[0])
        instance_ids = [int(i) for i in instance_ids if str(i).isdigit()]
        filtered_ids = []
        for request_id in instance_ids:
            leave_request = LeaveRequest.objects.get(id=request_id)
            if leave_request.employee_id.id != request.user.employee_get.id:
                filtered_ids.append(request_id)
        if request.user.is_superuser:
            filtered_ids = instance_ids
        formatted_ids = [str(filtered_ids)]
        request.POST = request.POST.copy()
        request.POST.setlist("instance_ids", formatted_ids)
        return super().handle_bulk_submission(request)

    def get_queryset(self):
        queryset = super().get_queryset()
        data = queryset
        from leave.request_access import VIEW_PERM, within_reach

        queryset = filter_conditional_leave_request(self.request)
        qs = data.filter(id__in=queryset.values_list("id", flat=True))
        data = filtersubordinates(self.request, data, "leave.view_leaverequest") | qs
        # Under "All my companies" the permission is the union of the
        # caller's companies: keep only the companies where they hold it
        # (2026-11-14), as every row's detail does.
        data = within_reach(self.request.user, data, VIEW_PERM, extra_ids=queryset.values_list("id", flat=True))
        return data.distinct().select_related(
            "employee_id", "employee_id__employee_work_info", "leave_type_id"
        )

    filter_class = LeaveRequestFilter
    model = LeaveRequest
    columns = [
        (_("Employee"), "leave_requests_custom_emp_col"),
        (_("Leave Type"), "leave_type_id__name"),
        (_("Start Date"), "start_date"),
        (_("End Date"), "end_date"),
        (_("Requested Days"), "requested_days"),
        (_("Leave Clash"), "leave_clash_col"),
        (_("Status"), "custom_status_col"),
        (_("Comment"), "comment_sidebar"),
        (_("Penalities"), "penality_col"),
    ]

    default_columns = [
        (_("Employee"), "leave_requests_custom_emp_col"),
        (_("Leave Type"), "leave_type_id__name"),
        (_("Start Date"), "start_date"),
        (_("End Date"), "end_date"),
        (_("Requested Days"), "requested_days"),
        (_("Status"), "custom_status_col"),
    ]

    row_attrs = """
        {is_attendance_request_cancelled},
        hx-get='{leave_requests_detail_view}?instance_ids={ordered_ids}'
        hx-target="#genericModalBody"
        data-target="#genericModal"
        data-toggle="oh-modal-toggle"
    """
    sortby_mapping = [
        (_("Employee"), "employee_id__get_full_name", "employee_id__get_avatar"),
        (_("Leave Type"), "leave_type_id__name"),
        (_("Start Date"), "start_date"),
        (_("End Date"), "end_date"),
        (_("Requested Days"), "requested_days"),
        (_("Status"), "status"),
    ]

    action_method = "leave_tab_actions"
    option_method = None

    header_attrs = {
        "leave_requests_custom_emp_col": """ style="width:200px !important;" """,
        "option": """ style="width:200px !important;" """,
        "penality_col": """ style="width:160px !important;" """,
    }

    row_status_indications = [
        (
            "rejected--dot",
            _("Rejected"),
            """
                onclick="
                    $('#applyFilter').closest('form').find('[name=status]').val('rejected');
                    $('[name=canceled]').val('unknown').change();
                    $('[name=approved]').val('unknown').change();
                    $('[name=requested]').val('unknown').change();
                    $('#applyFilter').click();
                "

            """,
        ),
        (
            "cancelled--dot",
            _("Cancelled"),
            """
                onclick="
                    $('#applyFilter').closest('form').find('[name=status]').val('cancelled');
                    $('[name=rejected]').val('unknown').change();
                    $('[name=approved]').val('unknown').change();
                    $('[name=requested]').val('unknown').change();
                    $('#applyFilter').click();
                "

            """,
        ),
        (
            "approved--dot",
            _("Approved"),
            """
                onclick="
                    $('#applyFilter').closest('form').find('[name=status]').val('approved');
                    $('[name=rejected]').val('unknown').change();
                    $('[name=canceled]').val('unknown').change();
                    $('[name=requested]').val('unknown').change();
                    $('#applyFilter').click();
                "

            """,
        ),
        (
            "requested--dot",
            _("Requested"),
            """
                onclick="
                    $('#applyFilter').closest('form').find('[name=status]').val('requested');
                    $('[name=rejected]').val('unknown').change();
                    $('[name=canceled]').val('unknown').change();
                    $('[name=approved]').val('unknown').change();
                    $('#applyFilter').click();
                "

            """,
        ),
    ]
    row_status_class = (
        "rejected-{status} cancelled-{status} requested-{status} approved-{status}"
    )


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter("leave.view_leaverequest"), name="dispatch")
class LeaveRequestsNavView(JoydigiNavView):
    """
    nav bar
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.search_url = reverse("request-filter")
        self.search_in = [
            ("leave_type_id", "Leave Type"),
            ("status", "Status"),
            ("employe_id__employee_work_info__department_id", "Department"),
            ("employee_id__employee_work_info__job_position_id", "Job Position"),
            ("employee_id__employee_work_info__company_id", "Company"),
        ]

        self.actions = [
            {
                "action": _("Bulk Approve"),
                "attrs": """
                    onclick="bulkApproveLeaveRequests();"
                    style="cursor: pointer;"
                """,
            },
        ]
        if has_export_access(self.request, LeaveRequest):
            self.actions.append(
                {
                    "action": _("Export"),
                    "attrs": f"""
                    data-toggle = "oh-modal-toggle"
                    data-target = "#genericModal"
                    hx-target="#genericModalBody"
                    hx-get ="{reverse('leave-requests-nav-export')}"
                    hx-vals='js:{{"has_selection": (JSON.parse(document.getElementById("selectedInstances")?.getAttribute("data-ids")||"[]").length>0)}}'
                    style="cursor: pointer;"
                """,
                }
            )
        self.actions.append(
            {
                "action": _("Delete"),
                "attrs": """
                    onclick="bulkDeleteLeaveRequests();"
                    data-action ="delete"
                    style="cursor: pointer; color:red !important"
                """,
            }
        )

        if self.request.user.has_perm("leave.add_leaverequest") or is_reportingmanager(
            self.request
        ):
            self.create_attrs = f"""
                hx-get="{reverse_lazy("request-creation")}"
                hx-target="#genericModalBody"
                data-target="#genericModal"
                data-toggle="oh-modal-toggle"
            """

    nav_title = _("Leave Requests")
    filter_instance = LeaveRequestFilter()
    filter_body_template = "cbv/leave_requests/filter.html"
    filter_form_context_name = "form"
    search_swap_target = "#listContainer"

    group_by_fields = [
        ("employee_id", _("Employee")),
        ("leave_type_id", _("Leave Type")),
        ("start_date", _("Start Date")),
        ("end_date", _("End Date")),
        ("status", _("Status")),
        ("requested_days", _("Requested Days")),
        (
            "employee_id__employee_work_info__reporting_manager_id",
            _("Reporting Manager"),
        ),
        ("employee_id__employee_work_info__department_id", _("Department")),
        ("employee_id__employee_work_info__job_position_id", _("Job Position")),
        ("employee_id__employee_work_info__employee_type_id", _("Employement Type")),
        ("employee_id__employee_work_info__company_id", _("Company")),
    ]


@method_decorator(login_required, name="dispatch")
@method_decorator(hx_request_required, name="dispatch")
@method_decorator(manager_can_enter("leave.view_leaverequest"), name="dispatch")
class LeaveRequestsExportNav(TemplateView):
    """
    for bulk export
    """

    template_name = "cbv/leave_requests/leave_requests_export.html"

    def get_context_data(self, **kwargs: Any):
        """
        get data for export
        """
        data = LeaveRequest.objects.all()
        export_form = LeaveRequestExportForm
        export_filter = LeaveRequestFilter(queryset=data)
        context = super().get_context_data(**kwargs)
        context["export_form"] = export_form
        context["export_filter"] = export_filter
        context["hide_export_filters"] = self.request.GET.get("has_selection") == "true"
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter("leave.view_leaverequest"), name="dispatch")
class LeaveRequestsDetailView(JoydigiDetailedView):
    """
    detail view of page
    """

    model = LeaveRequest
    detail_view_url_name = "leave-requests-detail-view"
    detail_view_permission = "leave.view_leaverequest"
    title = _("Details")
    header = {
        "title": "leave_requests_detail_subtitle",
        "subtitle": "my_leave_request_detail_subtitle",
        "avatar": "employee_id__get_avatar",
    }
    body = [
        (_("Leave Type"), "leave_type_id"),
        (_("Days"), "requested_days"),
        (_("Start Date"), "start_date"),
        (_("End Date"), "end_date"),
        (_("Created Date"), "requested_date"),
        (_("Created By"), "created_by"),
        ("Yêu cầu hủy", "cancellation_action", True),
        (_("Description"), "description"),
        (_("View attachment"), "attachment_action", True),
    ]
    cols = {
        "description": 12,
        "cancellation_action": 12,
    }

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.action_method = "leave_requests_detail_view_actions"
        if self.request.GET.get("dashboard"):
            self.action_method = None

    def get_object(self, queryset=None):
        """Any reporting manager enters; only a leave the caller may see is
        shown (own, someone they may manage, a step they approve) — else not
        found (2026-11-14)."""
        from leave.request_access import may_view_leave

        instance = super().get_object(queryset)
        if instance is not None and not may_view_leave(self.request.user, instance):
            self.instance = None
            raise Http404("Không tìm thấy đơn nghỉ phép.")
        return instance

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        body = list(self.body)

        if self.instance.multiple_approvals:
            insert_index = 7
            body.insert(
                insert_index,
                (_("Multiple Approvals"), "multiple_approval_action", True),
            )
            self.cols["multiple_approval_action"] = 12

        if self.instance.reject_reason:
            insert_index = 8
            body.insert(
                insert_index, (_("Reason for Rejection"), "rejected_action", True)
            )
            self.cols["rejected_action"] = 12

        context["body"] = body
        return context


@method_decorator(login_required, name="dispatch")
@method_decorator(manager_can_enter("leave.add_leaverequest"), name="dispatch")
class LeaveRequestFormView(JoydigiFormView):
    """
    form view
    """

    model = LeaveRequest
    form_class = LeaveRequestCreationForm
    template_name = "cbv/leave_requests/form/inherit.html"
    new_display_title = _("Leave Request")

    def get_queryset(self):
        """The leave being edited, only when the caller may manage its
        employee (2026-11-14): any reporting manager enters this form, but a
        team lead acts on their own team only. Refused: 403."""
        from base.access import can_manage_employee

        instance = super().get_queryset()
        if instance is not None and not can_manage_employee(
            self.request.user, instance.employee_id, "leave.change_leaverequest", allow_self=True
        ):
            raise PermissionDenied("Bạn không có quyền sửa đơn nghỉ phép của nhân viên này.")
        return instance

    def get_initial(self) -> dict:
        initial = super().get_initial()
        leave_type_id = self.kwargs.get("type_id")
        employee_id = self.kwargs.get("emp_id")
        if leave_type_id and employee_id:
            initial["leave_type_id"] = leave_type_id
            initial["employee_id"] = employee_id
            return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        from employee.models import Employee

        # Determine which employee/leave type the "leave_type_id" queryset
        # should be scoped to, preferring the actually submitted/selected
        # values so a validation-error re-render doesn't revert to defaults.
        employee = None
        leave_type_id = None
        if self.form.is_bound:
            submitted_employee_id = self.form.data.get("employee_id")
            if submitted_employee_id:
                employee = Employee.objects.filter(id=submitted_employee_id).first()
            leave_type_id = self.form.data.get("leave_type_id")
        elif self.form.instance.pk:
            leave_request = LeaveRequest.objects.get(id=self.form.instance.pk)
            employee = leave_request.employee_id
            leave_type_id = leave_request.leave_type_id_id
        else:
            emp_id = self.kwargs.get("emp_id")
            if emp_id:
                employee = Employee.objects.filter(id=emp_id).first()
            leave_type_id = self.kwargs.get("type_id")

        if not employee:
            employee = self.request.user.employee_get

        if employee:
            available_leaves = employee.available_leave.all()
            assigned_leave_types = LeaveType.objects.filter(
                id__in=available_leaves.values_list("leave_type_id", flat=True)
            )
            if (
                leave_type_id
                and not assigned_leave_types.filter(id=leave_type_id).exists()
            ):
                assigned_leave_types = assigned_leave_types | LeaveType.objects.filter(
                    id=leave_type_id
                )
            self.form.fields["leave_type_id"].queryset = assigned_leave_types

        self.form = choosesubordinates(
            self.request, self.form, "leave.add_leaverequest"
        )
        own_employee = getattr(self.request.user, "employee_get", None)
        if (
            own_employee
            and own_employee not in self.form.fields["employee_id"].queryset
        ):
            self.form.fields["employee_id"].queryset = (
                self.form.fields["employee_id"].queryset
                | Employee.objects.filter(id=own_employee.id)
            ).distinct()
        if self.form.instance.pk:
            self.form_class.verbose_name = _("Leave Request")

        context["form"] = self.form
        context["view_id"] = "leaverequest"

        return context

    def form_invalid(self, form: Any) -> HttpResponse:
        if self.form.instance.pk:
            self.form_class.verbose_name = _("Leave Request")
        self.form = form
        return self.render_to_response(self.get_context_data())

    def form_valid(self, form: LeaveRequestCreationForm) -> HttpResponse:
        from base.access import can_manage_employee

        user = self.request.user
        form = self.form_class(
            self.request.POST, self.request.FILES, instance=self.form.instance
        )
        if form.is_valid():
            # The employee the leave is (re)assigned to comes from the POST:
            # it must be someone the caller may manage (2026-11-14).
            target = form.cleaned_data.get("employee_id")
            if target is not None and not can_manage_employee(
                user, target, "leave.add_leaverequest", allow_self=True
            ):
                raise PermissionDenied("Bạn không có quyền tạo hoặc sửa đơn nghỉ phép cho nhân viên này.")
            if form.instance.pk:
                leave_request = form.save(commit=False)
                # Never over a decision committed meanwhile, never writing
                # the decision fields from the form's copy (2026-11-13, P1-2);
                # the right to edit is checked again on the locked row.
                save = save_edited_leave(
                    leave_request,
                    may_edit=lambda current: can_manage_employee(
                        user, current.employee_id, "leave.change_leaverequest", allow_self=True
                    ),
                )
                if not save:
                    messages.error(self.request, edit_refusal(leave_request))

                if save:
                    messages.success(
                        self.request, _("Leave request is updated successfully")
                    )
                    with contextlib.suppress(Exception):
                        notify.send(
                            self.request.user.employee_get,
                            recipient=leave_request.employee_id.employee_work_info.reporting_manager_id.employee_user_id,
                            verb=f"Leave request updated for {leave_request.employee_id}.",
                            verb_ar=f"تم تحديث طلب الإجازة لـ {leave_request.employee_id}.",
                            verb_de=f"Urlaubsantrag aktualisiert für {leave_request.employee_id}.",
                            verb_es=f"Solicitud de permiso actualizada para {leave_request.employee_id}.",
                            verb_fr=f"Demande de congé mise à jour pour {leave_request.employee_id}.",
                            icon="people-circle",
                            redirect=reverse("request-view")
                            + f"?id={leave_request.id}",
                        )

            else:
                leave_request = form.save(commit=False)
                save = True

                auto_approve = leave_request.leave_type_id.require_approval == "no"  # approved under locks once saved (2026-11-13)
                if save:
                    leave_request.created_by = self.request.user.employee_get
                    try:
                        leave_request.save()
                    except LeaveRuleError as refused:  # lost a race under the fund's lock (2026-11-15)
                        messages.error(self.request, " ".join(refused.messages))
                        return self.HttpResponse()
                    if auto_approve:
                        approve_without_review(leave_request)

                    if multiple_approvals_check(leave_request.id):
                        conditional_requests = multiple_approvals_check(
                            leave_request.id
                        )
                        managers = []
                        for manager in conditional_requests["managers"]:
                            managers.append(manager.employee_user_id)
                        with contextlib.suppress(Exception):
                            notify.send(
                                self.request.user.employee_get,
                                recipient=managers[0],
                                verb="You have a new leave request to validate.",
                                verb_ar="لديك طلب إجازة جديد يجب التحقق منه.",
                                verb_de="Sie haben eine neue Urlaubsanfrage zur Validierung.",
                                verb_es="Tiene una nueva solicitud de permiso que debe validar.",
                                verb_fr="Vous avez une nouvelle demande de congé à valider.",
                                icon="people-circle",
                                redirect=f"/leave/request-view?id={leave_request.id}",
                            )

                    mail_thread = LeaveMailSendThread(
                        self.request, leave_request, type="request"
                    )
                    mail_thread.start()
                    messages.success(
                        self.request, _("Leave request created successfully")
                    )
                    with contextlib.suppress(Exception):
                        notify.send(
                            self.request.user.employee_get,
                            recipient=leave_request.employee_id.employee_work_info.reporting_manager_id.employee_user_id,
                            verb=f"New leave request created for {leave_request.employee_id}.",
                            verb_ar=f"تم إنشاء طلب إجازة جديد لـ {leave_request.employee_id}.",
                            verb_de=f"Neuer Urlaubsantrag erstellt für {leave_request.employee_id}.",
                            verb_es=f"Nueva solicitud de permiso creada para {leave_request.employee_id}.",
                            verb_fr=f"Nouvelle demande de congé créée pour {leave_request.employee_id}.",
                            icon="people-circle",
                            redirect=reverse("request-view")
                            + f"?id={leave_request.id}",
                        )

            return self.HttpResponse("")
        return super().form_valid(form)


@method_decorator(login_required, name="dispatch")
class LeaveClashListView(LeaveRequestsListView):
    """
    list view of leave clash col
    """

    def get_queryset(self):
        from leave.request_access import VIEW_PERM, may_view_leave, within_reach

        queryset = JoydigiListView.get_queryset(self)
        pk = self.kwargs.get("pk")
        record = LeaveRequest.objects.filter(id=pk).first()
        # Only for a leave the caller may see, and only the clashing leaves
        # the leave list itself would show them (2026-11-14): this view
        # skipped that scope, so any reporting manager listed the leaves of
        # any department.
        if record is None or not may_view_leave(self.request.user, record):
            return queryset.none()
        steps = filter_conditional_leave_request(self.request).values_list("id", flat=True)
        queryset = within_reach(
            self.request.user,
            filtersubordinates(self.request, queryset, VIEW_PERM) | queryset.filter(id__in=steps),
            VIEW_PERM,
            extra_ids=steps,
        )
        if record.status != "rejected" or record.status != "cancelled":
            queryset = (
                queryset.filter(
                    Q(
                        employee_id__employee_work_info__department_id=record.employee_id.employee_work_info.department_id
                    )
                    | Q(
                        employee_id__employee_work_info__job_position_id=record.employee_id.employee_work_info.job_position_id
                    ),
                    start_date__lte=record.end_date,
                    end_date__gte=record.start_date,
                )
                .exclude(id=pk)
                .exclude(Q(status="cancelled") | Q(status="rejected"))
            )

        return queryset.distinct()

    columns = [
        col
        for col in LeaveRequestsListView.columns
        if col[1] not in ["leave_clash_col", "penality_col", "actions_col"]
    ] + [(_("Clased Due To"), "clashed_due_to")]

    row_status_class = ""
    row_status_indications = None
    bulk_select_option = None
    row_attrs = ""


ViewPenaltyList.columns.extend(
    [
        (_("Leave Type"), "leave_type_id"),
        (_("Minus Days"), "minus_leaves"),
        (_("Deducted FromCFD"), "get_deduct_from_carry_forward"),
    ]
)


class DashboardOnLeaveTable(LeaveRequestsListView):

    columns = [("", "leave_requests_custom_emp_col")]
    bulk_select_option = False
    show_toggle_form = False
    action_method = None
    option_method = None
    sortby_mapping = []

    def get_queryset(self):
        queryset = super().get_queryset()
        today = date.today()

        self.queryset = queryset.filter(
            start_date__lte=today, end_date__gte=today, status="approved"
        )

        return self.queryset

    row_attrs = """
        {is_attendance_request_cancelled},
        hx-get='{leave_requests_detail_view}?instance_ids={ordered_ids}&dashboard=True'
        hx-target="#genericModalBody"
        data-target="#genericModal"
        data-toggle="oh-modal-toggle"
        style="cursor:pointer"
    """
