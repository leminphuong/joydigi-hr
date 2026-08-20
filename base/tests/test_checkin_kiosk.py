from urllib.parse import parse_qs, urlparse

from django.test import TestCase
from django.urls import reverse

from base.checkin_tokens import kiosk_numeric_code, valid_kiosk_token


class CheckInKioskTests(TestCase):
    def test_kiosk_data_returns_matching_qr_and_six_digit_code(self):
        response = self.client.get(reverse("checkin-kiosk-data"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        token = parse_qs(urlparse(payload["qr_url"]).query)["token"][0]

        self.assertTrue(valid_kiosk_token(token))
        self.assertRegex(payload["code"], r"^\d{6}$")
        self.assertEqual(payload["code"], kiosk_numeric_code(token))

        image_response = self.client.get(payload["qr_url"])
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response["Content-Type"], "image/png")

    def test_kiosk_page_contains_numeric_code_area(self):
        response = self.client.get(reverse("checkin-kiosk"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="kioskCode"')
        self.assertContains(response, "Mã số")
