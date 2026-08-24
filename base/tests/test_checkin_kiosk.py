from urllib.parse import parse_qs, urlparse

from django.test import TestCase
from django.urls import reverse

from base.checkin_tokens import resolve_kiosk_code, resolve_kiosk_token
from base.models import CheckInLocation
from joydigi.testkit import make_company, make_employee, make_user


class CheckInKioskTests(TestCase):
    """Phase 6.1: the kiosk display used to be fully public/unauthenticated
    and its tokens carried no company/location binding at all (see
    `base.checkin_tokens` module docstring). It's now gated to an
    authenticated checkin-leader/admin and bound to one real,
    caller-owned `CheckInLocation`."""

    def setUp(self):
        self.company = make_company("Kiosk Co")
        self.admin_user = make_user(
            "kioskadmin", password="secret123", is_superuser=True
        )
        make_employee(
            company=self.company, email="kioskadmin@test.joydigi", user=self.admin_user
        )
        self.location = CheckInLocation.objects.create(
            company_id=self.company,
            name="Head Office",
            latitude=10.776530,
            longitude=106.700981,
            radius_meters=150,
        )
        self.client.force_login(self.admin_user)

    def test_kiosk_data_requires_login(self):
        anon_client = self.client_class()
        response = anon_client.get(
            reverse("checkin-kiosk-data"), {"location_id": self.location.id}
        )
        self.assertIn(response.status_code, (302, 403))

    def test_kiosk_data_requires_valid_location(self):
        response = self.client.get(reverse("checkin-kiosk-data"))
        self.assertEqual(response.status_code, 400)

        other_company = make_company("Other Co")
        other_location = CheckInLocation.objects.create(
            company_id=other_company,
            name="Other Office",
            latitude=1,
            longitude=1,
            radius_meters=100,
        )
        response = self.client.get(
            reverse("checkin-kiosk-data"), {"location_id": other_location.id}
        )
        self.assertEqual(response.status_code, 400)

    def test_kiosk_data_returns_matching_qr_and_six_digit_code_bound_to_location(self):
        response = self.client.get(
            reverse("checkin-kiosk-data"), {"location_id": self.location.id}
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        token = parse_qs(urlparse(payload["qr_url"]).query)["token"][0]

        token_session = resolve_kiosk_token(token)
        self.assertIsNotNone(token_session)
        self.assertEqual(token_session["company_id"], self.company.id)
        self.assertEqual(token_session["location_id"], self.location.id)

        self.assertRegex(payload["code"], r"^\d{6}$")
        code_session = resolve_kiosk_code(payload["code"])
        self.assertEqual(code_session, token_session)

        image_response = self.client.get(payload["qr_url"])
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response["Content-Type"], "image/png")

    def test_kiosk_qr_rejects_unknown_token(self):
        response = self.client.get(
            reverse("checkin-kiosk-qr"), {"token": "not-a-real-token"}
        )
        self.assertEqual(response.status_code, 403)

    def test_kiosk_page_requires_login(self):
        anon_client = self.client_class()
        response = anon_client.get(reverse("checkin-kiosk"))
        self.assertIn(response.status_code, (302, 403))

    def test_kiosk_page_shows_location_picker_without_location_id(self):
        response = self.client.get(reverse("checkin-kiosk"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Head Office")

    def test_kiosk_page_shows_qr_area_for_valid_location(self):
        response = self.client.get(
            reverse("checkin-kiosk"), {"location_id": self.location.id}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="kioskCode"')
