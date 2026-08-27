"""Phase AUTH-6A.2/AUTH-6B: session_version claim + revocation tests.

Covers login-token issuance (AUTH-6B: every login now bumps
session_version — single-device login), legacy-token backward
compatibility (no claim at all), and the force-logout revocation path
via SessionVersionJWTAuthentication. Token-refresh-endpoint tests live
in `test_token_refresh.py`.
"""

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from joydigi.testkit import make_company, make_employee, make_user


class SessionVersionDefaultTests(TestCase):
    def test_existing_user_default_session_version_is_zero(self):
        user = make_user("sv_default", password="secret123")
        self.assertEqual(user.session_version, 0)


class LoginTokenClaimTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("SV Login Co")
        self.password = "secret123"
        self.user = make_user("sv_login", password=self.password)
        make_employee(
            company=self.company,
            email="sv_login@test.joydigi",
            user=self.user,
        )

    def test_login_bumps_session_version_and_token_carries_it(self):
        """Phase AUTH-6B: login is no longer read-only for
        session_version — every successful login bumps it (single
        device rule), and the minted token carries the *new* value,
        not the pre-login one."""
        self.assertEqual(self.user.session_version, 0)
        response = self.client.post(
            "/api/auth/login/",
            {"username": "sv_login", "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db(fields=["session_version"])
        self.assertEqual(self.user.session_version, 1)
        token = AccessToken(response.data["access"])
        self.assertEqual(token["session_version"], 1)

    def test_login_response_includes_refresh_token(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "sv_login", "password": self.password},
            format="json",
        )
        self.assertIn("refresh", response.data)
        self.assertIsInstance(response.data["refresh"], str)
        self.assertTrue(response.data["refresh"])
        # Existing fields must be unchanged/untouched.
        for key in ("employee", "access", "geo_fencing", "company_id"):
            self.assertIn(key, response.data)

    def test_login_refresh_token_carries_session_version(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "sv_login", "password": self.password},
            format="json",
        )
        refresh = RefreshToken(response.data["refresh"])
        self.user.refresh_from_db(fields=["session_version"])
        self.assertEqual(refresh["session_version"], self.user.session_version)

    def test_second_login_bumps_version_again(self):
        first = self.client.post(
            "/api/auth/login/",
            {"username": "sv_login", "password": self.password},
            format="json",
        )
        first_access = AccessToken(first.data["access"])
        self.assertEqual(first_access["session_version"], 1)

        second = self.client.post(
            "/api/auth/login/",
            {"username": "sv_login", "password": self.password},
            format="json",
        )
        second_access = AccessToken(second.data["access"])
        self.assertEqual(second_access["session_version"], 2)

    def test_login_does_not_change_is_active_or_identity_data(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "sv_login", "password": self.password},
            format="json",
        )
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(
            response.data["employee"]["id"],
            self.user.employee_get.id,
        )

    def test_session_version_zero_token_authenticates(self):
        login = self.client.post(
            "/api/auth/login/",
            {"username": "sv_login", "password": self.password},
            format="json",
        )
        access = login.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 200)

    def test_new_login_after_force_logout_carries_bumped_new_version(self):
        self.user.session_version = 3
        self.user.save(update_fields=["session_version"])
        response = self.client.post(
            "/api/auth/login/",
            {"username": "sv_login", "password": self.password},
            format="json",
        )
        token = AccessToken(response.data["access"])
        self.assertEqual(token["session_version"], 4)


class LegacyTokenCompatibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("SV Legacy Co")
        self.user = make_user("sv_legacy", password="secret123")
        make_employee(
            company=self.company,
            email="sv_legacy@test.joydigi",
            user=self.user,
        )

    def _legacy_token_without_claim(self):
        """A token exactly like the ones minted before this phase —
        `RefreshToken.for_user` + `.access_token`, with no
        `session_version` claim ever set."""
        refresh = RefreshToken.for_user(self.user)
        return str(refresh.access_token)

    def test_legacy_token_without_claim_works_while_user_version_zero(self):
        self.assertEqual(self.user.session_version, 0)
        token = self._legacy_token_without_claim()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 200)

    def test_admin_bump_invalidates_legacy_token(self):
        token = self._legacy_token_without_claim()
        self.user.session_version = 1
        self.user.save(update_fields=["session_version"])

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)


class SessionVersionValidationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("SV Validate Co")
        self.user = make_user("sv_validate", password="secret123")
        make_employee(
            company=self.company,
            email="sv_validate@test.joydigi",
            user=self.user,
        )

    def _token_with_version(self, version):
        refresh = RefreshToken.for_user(self.user)
        access = refresh.access_token
        access["session_version"] = version
        return str(access)

    def test_matching_version_works(self):
        self.user.session_version = 2
        self.user.save(update_fields=["session_version"])
        token = self._token_with_version(2)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 200)

    def test_lower_token_version_rejected(self):
        self.user.session_version = 5
        self.user.save(update_fields=["session_version"])
        token = self._token_with_version(4)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)

    def test_higher_forged_token_version_rejected(self):
        self.user.session_version = 1
        self.user.save(update_fields=["session_version"])
        token = self._token_with_version(999)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)

    def test_inactive_user_still_rejected_same_as_before(self):
        """CHECK_USER_IS_ACTIVE behavior (already live before this
        phase) must be unchanged — still enforced inside the reused
        `super().get_user()` call."""
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        token = self._token_with_version(0)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)

    def test_raw_backend_detail_never_required_by_client(self):
        """The rejection response is a plain 401 — no assertion on its
        body text here, since Flutter must never rely on/display it
        (see error_mapper.dart changes); this only proves the request
        fails safely without a 500."""
        token = self._token_with_version(4)
        self.user.session_version = 5
        self.user.save(update_fields=["session_version"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("password", str(response.data).lower())


class ForceLogoutFlowTests(TestCase):
    """End-to-end: a token minted, then force-logged-out via the same
    mutation the admin view performs, then a fresh login."""

    def setUp(self):
        self.client = APIClient()
        self.company = make_company("SV Flow Co")
        self.password = "secret123"
        self.user = make_user("sv_flow", password=self.password)
        make_employee(
            company=self.company,
            email="sv_flow@test.joydigi",
            user=self.user,
        )

    def test_old_token_rejected_new_token_accepted_after_bump(self):
        login = self.client.post(
            "/api/auth/login/",
            {"username": "sv_flow", "password": self.password},
            format="json",
        )
        old_access = login.data["access"]

        # Simulate the admin force-logout mutation (the view itself is
        # covered separately in employee/tests).
        from django.db.models import F

        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=F("session_version") + 1
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {old_access}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)

        # Employee can log back in immediately — a real login screen
        # never sends the old (now-invalid) bearer token, so the test
        # client shouldn't either.
        self.client.credentials()
        relogin = self.client.post(
            "/api/auth/login/",
            {"username": "sv_flow", "password": self.password},
            format="json",
        )
        self.assertEqual(relogin.status_code, 200)
        new_access = relogin.data["access"]
        self.assertNotEqual(new_access, old_access)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {new_access}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 200)
