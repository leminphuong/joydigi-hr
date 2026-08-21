from datetime import date, timedelta

from django.test import TestCase
from django.template.loader import render_to_string
from django.urls import reverse

from base.dashboard import _get_setup_checklist_context
from base.models import CheckInLocation, EmployeeShift, OfficeWifi, Roster
from attendance.models import AttendanceConflictResolution
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
