"""Phase AUTH-6G.3 — recent refresh rejections, visible to an admin.

AUTH-6G.2 named the reason a refresh is refused but only in the server
log, which the operator cannot read. This phase keeps the last few
rejections where an admin page can show them.

Two properties matter equally and are tested with equal weight: the
classification is recorded accurately (especially the two
`session_version` integers, which are the whole point), and no token,
header or credential ever reaches the store, the page, or the log.
"""

import json
import os
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api.api_views import refresh_diagnostics
from joydigi_api.api_views.auth_tokens import (
    REJECT_SESSION_REVOKED,
    REJECT_TOKEN_EXPIRED,
    REJECT_TOKEN_INVALID,
    REJECT_USER_INACTIVE,
    REJECT_USER_NOT_FOUND,
)
from joydigi_api.tests.test_token_refresh_sliding import at_day

REFRESH_URL = "/api/auth/token/refresh/"
LOGIN_URL = "/api/auth/login/"
DEBUG_URL = "/attendance/auth-session-debug/"
AUTH_LOGGER = "joydigi_api.api_views.auth.views"


class DiagnosticsBase(TestCase):
    """Each test gets its own diagnostic file, so nothing leaks between
    tests and the developer's real file is never touched."""

    def setUp(self):
        self.diag_path = os.path.join(
            str(settings.BASE_DIR), f"test-auth-diag-{id(self)}.jsonl"
        )
        patcher = mock.patch.object(
            refresh_diagnostics, "_path", return_value=self.diag_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(
            lambda: os.path.exists(self.diag_path)
            and os.remove(self.diag_path)
        )

        self.client = APIClient()
        self.company = make_company("Diag Co")
        self.password = "secret123"
        self.user = make_user("diaguser", password=self.password)
        self.employee = make_employee(
            company=self.company, email="diag@test.joydigi", user=self.user
        )
        login = self.client.post(
            LOGIN_URL,
            {"username": "diaguser", "password": self.password},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.refresh_token = login.data["refresh"]

    def _refresh(self, raw):
        return self.client.post(REFRESH_URL, {"refresh": raw}, format="json")

    def _events(self):
        return refresh_diagnostics.recent_rejections()

    def _raw_file(self):
        if not os.path.exists(self.diag_path):
            return ""
        with open(self.diag_path, encoding="utf-8") as handle:
            return handle.read()


class RecordedReasonTests(DiagnosticsBase):
    def test_a_valid_refresh_records_nothing(self):
        """§11 A"""
        response = self._refresh(self.refresh_token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._events(), [])

    def test_an_expired_refresh_records_token_expired_with_no_subject(self):
        """§11 B / §5 — an unverified token is never decoded to name
        somebody, so there is no user id to record."""
        with at_day(31):
            response = self._refresh(self.refresh_token)

        self.assertEqual(response.status_code, 401)
        event = self._events()[0]
        self.assertEqual(event["reason"], REJECT_TOKEN_EXPIRED)
        self.assertIsNone(event["user_id"])
        self.assertEqual(event["status"], 401)

    def test_a_malformed_refresh_records_token_invalid(self):
        """§11 C / §6"""
        response = self._refresh("not-a-real-token")

        self.assertEqual(response.status_code, 401)
        event = self._events()[0]
        self.assertEqual(event["reason"], REJECT_TOKEN_INVALID)
        self.assertIsNone(event["user_id"])

    def test_an_access_token_used_as_refresh_records_token_invalid(self):
        response = self._refresh(str(AccessToken.for_user(self.user)))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self._events()[0]["reason"], REJECT_TOKEN_INVALID)

    def test_a_deleted_user_records_user_not_found(self):
        """§11 D"""
        token = self.refresh_token
        self.employee.delete()
        type(self.user).objects.filter(pk=self.user.pk).delete()

        response = self._refresh(token)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self._events()[0]["reason"], REJECT_USER_NOT_FOUND)

    def test_an_inactive_user_records_user_inactive(self):
        """§11 E"""
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)

        response = self._refresh(self.refresh_token)

        self.assertEqual(response.status_code, 401)
        event = self._events()[0]
        self.assertEqual(event["reason"], REJECT_USER_INACTIVE)
        self.assertEqual(event["user_id"], self.user.id)


class SessionVersionDetailTests(DiagnosticsBase):
    """§4 / §11 F-G — the two integers that make a revocation
    actionable."""

    def test_a_mismatch_records_both_versions(self):
        token_version = RefreshToken(self.refresh_token)["session_version"]
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=token_version + 1
        )

        response = self._refresh(self.refresh_token)

        self.assertEqual(response.status_code, 401)
        event = self._events()[0]
        self.assertEqual(event["reason"], REJECT_SESSION_REVOKED)
        self.assertEqual(event["user_id"], self.user.id)
        self.assertEqual(event["token_session_version"], token_version)
        self.assertEqual(event["current_session_version"], token_version + 1)

    def test_matching_versions_are_never_classified_as_revoked(self):
        """§11 G — the guard against a false diagnosis."""
        response = self._refresh(self.refresh_token)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            [e for e in self._events() if e["reason"] == REJECT_SESSION_REVOKED]
        )


class SecretSafetyTests(DiagnosticsBase):
    """§9 / §11 M — driven by sentinels, not by inspection."""

    def test_the_raw_token_never_reaches_the_store_or_the_log(self):
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=9999
        )

        with self.assertLogs(AUTH_LOGGER, level="WARNING") as logs:
            self._refresh(self.refresh_token)

        stored = self._raw_file()
        logged = "\n".join(logs.output)

        for blob in (stored, logged):
            self.assertNotIn(self.refresh_token, blob)
            self.assertNotIn("eyJ", blob)
            for forbidden in (
                "Bearer",
                "Authorization",
                "password",
                "secret",
                "cookie",
            ):
                self.assertNotIn(forbidden, blob)

    def test_a_sentinel_token_is_not_persisted(self):
        sentinel = "SENTINEL-REFRESH-TOKEN-DO-NOT-STORE"
        self._refresh(sentinel)

        self.assertNotIn(sentinel, self._raw_file())
        self.assertNotIn(sentinel, json.dumps(self._events()))

    def test_only_allow_listed_fields_are_written(self):
        self._refresh("bad-token")

        for line in self._raw_file().strip().splitlines():
            self.assertEqual(
                set(json.loads(line).keys()),
                set(refresh_diagnostics.ALLOWED_FIELDS),
            )

    def test_the_store_module_writes_no_database_rows(self):
        """§11 L"""
        with open(
            "joydigi_api/api_views/refresh_diagnostics.py", encoding="utf-8"
        ) as handle:
            source = handle.read()
        for forbidden in (
            ".save(",
            ".objects.",
            "bulk_update",
            "raw(",
            "cursor",
            "migrations",
        ):
            self.assertNotIn(forbidden, source, msg=f"{forbidden} found")


class BoundedBufferTests(DiagnosticsBase):
    def test_the_buffer_is_bounded(self):
        """§11 J — a long run of failures must not grow without limit."""
        for _ in range(refresh_diagnostics.TRIM_THRESHOLD + 20):
            refresh_diagnostics.record_rejection(reason=REJECT_TOKEN_INVALID)

        with open(self.diag_path, encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]

        self.assertLessEqual(len(lines), refresh_diagnostics.TRIM_THRESHOLD)
        self.assertLessEqual(
            len(refresh_diagnostics.recent_rejections()),
            refresh_diagnostics.MAX_EVENTS,
        )

    def test_events_are_newest_first(self):
        refresh_diagnostics.record_rejection(reason=REJECT_TOKEN_INVALID)
        refresh_diagnostics.record_rejection(reason=REJECT_USER_INACTIVE)

        self.assertEqual(self._events()[0]["reason"], REJECT_USER_INACTIVE)

    def test_a_broken_store_never_breaks_a_refresh(self):
        """The guarantee the whole design rests on."""
        with mock.patch.object(
            refresh_diagnostics, "_path", side_effect=OSError("disk gone")
        ):
            response = self._refresh("bad-token")

        self.assertEqual(response.status_code, 401)

    def test_reading_a_corrupt_store_returns_no_events(self):
        with open(self.diag_path, "w", encoding="utf-8") as handle:
            handle.write("not json at all\n{partial\n")

        self.assertEqual(refresh_diagnostics.recent_rejections(), [])


class ClientResponseUnchangedTests(DiagnosticsBase):
    """§10 / §11 K — nothing diagnostic may reach the mobile client."""

    def test_two_different_reasons_are_indistinguishable_to_the_client(self):
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=9999
        )
        revoked = self._refresh(self.refresh_token)
        malformed = self._refresh("not-a-real-token")

        self.assertEqual(revoked.status_code, 401)
        self.assertEqual(revoked.status_code, malformed.status_code)
        self.assertEqual(revoked.data, malformed.data)

    def test_the_response_never_carries_diagnostic_detail(self):
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=9999
        )
        body = str(self._refresh(self.refresh_token).data)

        for forbidden in (
            REJECT_SESSION_REVOKED,
            "session_version",
            "token_session_version",
            "current_session_version",
            "reason",
        ):
            self.assertNotIn(forbidden, body)


class AdminPageTests(DiagnosticsBase):
    def setUp(self):
        super().setUp()
        self.admin_user = make_user("diagadmin", password="secret123")
        make_employee(
            company=self.company,
            email="diagadmin@test.joydigi",
            user=self.admin_user,
        )
        self.admin_user.user_permissions.add(
            Permission.objects.get(
                codename="view_employee", content_type__app_label="employee"
            )
        )

    def test_an_admin_sees_a_recent_rejection(self):
        """§11 H"""
        token_version = RefreshToken(self.refresh_token)["session_version"]
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=token_version + 1
        )
        self._refresh(self.refresh_token)

        self.client.logout()
        self.client.login(username="diagadmin", password="secret123")
        body = self.client.get(DEBUG_URL).content.decode()

        self.assertIn("Refresh bị từ chối gần đây", body)
        self.assertIn(REJECT_SESSION_REVOKED, body)
        self.assertIn(f">{token_version}<", body)
        self.assertIn(f">{token_version + 1}<", body)

    def test_the_page_never_shows_a_token(self):
        self._refresh("SENTINEL-REFRESH-TOKEN-DO-NOT-STORE")

        self.client.logout()
        self.client.login(username="diagadmin", password="secret123")
        body = self.client.get(DEBUG_URL).content.decode()

        self.assertNotIn("SENTINEL-REFRESH-TOKEN-DO-NOT-STORE", body)
        self.assertNotIn("eyJ", body)

    def test_an_unauthorized_user_cannot_view_the_rejections(self):
        """§11 I"""
        self._refresh("bad-token")

        self.client.logout()
        self.client.login(username="diaguser", password=self.password)
        body = self.client.get(DEBUG_URL).content.decode()

        self.assertNotIn("Refresh bị từ chối gần đây", body)
        self.assertNotIn(REJECT_TOKEN_INVALID, body)
