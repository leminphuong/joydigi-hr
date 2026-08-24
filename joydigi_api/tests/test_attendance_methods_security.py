"""
Phase 6.1 — backend security remediation for the four mobile
attendance methods (Location, Wi-Fi, Dynamic QR, 6-digit fallback).

Covers:
- attendance-source validation fails closed (Location/Wi-Fi/QR/code)
- companies with no method configured keep today's exact behavior
  (backward compatible)
- the mobile API can no longer trigger the old "trusted device" bypass
- QR/6-digit sessions are bound to a real company + location, not
  global/unbound
- 6-digit fallback is server-side throttled
- the verify-source -> proof -> clock-in/out flow (TOCTOU fix)
- employee/company identity is always server-derived, never
  client-supplied
"""

from datetime import date, datetime
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from attendance.models import Attendance
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.checkin_tokens import create_kiosk_session
from base.models import (
    CheckInLocation,
    CheckInPolicy,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    OfficeWifi,
)
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

# Real coordinates ~11m apart (well inside any sane radius) and a
# second point ~5km away (outside any sane radius).
OFFICE_LAT, OFFICE_LNG = 10.776530, 106.700981
NEARBY_LAT, NEARBY_LNG = 10.776600, 106.701000
FAR_LAT, FAR_LNG = 10.826530, 106.750981


class AttendanceSecurityTestCase(TestCase):
    def setUp(self):
        # The 6-digit throttle counter lives in Django's cache, not the
        # DB — TestCase's transaction rollback never clears it, and
        # SQLite tends to reuse the same auto-incremented user id
        # across rolled-back tests, so a previous test's throttle
        # attempts can otherwise leak into this one.
        cache.clear()
        self.company = make_company("Security Co")
        self.user = make_user("secuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="secuser@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Security Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )
        # `make_employee()` (called above) already sets `company_id` on
        # the real DB row, but `Employee.save()` appears to cache a
        # blank `EmployeeWorkInformation` on `self.user`'s reverse
        # `employee_get` descriptor *before* that happens — without
        # this refresh, `request.user.employee_get.employee_work_info
        # .company_id` (exactly what `_resolve_checkin_company` reads)
        # stays `None` for the rest of this Python object's life, which
        # `validate_checkin_source` treats as "no company" and
        # unconditionally allows. Confirmed empirically, not guessed.
        self.user.refresh_from_db()
        self.today = date.today()
        self.day = EmployeeShiftDay.objects.get(day=self.today.strftime("%A").lower())
        EmployeeShiftSchedule.objects.get_or_create(
            shift_id=self.shift,
            day=self.day,
            defaults={
                "is_night_shift": False,
                "minimum_working_hour": "08:00",
                "start_time": "08:00:00",
                "end_time": "17:00:00",
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _clock_in_directly(self):
        """Bypasses the API (and its source validation) to put the
        employee in a real CHECKED_IN state for check-out tests."""
        clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=self.today,
            attendance_date=self.today,
            day=self.day,
            now="08:00",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=0,
            end_time=1,
            in_datetime=datetime.now(),
        )

    def _make_location(self, company=None, **overrides):
        defaults = {
            "company_id": company or self.company,
            "name": "Head Office",
            "latitude": OFFICE_LAT,
            "longitude": OFFICE_LNG,
            "radius_meters": 100,
        }
        defaults.update(overrides)
        return CheckInLocation.objects.create(**defaults)

    def _make_wifi(self, company=None, **overrides):
        defaults = {
            "company_id": company or self.company,
            "name": "Office Wifi",
            "ssid": "JoyDigi-Office",
            "bssid": "",
        }
        defaults.update(overrides)
        return OfficeWifi.objects.create(**defaults)


class BackwardCompatibilityTests(AttendanceSecurityTestCase):
    def test_checkin_with_no_method_configured_is_unaffected(self):
        """A company with zero CheckInLocation/OfficeWifi configured
        must keep today's exact behavior: plain check-in with no
        evidence still succeeds."""
        response = self.client.post("/api/attendance/clock-in/")
        self.assertEqual(response.status_code, 200, response.data)

    def test_checkout_with_no_method_configured_is_unaffected(self):
        self._clock_in_directly()
        response = self.client.post("/api/attendance/clock-out/")
        self.assertEqual(response.status_code, 200, response.data)


class TrustedDeviceBypassTests(AttendanceSecurityTestCase):
    def test_mobile_api_cannot_trigger_trusted_device_bypass_on_checkin(self):
        """Once a company has a real CheckInLocation configured, the
        mobile API must not get an automatic pass — that's exactly the
        bug this phase fixes (the old code inferred "trusted device"
        from the mere presence of `.datetime`, which every mobile
        request sets)."""
        self._make_location()

        response = self.client.post("/api/attendance/clock-in/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "VERIFICATION_REQUIRED")
        self.assertFalse(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists()
        )

    def test_mobile_api_cannot_trigger_trusted_device_bypass_on_checkout(self):
        self._make_location()
        self._clock_in_directly()

        response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "VERIFICATION_REQUIRED")
        attendance = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNone(attendance.attendance_clock_out)


class LocationValidationTests(AttendanceSecurityTestCase):
    def test_inside_radius_allowed(self):
        self._make_location()

        response = self.client.post(
            "/api/attendance/clock-in/",
            {"latitude": NEARBY_LAT, "longitude": NEARBY_LNG},
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_outside_radius_rejected_when_policy_disallows(self):
        self._make_location()
        CheckInPolicy.objects.create(
            company_id=self.company, allow_outside_radius_request=False
        )

        response = self.client.post(
            "/api/attendance/clock-in/", {"latitude": FAR_LAT, "longitude": FAR_LNG}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "LOCATION_OUTSIDE")
        self.assertFalse(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists()
        )

    def test_outside_radius_allowed_when_policy_allows_but_flagged(self):
        self._make_location()
        CheckInPolicy.objects.create(
            company_id=self.company, allow_outside_radius_request=True
        )

        response = self.client.post(
            "/api/attendance/clock-in/", {"latitude": FAR_LAT, "longitude": FAR_LNG}
        )

        self.assertEqual(response.status_code, 200, response.data)
        attendance = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertTrue(attendance.is_validate_request)

    def test_invalid_coordinates_rejected(self):
        self._make_location()

        response = self.client.post(
            "/api/attendance/clock-in/", {"latitude": 999, "longitude": 106.7}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "LOCATION_INVALID")

    def test_missing_longitude_treated_as_no_evidence(self):
        self._make_location()

        response = self.client.post(
            "/api/attendance/clock-in/", {"latitude": OFFICE_LAT}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "VERIFICATION_REQUIRED")

    def test_disabled_location_treated_as_not_configured(self):
        self._make_location(is_active=False)

        # No active locations at all -> falls back to today's
        # unauthenticated-evidence-allowed compatibility behavior.
        response = self.client.post("/api/attendance/clock-in/")

        self.assertEqual(response.status_code, 200, response.data)

    def test_other_company_location_never_matched(self):
        other_company = make_company("Rival Co")
        self._make_location(company=other_company)
        # self.company has zero locations of its own.

        response = self.client.post(
            "/api/attendance/clock-in/",
            {"latitude": NEARBY_LAT, "longitude": NEARBY_LNG},
        )

        # Falls back to "no location configured for my company" ->
        # allowed, never evaluated against the other company's office.
        self.assertEqual(response.status_code, 200, response.data)

    def test_validation_exception_fails_closed(self):
        self._make_location()
        # Django's test client re-raises unhandled server exceptions
        # instead of turning them into a 500 Response, so we assert on
        # the exception itself rather than a status code. The real
        # behavior we care about — no Attendance row was created — is
        # asserted below regardless.
        with mock.patch(
            "attendance.views.clock_in_out._distance_meters",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/api/attendance/clock-in/",
                    {"latitude": NEARBY_LAT, "longitude": NEARBY_LNG},
                )

        self.assertFalse(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists()
        )


class WifiValidationTests(AttendanceSecurityTestCase):
    def test_valid_ssid_allowed(self):
        self._make_wifi()

        response = self.client.post(
            "/api/attendance/clock-in/", {"wifi_ssid": "JoyDigi-Office"}
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_wrong_ssid_rejected(self):
        self._make_wifi()

        response = self.client.post(
            "/api/attendance/clock-in/", {"wifi_ssid": "Some-Other-Network"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "WIFI_NOT_ALLOWED")

    def test_bssid_mismatch_rejected_when_bssid_configured(self):
        self._make_wifi(bssid="AA:BB:CC:DD:EE:FF")

        response = self.client.post(
            "/api/attendance/clock-in/",
            {"wifi_ssid": "JoyDigi-Office", "wifi_bssid": "11:22:33:44:55:66"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "WIFI_NOT_ALLOWED")

    def test_inactive_network_not_matched(self):
        self._make_wifi(is_active=False)

        response = self.client.post(
            "/api/attendance/clock-in/", {"wifi_ssid": "JoyDigi-Office"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "WIFI_NOT_ALLOWED")

    def test_other_company_wifi_never_matched(self):
        other_company = make_company("Rival Wifi Co")
        self._make_wifi(company=other_company)

        response = self.client.post(
            "/api/attendance/clock-in/", {"wifi_ssid": "JoyDigi-Office"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "WIFI_NOT_ALLOWED")


class QrValidationTests(AttendanceSecurityTestCase):
    def test_valid_qr_token_allows_checkin(self):
        location = self._make_location()
        session = create_kiosk_session(
            company_id=self.company.id, location_id=location.id
        )

        response = self.client.post(
            "/api/attendance/clock-in/", {"qr_token": session["token"]}
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_tampered_token_rejected(self):
        location = self._make_location()
        session = create_kiosk_session(
            company_id=self.company.id, location_id=location.id
        )
        tampered = session["token"][:-1] + ("A" if session["token"][-1] != "A" else "B")

        response = self.client.post(
            "/api/attendance/clock-in/", {"qr_token": tampered}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "QR_EXPIRED")

    def test_unknown_token_rejected(self):
        response = self.client.post(
            "/api/attendance/clock-in/", {"qr_token": "does-not-exist"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "QR_EXPIRED")

    def test_wrong_company_token_rejected(self):
        other_company = make_company("QR Rival Co")
        other_location = self._make_location(
            company=other_company, name="Rival Office"
        )
        session = create_kiosk_session(
            company_id=other_company.id, location_id=other_location.id
        )

        response = self.client.post(
            "/api/attendance/clock-in/", {"qr_token": session["token"]}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "QR_WRONG_COMPANY")

    def test_location_deactivated_after_issuance_rejected(self):
        location = self._make_location()
        session = create_kiosk_session(
            company_id=self.company.id, location_id=location.id
        )
        location.is_active = False
        location.save(update_fields=["is_active"])

        response = self.client.post(
            "/api/attendance/clock-in/", {"qr_token": session["token"]}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "QR_WRONG_LOCATION")


class NumericCodeValidationTests(AttendanceSecurityTestCase):
    def test_valid_code_allows_checkin(self):
        location = self._make_location()
        session = create_kiosk_session(
            company_id=self.company.id, location_id=location.id
        )

        response = self.client.post(
            "/api/attendance/clock-in/", {"numeric_code": session["code"]}
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_wrong_code_rejected(self):
        self._make_location()

        response = self.client.post(
            "/api/attendance/clock-in/", {"numeric_code": "000000"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "QR_CODE_INVALID")

    def test_wrong_company_code_rejected(self):
        other_company = make_company("Code Rival Co")
        other_location = self._make_location(
            company=other_company, name="Rival Office"
        )
        session = create_kiosk_session(
            company_id=other_company.id, location_id=other_location.id
        )

        response = self.client.post(
            "/api/attendance/clock-in/", {"numeric_code": session["code"]}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data.get("code"), "QR_WRONG_COMPANY")

    def test_repeated_wrong_attempts_are_throttled(self):
        self._make_location()

        last_response = None
        for _ in range(10):
            last_response = self.client.post(
                "/api/attendance/clock-in/", {"numeric_code": "000000"}
            )

        self.assertEqual(last_response.status_code, 400)
        self.assertEqual(last_response.data.get("code"), "QR_CODE_THROTTLED")

    def test_throttle_is_per_employee(self):
        """A throttled employee doesn't lock out a different employee."""
        self._make_location()
        for _ in range(10):
            self.client.post("/api/attendance/clock-in/", {"numeric_code": "000000"})

        other_user = make_user("othercodeuser", password="secret123")
        other_employee = make_employee(
            company=self.company, email="othercode@test.joydigi", user=other_user
        )
        EmployeeWorkInformation.objects.filter(employee_id=other_employee).update(
            shift_id=self.shift
        )
        other_user.refresh_from_db()
        other_client = APIClient()
        other_client.force_authenticate(user=other_user)

        response = other_client.post(
            "/api/attendance/clock-in/", {"numeric_code": "000000"}
        )

        self.assertEqual(response.data.get("code"), "QR_CODE_INVALID")


class EmployeeAuthorityTests(AttendanceSecurityTestCase):
    def test_checkin_ignores_client_supplied_employee_and_company_id(self):
        other_user = make_user("victimuser", password="secret123")
        victim = make_employee(
            company=self.company, email="victim@test.joydigi", user=other_user
        )

        response = self.client.post(
            "/api/attendance/clock-in/",
            {"employee_id": victim.id, "company_id": 999999},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists()
        )
        self.assertFalse(
            Attendance.objects.filter(
                employee_id=victim, attendance_date=self.today
            ).exists()
        )


class VerifySourceAndProofTests(AttendanceSecurityTestCase):
    def test_verify_source_issues_proof_on_valid_evidence(self):
        self._make_wifi()

        response = self.client.post(
            "/api/attendance/verify-source/",
            {"method": "wifi", "wifi_ssid": "JoyDigi-Office"},
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["verified"])
        self.assertTrue(response.data["proof"])

    def test_verify_source_rejects_invalid_evidence_no_proof_issued(self):
        self._make_wifi()

        response = self.client.post(
            "/api/attendance/verify-source/",
            {"method": "wifi", "wifi_ssid": "Wrong-Network"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("proof", response.data)

    def test_verify_source_rejects_unknown_method(self):
        response = self.client.post(
            "/api/attendance/verify-source/", {"method": "teleport"}
        )
        self.assertEqual(response.status_code, 400)

    def test_proof_allows_checkin_without_resupplying_evidence(self):
        self._make_wifi()
        verify = self.client.post(
            "/api/attendance/verify-source/",
            {"method": "wifi", "wifi_ssid": "JoyDigi-Office"},
        )
        proof = verify.data["proof"]

        response = self.client.post(
            "/api/attendance/clock-in/", {"verification_proof": proof}
        )

        self.assertEqual(response.status_code, 200, response.data)

    def test_proof_is_single_use(self):
        self._make_wifi()
        verify = self.client.post(
            "/api/attendance/verify-source/",
            {"method": "wifi", "wifi_ssid": "JoyDigi-Office"},
        )
        proof = verify.data["proof"]
        first = self.client.post(
            "/api/attendance/clock-in/", {"verification_proof": proof}
        )
        self.assertEqual(first.status_code, 200, first.data)

        # The proof was already consumed by the first check-in — assert
        # the validator itself now rejects a second use directly,
        # rather than relying on unrelated clock-in/out state machine
        # rules (already-checked-in) to indirectly prove single-use.
        from attendance.methods.verification_proof import consume_verification_proof

        self.assertIsNone(
            consume_verification_proof(proof, self.employee.id)
        )

    def test_proof_cannot_be_used_by_a_different_employee(self):
        self._make_wifi()
        verify = self.client.post(
            "/api/attendance/verify-source/",
            {"method": "wifi", "wifi_ssid": "JoyDigi-Office"},
        )
        proof = verify.data["proof"]

        other_user = make_user("proofthief", password="secret123")
        other_employee = make_employee(
            company=self.company, email="proofthief@test.joydigi", user=other_user
        )
        EmployeeWorkInformation.objects.filter(employee_id=other_employee).update(
            shift_id=self.shift
        )
        other_user.refresh_from_db()
        other_client = APIClient()
        other_client.force_authenticate(user=other_user)

        response = other_client.post(
            "/api/attendance/clock-in/", {"verification_proof": proof}
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Attendance.objects.filter(
                employee_id=other_employee, attendance_date=self.today
            ).exists()
        )


class PolicyEndpointTests(AttendanceSecurityTestCase):
    def test_policy_reflects_no_methods_configured(self):
        response = self.client.get("/api/attendance/policy/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["location"]["enabled"])
        self.assertFalse(response.data["wifi"]["enabled"])
        self.assertFalse(response.data["qr"]["enabled"])
        self.assertFalse(response.data["numeric_code"]["enabled"])
        self.assertFalse(response.data["camera_ai"]["enabled"])

    def test_policy_reflects_configured_methods(self):
        self._make_location()
        self._make_wifi()

        response = self.client.get("/api/attendance/policy/")

        self.assertTrue(response.data["location"]["enabled"])
        self.assertTrue(response.data["wifi"]["enabled"])
        self.assertTrue(response.data["qr"]["enabled"])
        self.assertTrue(response.data["numeric_code"]["enabled"])
