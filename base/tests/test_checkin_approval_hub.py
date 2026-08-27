from datetime import date, timedelta

from django.contrib.auth.models import Group
from django.test import TestCase
from django.template.loader import render_to_string
from django.urls import reverse

from base.dashboard import _get_setup_checklist_context
from base.models import (
    CheckInLocation,
    CompanyGroupAssignment,
    EmployeeShift,
    OfficeWifi,
    Roster,
    ShiftRequest,
)
from base.roles import LEADER_ROLE
from attendance.models import (
    AttendanceConflictResolution,
    AttendanceExplanationRequest,
    AttendanceLateEarlyRequest,
    OvertimeRequest,
    RemoteWorkRequest,
)
from joydigi.testkit import make_company, make_employee, make_user
from leave.models import LeaveRequest, LeaveType


class CheckInApprovalHubTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Approval Hub Co")
        cls.admin_user = make_user("hub_admin", is_superuser=True)
        cls.admin = make_employee(
            company=cls.company,
            email="hub-admin@test.joydigi",
            user=cls.admin_user,
        )
        cls.leader = make_employee(
            company=cls.company,
            email="hub-leader@test.joydigi",
        )
        cls.worker = make_employee(
            company=cls.company,
            email="hub-worker@test.joydigi",
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])
        cls.leave_type = LeaveType.objects.create(
            company_id=cls.company,
            name="Nghỉ phép năm",
            payment="paid",
            total_days=12,
        )
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        cls.shift.company_id.add(cls.company)

    def test_admin_sees_request_while_first_step_waits_for_leader(self):
        start = date.today() + timedelta(days=2)
        request = LeaveRequest.objects.create(
            employee_id=self.worker,
            leave_type_id=self.leave_type,
            start_date=start,
            end_date=start + timedelta(days=1),
            start_date_breakdown="full_day",
            end_date_breakdown="full_day",
            description="Kiểm tra quy trình duyệt",
            status="requested",
        )

        first_step = request.leaverequestconditionapproval_set.order_by(
            "sequence"
        ).first()
        self.assertEqual(first_step.manager_id_id, self.leader.id)

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 200)
        visible = response.context["leave_requests"]
        self.assertEqual([item.id for item in visible], [request.id])
        self.assertFalse(visible[0].can_act)
        self.assertEqual(visible[0].current_approval.manager_id_id, self.leader.id)

    def test_admin_sees_processed_request_in_approval_hub(self):
        start = date.today() + timedelta(days=5)
        processed = LeaveRequest.objects.create(
            employee_id=self.worker,
            leave_type_id=self.leave_type,
            start_date=start,
            end_date=start,
            start_date_breakdown="full_day",
            end_date_breakdown="full_day",
            description="Đơn đã xử lý",
            status="approved",
        )

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            processed.id,
            [item.id for item in response.context["leave_requests"]],
        )
        self.assertContains(response, "Đã duyệt")

    def test_full_leave_request_page_is_available(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("request-view"))

        self.assertEqual(response.status_code, 200)

    def test_checkin_and_holiday_pages_use_standalone_layout(self):
        self.client.force_login(self.admin_user)

        for url_name in (
            "checkin-settings",
            "holiday-view",
            "holidays-view",
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(
                    reverse(url_name),
                    HTTP_HX_REQUEST="true",
                    HTTP_HX_SIDEBAR_NAV="true",
                )
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "index.html")
                self.assertTemplateNotUsed(response, "settings.html")

        response = self.client.get(
            reverse("attendance-rule-view"),
            HTTP_HX_REQUEST="true",
            HTTP_HX_SIDEBAR_NAV="true",
        )
        self.assertContains(response, 'id="settingsContainer"')

        response = self.client.get(reverse("checkin-settings"))
        self.assertContains(response, reverse("holiday-view"))

        response = self.client.get(reverse("attendance-rule-view"))
        self.assertNotContains(response, "Geofencing")
        self.assertNotContains(response, "Hàng rào địa lý")

        for url_name in ("holiday-view", "holidays-view"):
            response = self.client.get(reverse(url_name))
            self.assertTemplateUsed(response, "base/settings/holidays.html")

    def test_checkin_wifi_actions_update_inline_without_page_reload(self):
        self.client.force_login(self.admin_user)

        create_response = self.client.post(
            reverse("checkin-settings"),
            {
                "action": "wifi",
                "object_id": "",
                "name": "Wi-Fi tầng 5",
                "ssid": "JOYDIGI-FLOOR-5",
                "bssid": "AA:BB:CC:DD:EE:51",
                "is_active": "on",
            },
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="checkinSettingsContent",
        )
        self.assertEqual(create_response.status_code, 200)
        self.assertContains(create_response, "Đã lưu Wi-Fi văn phòng.")
        wifi = OfficeWifi.objects.get(company_id=self.company, ssid="JOYDIGI-FLOOR-5")

        edit_response = self.client.get(
            f"{reverse('checkin-settings')}?edit_wifi={wifi.pk}",
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="checkinSettingsContent",
        )
        self.assertEqual(edit_response.status_code, 200)
        self.assertContains(edit_response, f'value="{wifi.pk}"')

        delete_response = self.client.post(
            reverse("delete-office-wifi", kwargs={"pk": wifi.pk}),
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="checkinSettingsContent",
        )
        self.assertEqual(delete_response.status_code, 200)
        self.assertContains(delete_response, "Đã xóa Wi-Fi văn phòng.")
        self.assertFalse(OfficeWifi.objects.filter(pk=wifi.pk).exists())

    def test_checkin_location_and_holiday_are_separate_main_menu_buttons(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("dashboard"))
        menu_html = render_to_string(
            "joydigi_theme/components/sidebar/top_menu.html",
            request=response.wsgi_request,
        )

        self.assertIn(
            f'href="{reverse("checkin-settings")}"',
            menu_html,
        )
        self.assertIn('data-menu="Địa điểm và Wifi"', menu_html)
        self.assertIn(f'href="{reverse("holiday-view")}"', menu_html)
        self.assertIn('data-menu="Ngày nghỉ lễ"', menu_html)
        self.assertIn(f'href="{reverse("settings")}"', menu_html)
        self.assertIn('data-menu="Cài đặt"', menu_html)
        self.assertIn(f'href="{reverse("user-activity-log")}"', menu_html)
        self.assertContains(response, reverse("load-demo-database"))

        mobile_menu_html = render_to_string(
            "joydigi_theme/components/mobile_bottom_menu.html",
            request=response.wsgi_request,
        )
        for url_name in (
            "dashboard",
            "today-attendance",
            "approval-hub",
            "attendance-monthly-summary",
            "roster-home",
            "employee-view",
            "bulletin",
            "checkin-settings",
            "holiday-view",
            "settings",
            "user-activity-log",
        ):
            with self.subTest(mobile_url_name=url_name):
                self.assertIn(reverse(url_name), mobile_menu_html)

    def test_checkin_settings_create_location_and_multiple_wifi_networks(self):
        self.client.force_login(self.admin_user)
        url = reverse("checkin-settings")

        response = self.client.post(
            url,
            {
                "action": "location",
                "object_id": "",
                "name": "Văn phòng chính",
                "latitude": "10.776900",
                "longitude": "106.700900",
                "radius_meters": "200",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, url)
        self.assertTrue(
            CheckInLocation.objects.filter(
                company_id=self.company, name="Văn phòng chính"
            ).exists()
        )

        for number in (1, 2):
            with self.subTest(number=number):
                response = self.client.post(
                    url,
                    {
                        "action": "wifi",
                        "object_id": "",
                        "name": f"Wifi văn phòng {number}",
                        "ssid": f"JoyDigi-Office-{number}",
                        "bssid": "",
                        "is_active": "on",
                    },
                )
                self.assertRedirects(response, url)

        self.assertEqual(
            OfficeWifi.objects.filter(company_id=self.company).count(),
            2,
        )

        response = self.client.get(f"{url}?edit_location=&edit_wifi=")
        self.assertEqual(response.status_code, 200)

    def test_roster_saves_for_employee_without_department(self):
        self.client.force_login(self.admin_user)
        roster_date = date.today() + timedelta(days=3)

        response = self.client.post(
            reverse("roster-cell-update"),
            {
                "employee": self.worker.pk,
                "date": roster_date.isoformat(),
                "department": "",
                "shift": self.shift.pk,
                "notes": "Ca kiểm tra",
            },
        )

        self.assertEqual(response.status_code, 200)
        entry = Roster.objects.entire().get(
            employee=self.worker,
            date=roster_date,
        )
        self.assertEqual(entry.shift_id, self.shift.pk)
        self.assertIsNone(entry.department_id)

    def test_monthly_attendance_conflict_resolution_saves_with_company_filter(self):
        self.client.force_login(self.admin_user)
        conflict_date = date.today() - timedelta(days=1)

        response = self.client.post(
            reverse("attendance-monthly-summary-conflict-resolve"),
            {
                "employee_id": self.worker.pk,
                "date": conflict_date.isoformat(),
                "from_date": conflict_date.replace(day=1).isoformat(),
                "to_date": conflict_date.isoformat(),
                "resolution": "full_present",
                "conflict_type": "attendance",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        resolution = AttendanceConflictResolution.objects.entire().get(
            employee_id=self.worker,
            date=conflict_date,
        )
        self.assertEqual(resolution.resolution, "full_present")

    def test_monthly_attendance_page_is_available(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("attendance-monthly-summary"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bảng chấm công tháng")

    def test_setup_checklist_does_not_show_mail_server(self):
        request = self.client.request().wsgi_request
        request.user = self.admin_user
        request.GET = {"preview_checklist": "1"}
        context = _get_setup_checklist_context(request)

        self.assertNotIn(
            "mail_server",
            [step["key"] for step in context["setup_steps"]],
        )


class CheckInApprovalHubShiftRequestTests(TestCase):
    """Phase UI-4B.3: /duyet-don/ 'Đơn đổi ca' section — integration
    only, reusing the existing shift-request-approve/shift-request-cancel
    views and the existing _visible_employees() scoping. No new
    ShiftRequest business logic is introduced or tested here."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Shift Hub Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("shift_hub_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="shift-hub-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("shift_hub_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="shift-hub-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_leader_user = make_user("shift_hub_other_leader")
        cls.other_leader = make_employee(
            company=cls.company,
            email="shift-hub-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user, company=cls.company, group=leader_group
        )
        cls.unrelated_worker = make_employee(
            company=cls.company,
            email="shift-hub-unrelated@test.joydigi",
        )
        cls.unrelated_worker.employee_work_info.reporting_manager_id = (
            cls.other_leader
        )
        cls.unrelated_worker.employee_work_info.save(
            update_fields=["reporting_manager_id"]
        )

        cls.previous_shift = EmployeeShift.objects.create(
            employee_shift="Ca hành chính"
        )
        cls.previous_shift.company_id.add(cls.company)
        cls.requested_shift = EmployeeShift.objects.create(employee_shift="Ca đêm")
        cls.requested_shift.company_id.add(cls.company)
        cls.worker.employee_work_info.shift_id = cls.previous_shift
        cls.worker.employee_work_info.save(update_fields=["shift_id"])

    def _make_shift_request(self, employee, **overrides):
        from datetime import date, timedelta

        defaults = {
            "employee_id": employee,
            "shift_id": self.requested_shift,
            "previous_shift_id": self.previous_shift,
            "requested_date": date.today() + timedelta(days=1),
            "requested_till": date.today() + timedelta(days=5),
            "description": "Hỗ trợ dự án",
            "is_permanent_shift": False,
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return ShiftRequest.objects.create(**defaults)

    def test_visible_manager_sees_pending_shift_request(self):
        pending = self._make_shift_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            pending.id,
            [item.id for item in response.context["shift_requests"]],
        )
        self.assertContains(response, "Đơn đổi ca")

    def test_unrelated_manager_does_not_see_it(self):
        self._make_shift_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["shift_requests"]), [])

    def test_approved_requests_still_appear_with_approved_status(self):
        # Phase UI-4E.1A: the hub is a recent-history summary, not a
        # pending-only queue — processed requests stay visible.
        approved = self._make_shift_request(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            approved.id,
            [item.id for item in response.context["shift_requests"]],
        )
        self.assertContains(response, "Đã duyệt")

    def test_canceled_requests_still_appear_with_rejected_status(self):
        canceled = self._make_shift_request(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            canceled.id,
            [item.id for item in response.context["shift_requests"]],
        )
        self.assertContains(response, "Đã từ chối")

    def test_inactive_employee_requests_are_not_in_pending_section(self):
        pending = self._make_shift_request(self.worker)
        self.worker.is_active = False
        self.worker.save(update_fields=["is_active"])

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertNotIn(
            pending.id,
            [item.id for item in response.context["shift_requests"]],
        )

    def test_approve_action_uses_the_existing_approval_route_and_logic(self):
        # requested_date=today so the window is currently active — the
        # real shift_request_approve() view applies the shift change
        # immediately in that case (see base/views.py), which is what
        # this test verifies actually ran.
        pending = self._make_shift_request(
            self.worker,
            requested_date=date.today(),
            requested_till=date.today() + timedelta(days=5),
        )

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response, reverse("shift-request-approve", kwargs={"id": pending.id})
        )

        approve_response = self.client.post(
            reverse("shift-request-approve", kwargs={"id": pending.id}),
            HTTP_REFERER=reverse("approval-hub"),
        )

        self.assertRedirects(approve_response, reverse("approval-hub"))
        pending.refresh_from_db()
        # Proves the real shift_request_approve business logic ran (not
        # a hand-rolled `request.approved = True` in the hub view):
        # the immediate shift change it performs for a currently-active
        # window is visible on the employee's work info too.
        self.assertTrue(pending.approved)
        self.worker.employee_work_info.refresh_from_db()
        self.assertEqual(
            self.worker.employee_work_info.shift_id_id, self.requested_shift.id
        )

    def test_reject_action_uses_the_existing_cancel_route(self):
        pending = self._make_shift_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response, reverse("shift-request-cancel", kwargs={"id": pending.id})
        )

        cancel_response = self.client.post(
            reverse("shift-request-cancel", kwargs={"id": pending.id}),
            HTTP_REFERER=reverse("approval-hub"),
        )

        self.assertRedirects(cancel_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.canceled)
        self.assertFalse(pending.approved)

    def test_leave_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin phép")
        self.assertIn("leave_requests", response.context)

    def test_outside_radius_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Chấm công ngoài bán kính")
        self.assertIn("outside_requests", response.context)


class CheckInApprovalHubLateEarlyRequestTests(TestCase):
    """Phase UI-4C.1: /duyet-don/ 'Đơn xin đi muộn / về sớm' section —
    a brand-new admin approve/reject action pair (no pre-existing route
    to reuse), scoped through the same _visible_employees(request) used
    by every other section on this page."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Late Early Hub Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("late_early_hub_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="late-early-hub-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("late_early_hub_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="late-early-hub-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_company = make_company("Late Early Other Co")
        cls.other_leader_user = make_user("late_early_hub_other_leader")
        cls.other_leader = make_employee(
            company=cls.other_company,
            email="late-early-hub-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user, company=cls.other_company, group=leader_group
        )
        cls.other_worker = make_employee(
            company=cls.other_company,
            email="late-early-hub-other-worker@test.joydigi",
        )
        cls.other_worker.employee_work_info.reporting_manager_id = cls.other_leader
        cls.other_worker.employee_work_info.save(update_fields=["reporting_manager_id"])

    def _make_request(self, employee, **overrides):
        defaults = {
            "employee_id": employee,
            "request_type": "late_arrival",
            "request_date": date.today() + timedelta(days=1),
            "requested_time": "09:30",
            "description": "Đưa con đi khám bệnh",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return AttendanceLateEarlyRequest.objects.create(**defaults)

    def test_visible_manager_sees_pending_request(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            pending.id,
            [item.id for item in response.context["late_early_requests"]],
        )
        self.assertContains(response, "Đơn xin đi muộn / về sớm")

    def test_unrelated_manager_does_not_see_it(self):
        self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(list(response.context["late_early_requests"]), [])

    def test_approved_requests_still_appear_with_approved_status(self):
        # Phase UI-4E.1A: the hub is a recent-history summary, not a
        # pending-only queue — processed requests stay visible.
        approved = self._make_request(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            approved.id,
            [item.id for item in response.context["late_early_requests"]],
        )
        self.assertContains(response, "Đã duyệt")

    def test_canceled_requests_still_appear_with_rejected_status(self):
        canceled = self._make_request(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            canceled.id,
            [item.id for item in response.context["late_early_requests"]],
        )
        self.assertContains(response, "Đã từ chối")

    def test_approve_action_uses_dedicated_route_and_requires_permission(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response,
            reverse("late-early-request-approve", kwargs={"id": pending.id}),
        )

        approve_response = self.client.post(
            reverse("late-early-request-approve", kwargs={"id": pending.id})
        )

        self.assertRedirects(approve_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.approved)
        self.assertFalse(pending.canceled)

    def test_reject_action_uses_dedicated_route(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response,
            reverse("late-early-request-reject", kwargs={"id": pending.id}),
        )

        reject_response = self.client.post(
            reverse("late-early-request-reject", kwargs={"id": pending.id})
        )

        self.assertRedirects(reject_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.canceled)
        self.assertFalse(pending.approved)

    def test_cross_company_manager_cannot_approve(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.post(
            reverse("late-early-request-approve", kwargs={"id": pending.id})
        )

        self.assertRedirects(response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertFalse(pending.approved)

    def test_cross_company_manager_cannot_reject(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.post(
            reverse("late-early-request-reject", kwargs={"id": pending.id})
        )

        self.assertRedirects(response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertFalse(pending.canceled)

    def test_ordinary_employee_cannot_reach_the_hub_at_all(self):
        # checkin_leader_required already gates the whole page — an
        # ordinary employee (no leader/admin role) never reaches any
        # approve/reject action for any section, including this one.
        self.client.force_login(self.worker_user)

        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 403)

    def test_shift_request_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn đổi ca")
        self.assertIn("shift_requests", response.context)


class CheckInApprovalHubOvertimeRequestTests(TestCase):
    """Phase UI-4E.1: /duyet-don/ 'Đơn xin làm thêm giờ (OT)' section —
    a brand-new admin approve/reject action pair (no pre-existing route
    to reuse — the existing overtime-approve/<pk> endpoint approves a
    real Attendance row's computed OT, not a pre-request), scoped
    through the same _visible_employees(request) used by every other
    section on this page."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Overtime Hub Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("overtime_hub_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="overtime-hub-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("overtime_hub_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="overtime-hub-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_company = make_company("Overtime Other Co")
        cls.other_leader_user = make_user("overtime_hub_other_leader")
        cls.other_leader = make_employee(
            company=cls.other_company,
            email="overtime-hub-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user, company=cls.other_company, group=leader_group
        )
        cls.other_worker = make_employee(
            company=cls.other_company,
            email="overtime-hub-other-worker@test.joydigi",
        )
        cls.other_worker.employee_work_info.reporting_manager_id = cls.other_leader
        cls.other_worker.employee_work_info.save(
            update_fields=["reporting_manager_id"]
        )

    def _make_request(self, employee, **overrides):
        defaults = {
            "employee_id": employee,
            "request_date": date.today() + timedelta(days=1),
            "start_time": "18:00",
            "end_time": "21:00",
            "description": "Hoàn thành bản phát hành",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return OvertimeRequest.objects.create(**defaults)

    def test_visible_manager_sees_pending_request(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            pending.id,
            [item.id for item in response.context["overtime_requests"]],
        )
        self.assertContains(response, "Đơn xin làm thêm giờ (OT)")

    def test_unrelated_manager_does_not_see_it(self):
        self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(list(response.context["overtime_requests"]), [])

    def test_approved_requests_still_appear_with_approved_status(self):
        # Phase UI-4E.1A: the hub is a recent-history summary, not a
        # pending-only queue — processed requests stay visible.
        approved = self._make_request(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            approved.id,
            [item.id for item in response.context["overtime_requests"]],
        )
        self.assertContains(response, "Đã duyệt")

    def test_canceled_requests_still_appear_with_rejected_status(self):
        canceled = self._make_request(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            canceled.id,
            [item.id for item in response.context["overtime_requests"]],
        )
        self.assertContains(response, "Đã từ chối")

    def test_approve_action_uses_dedicated_route_and_requires_permission(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response,
            reverse("overtime-request-approve", kwargs={"id": pending.id}),
        )

        approve_response = self.client.post(
            reverse("overtime-request-approve", kwargs={"id": pending.id})
        )

        self.assertRedirects(approve_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.approved)
        self.assertFalse(pending.canceled)

    def test_approve_does_not_touch_attendance_fields(self):
        from attendance.models import Attendance

        before = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        self.client.post(
            reverse("overtime-request-approve", kwargs={"id": pending.id})
        )

        after = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        self.assertEqual(before, after)

    def test_reject_action_uses_dedicated_route(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response,
            reverse("overtime-request-reject", kwargs={"id": pending.id}),
        )

        reject_response = self.client.post(
            reverse("overtime-request-reject", kwargs={"id": pending.id})
        )

        self.assertRedirects(reject_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.canceled)
        self.assertFalse(pending.approved)

    def test_cross_company_manager_cannot_approve(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.post(
            reverse("overtime-request-approve", kwargs={"id": pending.id})
        )

        self.assertRedirects(response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertFalse(pending.approved)

    def test_cross_company_manager_cannot_reject(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.post(
            reverse("overtime-request-reject", kwargs={"id": pending.id})
        )

        self.assertRedirects(response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertFalse(pending.canceled)

    def test_ordinary_employee_cannot_reach_the_hub_at_all(self):
        self.client.force_login(self.worker_user)

        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 403)

    def test_leave_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin phép")
        self.assertIn("leave_requests", response.context)

    def test_shift_request_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn đổi ca")
        self.assertIn("shift_requests", response.context)

    def test_late_early_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin đi muộn / về sớm")
        self.assertIn("late_early_requests", response.context)

    def test_every_section_has_a_full_review_page_link(self):
        # Phase UI-4E.1A Step 4: every request type section on the hub
        # must link to a real "Mở trang duyệt đầy đủ" URL.
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, reverse("request-view"))
        self.assertContains(response, reverse("request-attendance-view"))
        self.assertContains(response, reverse("shift-request-view"))
        self.assertContains(response, reverse("late-early-request-list"))
        self.assertContains(response, reverse("overtime-request-list"))
        # Every occurrence is the literal, unfiltered full-page URL —
        # the pre-Phase-UI-4E.1A outside-radius link forced
        # `?approved=false`, hiding processed records; that must be gone.
        self.assertNotContains(response, "request-attendance-view/?approved=false")


class LateEarlyRequestFullPageTests(TestCase):
    """Phase UI-4E.1A: /duyet-don/di-muon-ve-som/ — the full review page
    for AttendanceLateEarlyRequest, showing every status (not just
    pending), scoped through the same _visible_employees(request) as
    the rest of the hub."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Late Early Full Page Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("le_fullpage_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="le-fullpage-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("le_fullpage_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="le-fullpage-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_company = make_company("Late Early Full Page Other Co")
        cls.other_leader_user = make_user("le_fullpage_other_leader")
        cls.other_leader = make_employee(
            company=cls.other_company,
            email="le-fullpage-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user, company=cls.other_company, group=leader_group
        )
        cls.other_worker = make_employee(
            company=cls.other_company,
            email="le-fullpage-other-worker@test.joydigi",
        )
        cls.other_worker.employee_work_info.reporting_manager_id = cls.other_leader
        cls.other_worker.employee_work_info.save(
            update_fields=["reporting_manager_id"]
        )

    def _make(self, employee, **overrides):
        defaults = {
            "employee_id": employee,
            "request_type": "late_arrival",
            "request_date": date.today() + timedelta(days=1),
            "requested_time": "09:30",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return AttendanceLateEarlyRequest.objects.create(**defaults)

    def test_authorized_leader_can_access(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("late-early-request-list"))
        self.assertEqual(response.status_code, 200)

    def test_ordinary_employee_denied(self):
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("late-early-request-list"))
        self.assertEqual(response.status_code, 403)

    def test_unrelated_manager_sees_nothing(self):
        self._make(self.worker)
        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("late-early-request-list"))
        self.assertEqual(list(response.context["requests"]), [])

    def test_cross_company_request_does_not_appear(self):
        self._make(self.other_worker)
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("late-early-request-list"))
        self.assertEqual(list(response.context["requests"]), [])

    def test_pending_approved_and_rejected_all_appear_together(self):
        pending = self._make(self.worker)
        approved = self._make(self.worker, approved=True)
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("late-early-request-list"))

        ids = [item.id for item in response.context["requests"]]
        self.assertIn(pending.id, ids)
        self.assertIn(approved.id, ids)
        self.assertIn(rejected.id, ids)
        self.assertContains(response, "Chờ duyệt")
        self.assertContains(response, "Đã duyệt")
        self.assertContains(response, "Đã từ chối")

    def test_display_type_never_shows_raw_enum(self):
        self._make(self.worker, request_type="late_arrival")
        self._make(self.worker, request_type="early_leave")

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("late-early-request-list"))

        self.assertContains(response, "Đi muộn")
        self.assertContains(response, "Về sớm")
        self.assertNotContains(response, "late_arrival")
        self.assertNotContains(response, "early_leave")

    def test_pending_has_approve_and_reject_actions(self):
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("late-early-request-list"))

        self.assertContains(
            response, reverse("late-early-request-approve", kwargs={"id": pending.id})
        )
        self.assertContains(
            response, reverse("late-early-request-reject", kwargs={"id": pending.id})
        )

    def test_approved_has_no_approve_or_reject_action(self):
        approved = self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("late-early-request-list"))

        self.assertNotContains(
            response, reverse("late-early-request-approve", kwargs={"id": approved.id})
        )
        self.assertNotContains(
            response, reverse("late-early-request-reject", kwargs={"id": approved.id})
        )
        self.assertContains(response, "Đã xử lý")

    def test_rejected_has_no_approve_or_reject_action(self):
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("late-early-request-list"))

        self.assertNotContains(
            response, reverse("late-early-request-approve", kwargs={"id": rejected.id})
        )
        self.assertNotContains(
            response, reverse("late-early-request-reject", kwargs={"id": rejected.id})
        )

    def test_status_filter_pending(self):
        pending = self._make(self.worker)
        self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(
            reverse("late-early-request-list"), {"status": "pending"}
        )

        ids = [item.id for item in response.context["requests"]]
        self.assertEqual(ids, [pending.id])

    def test_status_filter_approved(self):
        self._make(self.worker)
        approved = self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(
            reverse("late-early-request-list"), {"status": "approved"}
        )

        ids = [item.id for item in response.context["requests"]]
        self.assertEqual(ids, [approved.id])


class OvertimeRequestFullPageTests(TestCase):
    """Phase UI-4E.1A: /duyet-don/lam-them-gio/ — the full review page
    for OvertimeRequest, showing every status, scoped through the same
    _visible_employees(request) as the rest of the hub."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Overtime Full Page Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("ot_fullpage_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="ot-fullpage-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("ot_fullpage_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="ot-fullpage-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_company = make_company("Overtime Full Page Other Co")
        cls.other_leader_user = make_user("ot_fullpage_other_leader")
        cls.other_leader = make_employee(
            company=cls.other_company,
            email="ot-fullpage-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user, company=cls.other_company, group=leader_group
        )
        cls.other_worker = make_employee(
            company=cls.other_company,
            email="ot-fullpage-other-worker@test.joydigi",
        )
        cls.other_worker.employee_work_info.reporting_manager_id = cls.other_leader
        cls.other_worker.employee_work_info.save(
            update_fields=["reporting_manager_id"]
        )

    def _make(self, employee, **overrides):
        defaults = {
            "employee_id": employee,
            "request_date": date.today() + timedelta(days=1),
            "start_time": "18:00",
            "end_time": "21:00",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return OvertimeRequest.objects.create(**defaults)

    def test_authorized_leader_can_access(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("overtime-request-list"))
        self.assertEqual(response.status_code, 200)

    def test_ordinary_employee_denied(self):
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("overtime-request-list"))
        self.assertEqual(response.status_code, 403)

    def test_unrelated_manager_sees_nothing(self):
        self._make(self.worker)
        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("overtime-request-list"))
        self.assertEqual(list(response.context["requests"]), [])

    def test_cross_company_request_does_not_appear(self):
        self._make(self.other_worker)
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("overtime-request-list"))
        self.assertEqual(list(response.context["requests"]), [])

    def test_pending_approved_and_rejected_all_appear_together(self):
        pending = self._make(self.worker)
        approved = self._make(self.worker, approved=True)
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("overtime-request-list"))

        ids = [item.id for item in response.context["requests"]]
        self.assertIn(pending.id, ids)
        self.assertIn(approved.id, ids)
        self.assertIn(rejected.id, ids)
        self.assertContains(response, "Chờ duyệt")
        self.assertContains(response, "Đã duyệt")
        self.assertContains(response, "Đã từ chối")

    def test_pending_has_approve_and_reject_actions(self):
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("overtime-request-list"))

        self.assertContains(
            response, reverse("overtime-request-approve", kwargs={"id": pending.id})
        )
        self.assertContains(
            response, reverse("overtime-request-reject", kwargs={"id": pending.id})
        )

    def test_approved_has_no_approve_or_reject_action(self):
        approved = self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("overtime-request-list"))

        self.assertNotContains(
            response, reverse("overtime-request-approve", kwargs={"id": approved.id})
        )
        self.assertNotContains(
            response, reverse("overtime-request-reject", kwargs={"id": approved.id})
        )
        self.assertContains(response, "Đã xử lý")

    def test_rejected_has_no_approve_or_reject_action(self):
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("overtime-request-list"))

        self.assertNotContains(
            response, reverse("overtime-request-approve", kwargs={"id": rejected.id})
        )
        self.assertNotContains(
            response, reverse("overtime-request-reject", kwargs={"id": rejected.id})
        )

    def test_approve_from_full_page_does_not_touch_attendance(self):
        from attendance.models import Attendance

        before = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        self.client.post(
            reverse("overtime-request-approve", kwargs={"id": pending.id}),
            HTTP_REFERER=reverse("overtime-request-list"),
        )

        after = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        self.assertEqual(before, after)

    def test_approve_from_full_page_redirects_back_to_full_page(self):
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.post(
            reverse("overtime-request-approve", kwargs={"id": pending.id}),
            HTTP_REFERER=reverse("overtime-request-list"),
        )

        self.assertRedirects(response, reverse("overtime-request-list"))


class CheckInApprovalHubExplanationRequestTests(TestCase):
    """Phase UI-4F.1: /duyet-don/ 'Đơn giải trình' section — a
    brand-new admin approve/reject action pair, scoped through the
    same _visible_employees(request) used by every other section on
    this page. Structurally independent of the pre-existing Attendance
    'is_validate_request' mechanism (different model, never touched)."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Explanation Hub Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("explanation_hub_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="explanation-hub-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("explanation_hub_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="explanation-hub-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_company = make_company("Explanation Other Co")
        cls.other_leader_user = make_user("explanation_hub_other_leader")
        cls.other_leader = make_employee(
            company=cls.other_company,
            email="explanation-hub-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user,
            company=cls.other_company,
            group=leader_group,
        )
        cls.other_worker = make_employee(
            company=cls.other_company,
            email="explanation-hub-other-worker@test.joydigi",
        )
        cls.other_worker.employee_work_info.reporting_manager_id = cls.other_leader
        cls.other_worker.employee_work_info.save(
            update_fields=["reporting_manager_id"]
        )

    def _make_request(self, employee, **overrides):
        defaults = {
            "employee_id": employee,
            "request_type": "missing_check_in",
            "request_date": date.today() - timedelta(days=1),
            "description": "Tôi quên chấm công khi đến văn phòng.",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return AttendanceExplanationRequest.objects.create(**defaults)

    def test_visible_manager_sees_pending_request(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            pending.id,
            [item.id for item in response.context["explanation_requests"]],
        )
        self.assertContains(response, "Đơn giải trình")

    def test_unrelated_manager_does_not_see_it(self):
        self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(list(response.context["explanation_requests"]), [])

    def test_approved_requests_still_appear_with_approved_status(self):
        approved = self._make_request(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            approved.id,
            [item.id for item in response.context["explanation_requests"]],
        )
        self.assertContains(response, "Đã duyệt")

    def test_canceled_requests_still_appear_with_rejected_status(self):
        canceled = self._make_request(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            canceled.id,
            [item.id for item in response.context["explanation_requests"]],
        )
        self.assertContains(response, "Đã từ chối")

    def test_request_type_renders_vietnamese_label_not_raw_value(self):
        self._make_request(self.worker, request_type="missing_check_in")

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Quên chấm công vào")
        self.assertNotContains(response, "missing_check_in")

    def test_approve_action_uses_dedicated_route_and_requires_permission(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response,
            reverse("explanation-request-approve", kwargs={"id": pending.id}),
        )

        approve_response = self.client.post(
            reverse("explanation-request-approve", kwargs={"id": pending.id})
        )

        self.assertRedirects(approve_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.approved)
        self.assertFalse(pending.canceled)

    def test_approve_does_not_touch_attendance_fields(self):
        from attendance.models import Attendance

        before = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        self.client.post(
            reverse("explanation-request-approve", kwargs={"id": pending.id})
        )

        after = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        self.assertEqual(before, after)

    def test_reject_action_uses_dedicated_route(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response,
            reverse("explanation-request-reject", kwargs={"id": pending.id}),
        )

        reject_response = self.client.post(
            reverse("explanation-request-reject", kwargs={"id": pending.id})
        )

        self.assertRedirects(reject_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.canceled)
        self.assertFalse(pending.approved)

    def test_cross_company_manager_cannot_approve(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.post(
            reverse("explanation-request-approve", kwargs={"id": pending.id})
        )

        self.assertRedirects(response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertFalse(pending.approved)

    def test_cross_company_manager_cannot_reject(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.post(
            reverse("explanation-request-reject", kwargs={"id": pending.id})
        )

        self.assertRedirects(response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertFalse(pending.canceled)

    def test_ordinary_employee_cannot_reach_the_hub_at_all(self):
        self.client.force_login(self.worker_user)

        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 403)

    def test_leave_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin phép")
        self.assertIn("leave_requests", response.context)

    def test_shift_request_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn đổi ca")
        self.assertIn("shift_requests", response.context)

    def test_late_early_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin đi muộn / về sớm")
        self.assertIn("late_early_requests", response.context)

    def test_overtime_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin làm thêm giờ (OT)")
        self.assertIn("overtime_requests", response.context)

    def test_every_section_has_a_full_review_page_link(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, reverse("request-view"))
        self.assertContains(response, reverse("request-attendance-view"))
        self.assertContains(response, reverse("shift-request-view"))
        self.assertContains(response, reverse("late-early-request-list"))
        self.assertContains(response, reverse("overtime-request-list"))
        self.assertContains(response, reverse("explanation-request-list"))


class ExplanationRequestFullPageTests(TestCase):
    """Phase UI-4F.1: /duyet-don/giai-trinh/ — the full review page for
    AttendanceExplanationRequest, showing every status, scoped through
    the same _visible_employees(request) as the rest of the hub."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Explanation Full Page Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("expl_fullpage_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="expl-fullpage-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("expl_fullpage_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="expl-fullpage-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_company = make_company("Explanation Full Page Other Co")
        cls.other_leader_user = make_user("expl_fullpage_other_leader")
        cls.other_leader = make_employee(
            company=cls.other_company,
            email="expl-fullpage-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user,
            company=cls.other_company,
            group=leader_group,
        )
        cls.other_worker = make_employee(
            company=cls.other_company,
            email="expl-fullpage-other-worker@test.joydigi",
        )
        cls.other_worker.employee_work_info.reporting_manager_id = cls.other_leader
        cls.other_worker.employee_work_info.save(
            update_fields=["reporting_manager_id"]
        )

    def _make(self, employee, **overrides):
        defaults = {
            "employee_id": employee,
            "request_type": "missing_check_in",
            "request_date": date.today() - timedelta(days=1),
            "description": "Tôi quên chấm công khi đến văn phòng.",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return AttendanceExplanationRequest.objects.create(**defaults)

    def test_authorized_leader_can_access(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("explanation-request-list"))
        self.assertEqual(response.status_code, 200)

    def test_ordinary_employee_denied(self):
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("explanation-request-list"))
        self.assertEqual(response.status_code, 403)

    def test_unrelated_manager_sees_nothing(self):
        self._make(self.worker)
        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("explanation-request-list"))
        self.assertEqual(list(response.context["requests"]), [])

    def test_cross_company_request_does_not_appear(self):
        self._make(self.other_worker)
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("explanation-request-list"))
        self.assertEqual(list(response.context["requests"]), [])

    def test_default_filter_is_all(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("explanation-request-list"))
        self.assertEqual(response.context["status"], "all")

    def test_pending_approved_and_rejected_all_appear_together(self):
        pending = self._make(self.worker)
        approved = self._make(self.worker, approved=True)
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("explanation-request-list"))

        ids = [item.id for item in response.context["requests"]]
        self.assertIn(pending.id, ids)
        self.assertIn(approved.id, ids)
        self.assertIn(rejected.id, ids)
        self.assertContains(response, "Chờ duyệt")
        self.assertContains(response, "Đã duyệt")
        self.assertContains(response, "Đã từ chối")

    def test_pending_status_filter(self):
        pending = self._make(self.worker)
        self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(
            reverse("explanation-request-list"), {"status": "pending"}
        )

        ids = [item.id for item in response.context["requests"]]
        self.assertEqual(ids, [pending.id])

    def test_approved_status_filter(self):
        self._make(self.worker)
        approved = self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(
            reverse("explanation-request-list"), {"status": "approved"}
        )

        ids = [item.id for item in response.context["requests"]]
        self.assertEqual(ids, [approved.id])

    def test_rejected_status_filter(self):
        self._make(self.worker)
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(
            reverse("explanation-request-list"), {"status": "rejected"}
        )

        ids = [item.id for item in response.context["requests"]]
        self.assertEqual(ids, [rejected.id])

    def test_pending_has_approve_and_reject_actions(self):
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("explanation-request-list"))

        self.assertContains(
            response,
            reverse("explanation-request-approve", kwargs={"id": pending.id}),
        )
        self.assertContains(
            response,
            reverse("explanation-request-reject", kwargs={"id": pending.id}),
        )

    def test_approved_has_no_approve_or_reject_action(self):
        approved = self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("explanation-request-list"))

        self.assertNotContains(
            response,
            reverse("explanation-request-approve", kwargs={"id": approved.id}),
        )
        self.assertNotContains(
            response,
            reverse("explanation-request-reject", kwargs={"id": approved.id}),
        )
        self.assertContains(response, "Đã xử lý")

    def test_rejected_has_no_approve_or_reject_action(self):
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("explanation-request-list"))

        self.assertNotContains(
            response,
            reverse("explanation-request-approve", kwargs={"id": rejected.id}),
        )
        self.assertNotContains(
            response,
            reverse("explanation-request-reject", kwargs={"id": rejected.id}),
        )

    def test_approve_from_full_page_does_not_touch_attendance(self):
        from attendance.models import Attendance

        before = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        self.client.post(
            reverse("explanation-request-approve", kwargs={"id": pending.id}),
            HTTP_REFERER=reverse("explanation-request-list"),
        )

        after = list(
            Attendance.objects.values_list(
                "id", "attendance_overtime", "attendance_overtime_approve"
            )
        )
        self.assertEqual(before, after)

    def test_approve_from_full_page_redirects_back_to_full_page(self):
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.post(
            reverse("explanation-request-approve", kwargs={"id": pending.id}),
            HTTP_REFERER=reverse("explanation-request-list"),
        )

        self.assertRedirects(response, reverse("explanation-request-list"))


class CheckInApprovalHubRemoteRequestTests(TestCase):
    """Phase UI-4G.1: /duyet-don/ 'Đơn Remote' section — a brand-new
    admin approve/reject action pair, scoped through the same
    _visible_employees(request) used by every other section on this
    page. Structurally independent of the pre-existing WorkTypeRequest
    (different model, never touched — see RemoteWorkRequest docstring
    for why it was not reused)."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Remote Hub Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("remote_hub_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="remote-hub-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("remote_hub_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="remote-hub-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_company = make_company("Remote Other Co")
        cls.other_leader_user = make_user("remote_hub_other_leader")
        cls.other_leader = make_employee(
            company=cls.other_company,
            email="remote-hub-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user, company=cls.other_company, group=leader_group
        )
        cls.other_worker = make_employee(
            company=cls.other_company,
            email="remote-hub-other-worker@test.joydigi",
        )
        cls.other_worker.employee_work_info.reporting_manager_id = cls.other_leader
        cls.other_worker.employee_work_info.save(
            update_fields=["reporting_manager_id"]
        )

    def _make_request(self, employee, **overrides):
        defaults = {
            "employee_id": employee,
            "start_date": date.today() + timedelta(days=1),
            "end_date": date.today() + timedelta(days=2),
            "description": "Chăm sóc con nhỏ tại nhà.",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return RemoteWorkRequest.objects.create(**defaults)

    def test_visible_manager_sees_pending_request(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            pending.id,
            [item.id for item in response.context["remote_requests"]],
        )
        self.assertContains(response, "Đơn Remote")

    def test_unrelated_manager_does_not_see_it(self):
        self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(list(response.context["remote_requests"]), [])

    def test_approved_requests_still_appear_with_approved_status(self):
        approved = self._make_request(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            approved.id,
            [item.id for item in response.context["remote_requests"]],
        )
        self.assertContains(response, "Đã duyệt")

    def test_canceled_requests_still_appear_with_rejected_status(self):
        canceled = self._make_request(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertIn(
            canceled.id,
            [item.id for item in response.context["remote_requests"]],
        )
        self.assertContains(response, "Đã từ chối")

    def test_approve_action_uses_dedicated_route_and_requires_permission(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response,
            reverse("remote-request-approve", kwargs={"id": pending.id}),
        )

        approve_response = self.client.post(
            reverse("remote-request-approve", kwargs={"id": pending.id})
        )

        self.assertRedirects(approve_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.approved)
        self.assertFalse(pending.canceled)

    def test_approve_does_not_touch_employee_work_type(self):
        before_work_type_id = self.worker.employee_work_info.work_type_id_id
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        self.client.post(
            reverse("remote-request-approve", kwargs={"id": pending.id})
        )

        self.worker.employee_work_info.refresh_from_db()
        self.assertEqual(
            self.worker.employee_work_info.work_type_id_id, before_work_type_id
        )

    def test_reject_action_uses_dedicated_route(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))
        self.assertContains(
            response,
            reverse("remote-request-reject", kwargs={"id": pending.id}),
        )

        reject_response = self.client.post(
            reverse("remote-request-reject", kwargs={"id": pending.id})
        )

        self.assertRedirects(reject_response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertTrue(pending.canceled)
        self.assertFalse(pending.approved)

    def test_cross_company_manager_cannot_approve(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.post(
            reverse("remote-request-approve", kwargs={"id": pending.id})
        )

        self.assertRedirects(response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertFalse(pending.approved)

    def test_cross_company_manager_cannot_reject(self):
        pending = self._make_request(self.worker)

        self.client.force_login(self.other_leader_user)
        response = self.client.post(
            reverse("remote-request-reject", kwargs={"id": pending.id})
        )

        self.assertRedirects(response, reverse("approval-hub"))
        pending.refresh_from_db()
        self.assertFalse(pending.canceled)

    def test_ordinary_employee_cannot_reach_the_hub_at_all(self):
        self.client.force_login(self.worker_user)

        response = self.client.get(reverse("approval-hub"))

        self.assertEqual(response.status_code, 403)

    def test_leave_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin phép")
        self.assertIn("leave_requests", response.context)

    def test_shift_request_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn đổi ca")
        self.assertIn("shift_requests", response.context)

    def test_late_early_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin đi muộn / về sớm")
        self.assertIn("late_early_requests", response.context)

    def test_overtime_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn xin làm thêm giờ (OT)")
        self.assertIn("overtime_requests", response.context)

    def test_explanation_section_still_renders(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, "Đơn giải trình")
        self.assertIn("explanation_requests", response.context)

    def test_every_section_has_a_full_review_page_link(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("approval-hub"))

        self.assertContains(response, reverse("request-view"))
        self.assertContains(response, reverse("request-attendance-view"))
        self.assertContains(response, reverse("shift-request-view"))
        self.assertContains(response, reverse("late-early-request-list"))
        self.assertContains(response, reverse("overtime-request-list"))
        self.assertContains(response, reverse("explanation-request-list"))
        self.assertContains(response, reverse("remote-request-list"))


class RemoteRequestFullPageTests(TestCase):
    """Phase UI-4G.1: /duyet-don/remote/ — the full review page for
    RemoteWorkRequest, showing every status, scoped through the same
    _visible_employees(request) as the rest of the hub."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Remote Full Page Co")
        leader_group, _ = Group.objects.get_or_create(name=LEADER_ROLE)

        cls.leader_user = make_user("remote_fullpage_leader")
        cls.leader = make_employee(
            company=cls.company,
            email="remote-fullpage-leader@test.joydigi",
            user=cls.leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.leader_user, company=cls.company, group=leader_group
        )
        cls.worker_user = make_user("remote_fullpage_worker")
        cls.worker = make_employee(
            company=cls.company,
            email="remote-fullpage-worker@test.joydigi",
            user=cls.worker_user,
        )
        cls.worker.employee_work_info.reporting_manager_id = cls.leader
        cls.worker.employee_work_info.save(update_fields=["reporting_manager_id"])

        cls.other_company = make_company("Remote Full Page Other Co")
        cls.other_leader_user = make_user("remote_fullpage_other_leader")
        cls.other_leader = make_employee(
            company=cls.other_company,
            email="remote-fullpage-other-leader@test.joydigi",
            user=cls.other_leader_user,
        )
        CompanyGroupAssignment.objects.create(
            user=cls.other_leader_user,
            company=cls.other_company,
            group=leader_group,
        )
        cls.other_worker = make_employee(
            company=cls.other_company,
            email="remote-fullpage-other-worker@test.joydigi",
        )
        cls.other_worker.employee_work_info.reporting_manager_id = cls.other_leader
        cls.other_worker.employee_work_info.save(
            update_fields=["reporting_manager_id"]
        )

    def _make(self, employee, **overrides):
        defaults = {
            "employee_id": employee,
            "start_date": date.today() + timedelta(days=1),
            "end_date": date.today() + timedelta(days=2),
            "description": "Chăm sóc con nhỏ tại nhà.",
            "approved": False,
            "canceled": False,
        }
        defaults.update(overrides)
        return RemoteWorkRequest.objects.create(**defaults)

    def test_authorized_leader_can_access(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("remote-request-list"))
        self.assertEqual(response.status_code, 200)

    def test_ordinary_employee_denied(self):
        self.client.force_login(self.worker_user)
        response = self.client.get(reverse("remote-request-list"))
        self.assertEqual(response.status_code, 403)

    def test_unrelated_manager_sees_nothing(self):
        self._make(self.worker)
        self.client.force_login(self.other_leader_user)
        response = self.client.get(reverse("remote-request-list"))
        self.assertEqual(list(response.context["requests"]), [])

    def test_cross_company_request_does_not_appear(self):
        self._make(self.other_worker)
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("remote-request-list"))
        self.assertEqual(list(response.context["requests"]), [])

    def test_default_filter_is_all(self):
        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("remote-request-list"))
        self.assertEqual(response.context["status"], "all")

    def test_pending_approved_and_rejected_all_appear_together(self):
        pending = self._make(self.worker)
        approved = self._make(self.worker, approved=True)
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("remote-request-list"))

        ids = [item.id for item in response.context["requests"]]
        self.assertIn(pending.id, ids)
        self.assertIn(approved.id, ids)
        self.assertIn(rejected.id, ids)
        self.assertContains(response, "Chờ duyệt")
        self.assertContains(response, "Đã duyệt")
        self.assertContains(response, "Đã từ chối")

    def test_pending_status_filter(self):
        pending = self._make(self.worker)
        self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(
            reverse("remote-request-list"), {"status": "pending"}
        )

        ids = [item.id for item in response.context["requests"]]
        self.assertEqual(ids, [pending.id])

    def test_approved_status_filter(self):
        self._make(self.worker)
        approved = self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(
            reverse("remote-request-list"), {"status": "approved"}
        )

        ids = [item.id for item in response.context["requests"]]
        self.assertEqual(ids, [approved.id])

    def test_rejected_status_filter(self):
        self._make(self.worker)
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(
            reverse("remote-request-list"), {"status": "rejected"}
        )

        ids = [item.id for item in response.context["requests"]]
        self.assertEqual(ids, [rejected.id])

    def test_pending_has_approve_and_reject_actions(self):
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("remote-request-list"))

        self.assertContains(
            response,
            reverse("remote-request-approve", kwargs={"id": pending.id}),
        )
        self.assertContains(
            response,
            reverse("remote-request-reject", kwargs={"id": pending.id}),
        )

    def test_approved_has_no_approve_or_reject_action(self):
        approved = self._make(self.worker, approved=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("remote-request-list"))

        self.assertNotContains(
            response,
            reverse("remote-request-approve", kwargs={"id": approved.id}),
        )
        self.assertNotContains(
            response,
            reverse("remote-request-reject", kwargs={"id": approved.id}),
        )
        self.assertContains(response, "Đã xử lý")

    def test_rejected_has_no_approve_or_reject_action(self):
        rejected = self._make(self.worker, canceled=True)

        self.client.force_login(self.leader_user)
        response = self.client.get(reverse("remote-request-list"))

        self.assertNotContains(
            response,
            reverse("remote-request-approve", kwargs={"id": rejected.id}),
        )
        self.assertNotContains(
            response,
            reverse("remote-request-reject", kwargs={"id": rejected.id}),
        )

    def test_approve_from_full_page_does_not_touch_employee_work_type(self):
        before_work_type_id = self.worker.employee_work_info.work_type_id_id
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        self.client.post(
            reverse("remote-request-approve", kwargs={"id": pending.id}),
            HTTP_REFERER=reverse("remote-request-list"),
        )

        self.worker.employee_work_info.refresh_from_db()
        self.assertEqual(
            self.worker.employee_work_info.work_type_id_id, before_work_type_id
        )

    def test_approve_from_full_page_redirects_back_to_full_page(self):
        pending = self._make(self.worker)

        self.client.force_login(self.leader_user)
        response = self.client.post(
            reverse("remote-request-approve", kwargs={"id": pending.id}),
            HTTP_REFERER=reverse("remote-request-list"),
        )

        self.assertRedirects(response, reverse("remote-request-list"))
