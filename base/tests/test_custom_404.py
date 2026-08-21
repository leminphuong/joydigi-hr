from django.test import TestCase, override_settings
from django.urls import resolve, reverse


class Custom404Tests(TestCase):
    def test_leave_request_route_is_registered_before_404_fallback(self):
        url = reverse("request-view")
        match = resolve(url)

        self.assertEqual(url, "/leave/request-view/")
        self.assertEqual(match.view_name, "request-view")

    def test_monthly_attendance_route_is_registered_before_404_fallback(self):
        url = reverse("attendance-monthly-summary")
        match = resolve(url)

        self.assertEqual(url, "/attendance/monthly-summary/")
        self.assertEqual(match.view_name, "attendance-monthly-summary")

    def test_unknown_path_uses_custom_page_while_debug_is_enabled(self):
        response = self.client.get("/emplwdq")

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertContains(
            response,
            "Không tìm thấy trang bạn yêu cầu",
            status_code=404,
        )

    @override_settings(DEBUG=False)
    def test_unknown_path_uses_vietnamese_404_page(self):
        response = self.client.get("/duong-dan-khong-ton-tai/")

        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertContains(
            response,
            "Không tìm thấy trang bạn yêu cầu",
            status_code=404,
        )
        self.assertContains(response, "/duong-dan-khong-ton-tai/", status_code=404)
