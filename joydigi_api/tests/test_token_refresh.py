"""Phase AUTH-6B: mobile token-refresh endpoint + single-device login.

Covers: refresh contract, session_version/is_active enforcement on the
refresh path, single-device invalidation, and admin-force-logout +
refresh interplay.
"""

import threading

from django.db import connection
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from joydigi.testkit import make_company, make_employee, make_user

REFRESH_URL = "/api/auth/token/refresh/"


def _login(client, username, password):
    return client.post(
        "/api/auth/login/", {"username": username, "password": password}, format="json"
    )


class TokenRefreshContractTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Refresh Co")
        self.password = "secret123"
        self.user = make_user("refresh_user", password=self.password)
        make_employee(
            company=self.company, email="refresh_user@test.joydigi", user=self.user
        )
        self.login = _login(self.client, "refresh_user", self.password)

    def test_valid_refresh_succeeds(self):
        response = self.client.post(
            REFRESH_URL, {"refresh": self.login.data["refresh"]}, format="json"
        )
        self.assertEqual(response.status_code, 200)

    def test_response_contract_is_exactly_access_and_refresh(self):
        response = self.client.post(
            REFRESH_URL, {"refresh": self.login.data["refresh"]}, format="json"
        )
        self.assertEqual(set(response.data.keys()), {"access", "refresh"})
        self.assertIsInstance(response.data["access"], str)
        self.assertIsInstance(response.data["refresh"], str)

    def test_new_access_carries_current_session_version(self):
        response = self.client.post(
            REFRESH_URL, {"refresh": self.login.data["refresh"]}, format="json"
        )
        self.user.refresh_from_db(fields=["session_version"])
        access = AccessToken(response.data["access"])
        self.assertEqual(access["session_version"], self.user.session_version)

    def test_new_access_actually_authenticates(self):
        response = self.client.post(
            REFRESH_URL, {"refresh": self.login.data["refresh"]}, format="json"
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
        )
        api_response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(api_response.status_code, 200)

    def test_no_user_id_spoof_accepted(self):
        """The request accepts only `refresh` — identity comes solely
        from the token's own claims, never a client-supplied id."""
        other_user = make_user("refresh_other", password="secret123")
        make_employee(
            company=self.company,
            email="refresh_other@test.joydigi",
            user=other_user,
        )
        response = self.client.post(
            REFRESH_URL,
            {
                "refresh": self.login.data["refresh"],
                "user_id": other_user.pk,
                "employee_id": 999,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        access = AccessToken(response.data["access"])
        self.assertEqual(access["user_id"], self.user.pk)

    def test_missing_refresh_field_rejected_safely(self):
        response = self.client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_malformed_refresh_token_rejected(self):
        response = self.client.post(
            REFRESH_URL, {"refresh": "not-a-real-token"}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_access_token_used_as_refresh_is_rejected(self):
        """Wrong token type must not work as a refresh token."""
        response = self.client.post(
            REFRESH_URL, {"refresh": self.login.data["access"]}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_no_raw_password_or_token_in_response_body(self):
        response = self.client.post(
            REFRESH_URL, {"refresh": self.login.data["refresh"]}, format="json"
        )
        blob = str(response.data).lower()
        self.assertNotIn("password", blob)


class TokenRefreshSessionVersionTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Refresh Version Co")
        self.password = "secret123"
        self.user = make_user("refresh_version", password=self.password)
        make_employee(
            company=self.company,
            email="refresh_version@test.joydigi",
            user=self.user,
        )
        self.login = _login(self.client, "refresh_version", self.password)

    def test_admin_bumped_version_rejects_the_old_refresh(self):
        # `self.user` is stale here — login already bumped the DB
        # value in `setUp()` without updating this Python instance, so
        # refresh first to bump from the *true* current value.
        self.user.refresh_from_db(fields=["session_version"])
        self.user.session_version += 1
        self.user.save(update_fields=["session_version"])

        response = self.client.post(
            REFRESH_URL, {"refresh": self.login.data["refresh"]}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_wrong_session_version_in_forged_refresh_rejected(self):
        forged = RefreshToken.for_user(self.user)
        forged["session_version"] = 999
        response = self.client.post(
            REFRESH_URL, {"refresh": str(forged)}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_inactive_user_refresh_rejected(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        response = self.client.post(
            REFRESH_URL, {"refresh": self.login.data["refresh"]}, format="json"
        )
        self.assertEqual(response.status_code, 401)

    def test_inactive_user_does_not_get_reactivated_by_refresh_attempt(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        self.client.post(REFRESH_URL, {"refresh": self.login.data["refresh"]}, format="json")

        self.user.refresh_from_db(fields=["is_active"])
        self.assertFalse(self.user.is_active)


class AdminForceLogoutRefreshInteractionTests(TestCase):
    """Section 20/22 end-to-end: force logout must also kill the
    refresh path, and a subsequent fresh login must work normally
    without any admin "unlock" step."""

    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Force Logout Refresh Co")
        self.password = "secret123"
        self.user = make_user("force_refresh", password=self.password)
        make_employee(
            company=self.company,
            email="force_refresh@test.joydigi",
            user=self.user,
        )

    def test_old_access_and_refresh_both_rejected_after_force_logout(self):
        login = _login(self.client, "force_refresh", self.password)
        old_access = login.data["access"]
        old_refresh = login.data["refresh"]

        from django.db.models import F

        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=F("session_version") + 1
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {old_access}")
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)

        self.client.credentials()
        refresh_response = self.client.post(
            REFRESH_URL, {"refresh": old_refresh}, format="json"
        )
        self.assertEqual(refresh_response.status_code, 401)

    def test_relogin_after_force_logout_works_without_admin_unlock(self):
        _login(self.client, "force_refresh", self.password)

        from django.db.models import F

        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=F("session_version") + 1
        )
        self.user.refresh_from_db(fields=["is_active"])
        self.assertTrue(self.user.is_active)

        relogin = _login(self.client, "force_refresh", self.password)
        self.assertEqual(relogin.status_code, 200)

        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {relogin.data['access']}"
        )
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 200)


class SingleDeviceLoginTests(TestCase):
    """Section 21: a second device's login invalidates the first."""

    def setUp(self):
        self.company = make_company("Single Device Co")
        self.password = "secret123"
        self.user = make_user("single_device", password=self.password)
        make_employee(
            company=self.company,
            email="single_device@test.joydigi",
            user=self.user,
        )
        self.phone_a = APIClient()
        self.phone_b = APIClient()

    def test_second_device_login_invalidates_first_device_access_and_refresh(self):
        login_a = _login(self.phone_a, "single_device", self.password)
        login_b = _login(self.phone_b, "single_device", self.password)

        self.assertEqual(login_a.status_code, 200)
        self.assertEqual(login_b.status_code, 200)
        self.assertNotEqual(login_a.data["access"], login_b.data["access"])

        # Phone A's access is now stale.
        self.phone_a.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login_a.data['access']}"
        )
        response = self.phone_a.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)

        # Phone A's refresh is equally stale.
        self.phone_a.credentials()
        refresh_response = self.phone_a.post(
            REFRESH_URL, {"refresh": login_a.data["refresh"]}, format="json"
        )
        self.assertEqual(refresh_response.status_code, 401)

        # Phone B is unaffected.
        self.phone_b.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login_b.data['access']}"
        )
        response = self.phone_b.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 200)


class ConcurrentLoginTests(TransactionTestCase):
    """Section 3/31.12: the F()-expression atomic bump must not lose
    an increment when two logins race — run with real threads and
    separate DB connections so this is a genuine concurrency test,
    not just two sequential calls."""

    def setUp(self):
        self.company = make_company("Concurrent Login Co")
        self.password = "secret123"
        self.user = make_user("concurrent_login", password=self.password)
        make_employee(
            company=self.company,
            email="concurrent_login@test.joydigi",
            user=self.user,
        )

    def test_two_near_simultaneous_logins_do_not_lose_an_increment(self):
        results = []
        errors = []

        def do_login():
            try:
                client = APIClient()
                response = _login(client, "concurrent_login", self.password)
                results.append(response.status_code)
            except Exception as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=do_login) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(results, [200, 200])
        self.user.refresh_from_db(fields=["session_version"])
        # Started at 0; two real logins, each incrementing by exactly
        # 1, must land on 2 — never 1 (which would mean one increment
        # was lost to a race).
        self.assertEqual(self.user.session_version, 2)
