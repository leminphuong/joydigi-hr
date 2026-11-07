"""``/api/okr/`` endpoints (contract: ``appcheckin/docs/okr_api_contract.md``).

Views only translate HTTP to service calls; scoping and permission checks
live in ``okr.services``.
"""

from __future__ import annotations

from django.db.models import Count, Prefetch, Q
from django.http import FileResponse
from rest_framework import status
from rest_framework.response import Response

from okr.api import render
from okr.api.base import OKRAPIView
from okr.models.choices import ApprovalStatus, CycleStatus, CycleType, Scope
from okr.services import cycles, engagement, notifications, okrs, organization, reports
from okr.services import search as text_search
from okr.services.errors import OKRError, forbidden, validation

ORDERINGS = {
    "updated": ("-updated_at", "-id"),
    "title": ("title", "id"),
    "progress": ("-progress", "id"),
}

LISTED_STATUSES = (
    ApprovalStatus.DRAFT,
    ApprovalStatus.SUBMITTED,
    ApprovalStatus.APPROVED,
    ApprovalStatus.REJECTED,
)


class MeView(OKRAPIView):
    def get(self, request):
        viewer = self.viewer
        active = cycles.active_cycle(viewer.company_id)
        return Response(
            {
                "employee_id": viewer.employee_id,
                "name": viewer.employee.get_full_name(),
                "can_manage": viewer.is_manager,
                "can_approve": viewer.is_manager,
                "is_admin": viewer.is_admin,
                "capabilities": viewer.capabilities(),
                "active_cycle_id": active.pk if active else None,
            }
        )


class CycleListView(OKRAPIView):
    def get(self, request):
        items = (
            cycles.cycles_for(self.viewer.company_id)
            .annotate(objective_count=Count("objectives", filter=Q(objectives__deleted_at__isnull=True)))
            .order_by("-start_date", "-id")
        )
        wanted = request.query_params.get("status")
        if wanted:
            if wanted not in CycleStatus.values:
                raise validation({"status": "Giá trị không hợp lệ."})
            items = items.filter(status=wanted)
        kind = request.query_params.get("cycle_type")
        if kind:
            if kind not in CycleType.values:
                raise validation({"cycle_type": "Giá trị không hợp lệ."})
            items = items.filter(cycle_type=kind)
        return self.paginate(items, render.cycle_with_count)

    def post(self, request):
        cycle = cycles.create_cycle(self.viewer, _body(request))
        return Response(render.cycle(cycle), status=status.HTTP_201_CREATED)


class DashboardView(OKRAPIView):
    def get(self, request):
        cycle = cycles.resolve_cycle(self.viewer, self.int_param("cycle"))
        ctx = render.Context(self.viewer)
        return Response(render.dashboard(ctx, reports.dashboard(self.viewer, cycle)))


class ObjectiveListView(OKRAPIView):
    def get(self, request):
        viewer, params = self.viewer, request.query_params
        items = okrs.objectives_for(viewer.company_id).filter(viewer.objective_visibility())
        owner = params.get("owner")
        if owner == "me":
            # "Của tôi" = what the viewer is assigned: objectives they own, and
            # objectives (still visible to them) in which they own a key result.
            items = items.filter(
                Q(owner_id=viewer.employee_id)
                | Q(key_results__owner_id=viewer.employee_id, key_results__deleted_at__isnull=True)
            ).distinct()
        elif owner:
            # Another person's objectives, still only those the viewer may see.
            items = items.filter(owner_id=self.int_param("owner"))
        team_id = self.int_param("team")
        if team_id:
            items = items.filter(team_id=team_id)
        # F7 (app "Công ty" tab): the visible children of one objective.
        parent_id = self.int_param("parent")
        if parent_id:
            items = items.filter(parent_id=parent_id)
        cycle_id = self.int_param("cycle")
        if cycle_id:
            items = items.filter(cycle_id=cycle_id)
        wanted = params.get("status")
        if wanted:
            if wanted not in LISTED_STATUSES:
                raise validation({"status": "Giá trị không hợp lệ."})
            items = items.filter(approval_status=wanted)
        scope = params.get("scope")
        if scope:
            if scope not in Scope.values:
                raise validation({"scope": "Giá trị không hợp lệ."})
            items = items.filter(scope=scope)
        department_id = self.int_param("department")
        if department_id:
            items = items.filter(department_id=department_id)
        # Vietnamese-aware and independent of the database collation.
        items = text_search.search(items, params.get("q") or "", ["title", "description"])
        order = ORDERINGS.get(params.get("order_by") or "updated")
        if order is None:
            raise validation({"order_by": "Giá trị không hợp lệ."})
        items = items.prefetch_related("key_results").order_by(*order)
        ctx = render.Context(viewer)
        return self.paginate(items, render_page=lambda page: render.objective_summaries(ctx, page))

    def post(self, request):
        objective = okrs.create_objective(self.viewer, _body(request))
        return Response(_objective_detail(self.viewer, objective.pk), status=status.HTTP_201_CREATED)


def _objective_detail(viewer, pk):
    return render.objective_detail(render.Context(viewer), okrs.get_objective(viewer, pk))


class ObjectiveDetailView(OKRAPIView):
    def get(self, request, pk):
        objective = okrs.visible_objective(self.viewer, pk)
        return Response(render.objective_detail(render.Context(self.viewer), objective))

    def put(self, request, pk):
        okrs.visible_objective(self.viewer, pk)
        okrs.update_objective(self.viewer, pk, _body(request))
        return Response(_objective_detail(self.viewer, pk))

    def delete(self, request, pk):
        okrs.visible_objective(self.viewer, pk)
        okrs.delete_objective(self.viewer, pk)
        return Response({"message": "Đã xóa Objective."})


class ObjectiveNormalizeWeightsView(OKRAPIView):
    def post(self, request, pk):
        okrs.visible_objective(self.viewer, pk)
        okrs.normalize_key_result_weights(self.viewer, pk)
        return Response(_objective_detail(self.viewer, pk))


class _ObjectiveActionView(OKRAPIView):
    def respond(self, result):
        objective = okrs.get_objective(self.viewer, result["objective"].pk)
        return Response(
            {
                "message": result["message"],
                "notification_count": result["notification_count"],
                "objective": render.objective_summary(render.Context(self.viewer), objective),
            }
        )


class ObjectiveSubmitView(_ObjectiveActionView):
    def post(self, request, pk):
        return self.respond(okrs.submit_objective(self.viewer, pk))


class ObjectiveApproveView(_ObjectiveActionView):
    def post(self, request, pk):
        return self.respond(okrs.approve_objective(self.viewer, pk))


class ObjectiveRejectView(_ObjectiveActionView):
    def post(self, request, pk):
        reason = request.data.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise validation({"reason": "Vui lòng nhập lý do."})
        return self.respond(okrs.reject_objective(self.viewer, pk, reason or ""))


def _key_result(viewer, pk):
    return render.key_results(render.Context(viewer), [okrs.get_key_result(viewer, pk)])[0]


class KeyResultListView(OKRAPIView):
    def post(self, request):
        data = _body(request)
        if data.get("objective_id"):
            # An objective the viewer cannot see reads as "not found".
            okrs.visible_objective(self.viewer, data.get("objective_id"))
        key_result = okrs.create_key_result(self.viewer, data)
        return Response(_key_result(self.viewer, key_result.pk), status=status.HTTP_201_CREATED)


class KeyResultDetailView(OKRAPIView):
    def get(self, request, pk):
        key_result = okrs.visible_key_result(self.viewer, pk)
        return Response(render.key_results(render.Context(self.viewer), [key_result])[0])

    def put(self, request, pk):
        okrs.visible_key_result(self.viewer, pk)
        okrs.update_key_result(self.viewer, pk, _body(request))
        return Response(_key_result(self.viewer, pk))

    def delete(self, request, pk):
        okrs.visible_key_result(self.viewer, pk)
        okrs.delete_key_result(self.viewer, pk)
        return Response({"message": "Đã xóa Key Result."})


def _body(request) -> dict:
    """The request body as a plain dict (JSON object or form fields)."""
    data = request.data
    if hasattr(data, "dict"):
        return data.dict()
    if not isinstance(data, dict):
        raise validation({"body": "Dữ liệu gửi lên phải là một đối tượng JSON."})
    return data


def _text(data, name: str) -> str:
    value = data.get(name)
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise validation({name: "Giá trị không hợp lệ."})
    return str(value)


class CheckinListView(OKRAPIView):
    def get(self, request):
        items = engagement.list_checkins(self.viewer, self.int_param("key_result", required=True))
        return self.paginate(items, render.checkin)

    def post(self, request):
        data = request.data
        key_result_id = self.body_int("key_result")
        if key_result_id is None:
            raise validation({"key_result": "Thiếu Key Result."})
        result = okrs.checkin(
            self.viewer,
            key_result_id,
            current_value=data.get("current_value"),
            confidence=data.get("confidence"),
            update_text=_text(data, "update_text"),
            blockers=_text(data, "blockers"),
            next_steps=_text(data, "next_steps"),
            client_request_id=_text(data, "client_request_id"),
        )
        ctx = render.Context(self.viewer)
        body = {
            "checkin": render.checkin(result["checkin"]),
            "key_result": render.key_results(ctx, [result["key_result"]])[0],
        }
        return Response(body, status=status.HTTP_201_CREATED if result["created"] else status.HTTP_200_OK)


class EvidenceListView(OKRAPIView):
    def get(self, request):
        items = engagement.list_evidence(self.viewer, self.int_param("key_result", required=True))
        return self.paginate(items, render.evidence)

    def post(self, request):
        data = request.data
        key_result_id = self.body_int("key_result")
        if key_result_id is None:
            raise validation({"key_result": "Thiếu Key Result."})
        upload = request.FILES.get("file")
        kind = _text(data, "evidence_type") or ("file" if upload else "url")
        url = _text(data, "url")
        if kind == "url":
            if not url.strip():
                raise validation({"url": "Vui lòng nhập đường dẫn."})
            upload = None
        elif kind == "file":
            url = ""
        else:
            raise validation({"evidence_type": "Giá trị không hợp lệ."})
        item = engagement.create_evidence(
            self.viewer,
            key_result_id,
            url=url,
            label=_text(data, "label"),
            upload=upload,
            checkin_id=self.body_int("checkin"),
        )
        return Response(render.evidence(item), status=status.HTTP_201_CREATED)


class EvidenceDownloadView(OKRAPIView):
    def get(self, request, pk):
        item = engagement.evidence_for_download(self.viewer, pk)
        try:
            handle = item.file.open("rb")
        except (FileNotFoundError, OSError):
            raise OKRError("file_missing", "Không tìm thấy tệp bằng chứng.", 404)
        response = FileResponse(
            handle,
            as_attachment=True,
            filename=item.original_name or item.label,
            content_type=item.content_type or "application/octet-stream",
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


class CommentListView(OKRAPIView):
    def get(self, request):
        items = engagement.list_comments(
            self.viewer, objective_id=self.int_param("objective"), key_result_id=self.int_param("key_result")
        )
        return self.paginate(items, render.comment)

    def post(self, request):
        item = engagement.create_comment(
            self.viewer,
            objective_id=self.body_int("objective"),
            key_result_id=self.body_int("key_result"),
            content=_text(request.data, "content"),
            parent_id=self.body_int("parent"),
        )
        return Response(render.comment(item), status=status.HTTP_201_CREATED)


class ActivityListView(OKRAPIView):
    def get(self, request):
        items = engagement.list_activity(
            self.viewer, objective_id=self.int_param("objective"), key_result_id=self.int_param("key_result")
        )
        return self.paginate(items, render.activity)


class ApprovalListView(OKRAPIView):
    def get(self, request):
        if not self.viewer.is_manager:
            raise forbidden("Chỉ người duyệt OKR mới xem được hàng chờ duyệt.")
        ctx = render.Context(self.viewer)
        return self.paginate(reports.approval_queue(self.viewer), lambda item: render.approval_item(ctx, item))


class TeamReportView(OKRAPIView):
    def get(self, request):
        if not self.viewer.is_manager:
            raise forbidden("Chỉ quản lý OKR mới xem được báo cáo nhóm.")
        cycle = cycles.resolve_cycle(self.viewer, self.int_param("cycle"))
        return Response(render.team_report(reports.team_report(self.viewer, cycle)))


class NotificationListView(OKRAPIView):
    def get(self, request):
        okrs.sync_pending_approval_notifications(self.viewer)
        items = notifications.for_viewer(self.viewer).select_related("actor").order_by("-created_at", "-id")

        def render_page(page):
            availability = notifications.reference_availability(self.viewer, page)
            return [render.notification(item, availability.get(item.pk)) for item in page]

        response = self.paginate(items, render_page=render_page)
        # Counted after the page settled stale approval requests.
        response.data["unread_count"] = notifications.unread_count(self.viewer)
        return response


class NotificationReadView(OKRAPIView):
    def post(self, request, pk):
        notifications.mark_read(self.viewer, pk)
        return Response({"message": "Đã đọc thông báo."})


class NotificationReadAllView(OKRAPIView):
    def post(self, request):
        count = notifications.mark_all_read(self.viewer)
        return Response({"message": "Đã đọc tất cả thông báo.", "count": count})


# -- web and administration ---------------------------------------------------------


class CycleDetailView(OKRAPIView):
    def get(self, request, pk):
        return Response(render.cycle(cycles.get_cycle(self.viewer, pk)))

    def put(self, request, pk):
        return Response(render.cycle(cycles.update_cycle(self.viewer, pk, _body(request))))


class CycleStatusView(OKRAPIView):
    def post(self, request, pk):
        wanted = request.data.get("status")
        return Response(render.cycle(cycles.change_status(self.viewer, pk, wanted if isinstance(wanted, str) else "")))


class CycleCloneView(OKRAPIView):
    def post(self, request, pk):
        result = cycles.clone_cycle(self.viewer, pk, self.body_int("destination_cycle_id"))
        return Response({"message": "Đã sao chép OKR sang chu kỳ mới.", **result})


class PeopleView(OKRAPIView):
    def get(self, request):
        rows = organization.people(
            self.viewer,
            search=request.query_params.get("q") or "",
            department_id=self.int_param("department"),
        )
        return self.paginate(rows, render.person_row)


class DepartmentListView(OKRAPIView):
    def get(self, request):
        return self.paginate(organization.departments(self.viewer), render.department)


class DepartmentManagerView(OKRAPIView):
    def put(self, request, pk):
        department = organization.set_department_manager(self.viewer, pk, self.body_int("manager_id"))
        return Response(render.department(department))


class TeamListView(OKRAPIView):
    def get(self, request):
        # Members come with each row: the web admin table lists them, as JOYOKR did.
        rows = organization.teams(self.viewer, request.query_params.get("status") or "").prefetch_related(
            Prefetch("memberships", queryset=organization.member_order(), to_attr="ordered_members")
        )
        return self.paginate(rows, lambda team: render.team(team, team.ordered_members))

    def post(self, request):
        team = organization.save_team(self.viewer, _body(request))
        return Response(render.team(team, organization.team_members(team)), status=status.HTTP_201_CREATED)


class TeamDetailView(OKRAPIView):
    def get(self, request, pk):
        team = organization.get_team(self.viewer, pk)
        return Response(render.team(team, organization.team_members(team)))

    def put(self, request, pk):
        team = organization.save_team(self.viewer, _body(request), team_id=pk)
        return Response(render.team(team, organization.team_members(team)))


class TeamStatusView(OKRAPIView):
    def post(self, request, pk):
        wanted = request.data.get("status")
        team = organization.set_team_status(self.viewer, pk, wanted if isinstance(wanted, str) else "")
        return Response(render.team(team, organization.team_members(team)))


class TeamMembersView(OKRAPIView):
    def put(self, request, pk):
        team = organization.set_team_members(self.viewer, pk, request.data.get("employee_ids"))
        return Response(render.team(team, organization.team_members(team)))


class RoleListView(OKRAPIView):
    def get(self, request):
        return self.paginate(organization.roles(self.viewer), render.role)

    def post(self, request):
        role = request.data.get("role")
        granted = organization.grant_role(
            self.viewer, self.body_int("employee_id"), role if isinstance(role, str) else ""
        )
        return Response(render.role(granted), status=status.HTTP_201_CREATED)


class RoleDetailView(OKRAPIView):
    def delete(self, request, pk):
        organization.revoke_role(self.viewer, pk)
        return Response({"message": "Đã gỡ vai trò."})


class RoleHistoryView(OKRAPIView):
    def get(self, request):
        return self.paginate(organization.role_history(self.viewer), render.role_event)


class SettingsView(OKRAPIView):
    def get(self, request):
        return Response(render.settings(organization.get_settings(self.viewer.company_id)))

    def put(self, request):
        return Response(render.settings(organization.update_settings(self.viewer, _body(request))))


HEALTH_VALUES = ("on_track", "at_risk", "off_track", "completed")


class ReportingView(OKRAPIView):
    """Shared by the dashboard, the report, the tree and check-in management
    (W4): one way to pick the cycle and one set of filters, so tables,
    charts and the CSV built from them always agree."""

    def chosen_cycle(self, *, fallback_latest: bool = False):
        requested = self.int_param("cycle")
        cycle = cycles.resolve_cycle(self.viewer, requested, fallback_latest=fallback_latest)
        return cycle, cycles.cycle_selection(self.viewer, cycle, requested)

    def objective_filters(self) -> dict:
        scope = self.request.query_params.get("scope")
        if scope and scope not in Scope.values:
            raise validation({"scope": "Giá trị không hợp lệ."})
        return {
            "scope": scope,
            "department_id": self.int_param("department"),
            "team_id": self.int_param("team"),
            "owner_id": self.int_param("owner"),
        }

    def choice(self, name: str, allowed):
        value = self.request.query_params.get(name) or None
        if value and value not in allowed:
            raise validation({name: "Giá trị không hợp lệ."})
        return value


class CompanyDashboardView(ReportingView):
    def get(self, request):
        cycle, selection = self.chosen_cycle()
        data = reports.company_dashboard(self.viewer, cycle, self.objective_filters())
        data["cycle_selection"] = selection
        return Response(render.company_dashboard(render.Context(self.viewer), data))


class ReportView(ReportingView):
    def get(self, request):
        cycle, selection = self.chosen_cycle()
        filters = self.objective_filters()
        data = reports.report(self.viewer, cycle, filters, health=self.choice("health", HEALTH_VALUES))
        data["cycle_selection"] = selection
        data["filters"] = {**filters, "health": self.choice("health", HEALTH_VALUES)}
        return Response(render.report(render.Context(self.viewer), data))


class TreeView(ReportingView):
    def get(self, request):
        cycle, selection = self.chosen_cycle(fallback_latest=True)
        data = reports.tree(self.viewer, cycle)
        data["cycle_selection"] = selection
        return Response(render.tree(render.Context(self.viewer), data))


class CheckinQueueView(OKRAPIView):
    def get(self, request):
        cycle_id = self.int_param("cycle")
        cycle = cycles.get_cycle(self.viewer, cycle_id) if cycle_id else None
        ctx = render.Context(self.viewer)
        return self.paginate(
            reports.checkin_queue(self.viewer, cycle),
            render_page=lambda page: render.checkin_queue_rows(ctx, page),
        )


class CheckinManagementView(ReportingView):
    def get(self, request):
        cycle, selection = self.chosen_cycle(fallback_latest=True)
        filters = {
            "department_id": self.int_param("department"),
            "team_id": self.int_param("team"),
            "owner_id": self.int_param("owner"),
            "status": self.choice("status", reports.CHECKIN_STATUSES),
        }
        data = reports.checkin_management(self.viewer, cycle, filters)
        data["cycle_selection"] = selection
        return Response(render.checkin_management(data))
