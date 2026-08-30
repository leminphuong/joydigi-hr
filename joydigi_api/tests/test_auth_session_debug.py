"""Phase AUTH-6G.2 — refresh-rejection classification + admin diagnostic.

AUTH-6G.1 proved on a real device that production refuses a 70-minute-old
refresh token with 401. Every rejection reason returns that same 401 with
the same message on purpose, so the cause cannot be told apart from
outside. This phase names the reason internally — in the server log and
in an admin-only page — without changing a single byte of the response.

The tests therefore care about two things in equal measure: that the
classification is correct, and that nothing new leaks.
"""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api.api_views.auth_tokens import (
    REJECT_SESSION_REVOKED,
    REJECT_TOKEN_EXPIRED,
    REJECT_TOKEN_INVALID,
    REJECT_USER_INACTIVE,
    REJECT_USER_NOT_FOUND,
    classify_refresh_subject,
    resolve_refresh_subject,
)

from joydigi_api.tests.test_token_refresh_sliding import at_day

DEBUG_URL = "/attendance/auth-session-debug/"
REFRESH_URL = "/api/auth/token/refresh/"
LOGIN_URL = "/api/auth/login/"
AUTH_LOGGER = "joydigi_api.api_views.auth.views"


class RefreshBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Auth Debug Co")
        self.password = "secret123"
        self.user = make_user("authdebuguser", password=self.password)
        self.employee = make_employee(
            company=self.company,
            email="authdebug@test.joydigi",
            user=self.user,
        )
        login = self.client.post(
            LOGIN_URL,
            {"username": "authdebuguser", "password": self.password},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.refresh_token = login.data["refresh"]

    def _refresh(self, raw):
        return self.client.post(REFRESH_URL, {"refresh": raw}, format="json")


class ClassificationTests(RefreshBase):
    """§10 K-N — each rejection reason is named correctly."""

    def test_a_valid_refresh_classifies_as_success(self):
        user, reason, _detail = classify_refresh_subject(RefreshToken(self.refresh_token))
        self.assertEqual(user, self.user)
        self.assertIsNone(reason)

    def test_resolve_and_classify_never_disagree(self):
        """The old yes/no helper is now a wrapper, so drift is impossible
        — this pins that."""
        token = RefreshToken(self.refresh_token)
        self.assertEqual(
            resolve_refresh_subject(token), classify_refresh_subject(token)[0]
        )

    def test_session_version_mismatch_is_named(self):
        """§10 M — admin force logout / newer login on another device."""
        token = RefreshToken(self.refresh_token)
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=999
        )

        user, reason, _detail = classify_refresh_subject(token)

        self.assertIsNone(user)
        self.assertEqual(reason, REJECT_SESSION_REVOKED)

    def test_inactive_user_is_named(self):
        """§10 N"""
        token = RefreshToken(self.refresh_token)
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)

        user, reason, _detail = classify_refresh_subject(token)

        self.assertIsNone(user)
        self.assertEqual(reason, REJECT_USER_INACTIVE)

    def test_a_deleted_user_is_named(self):
        token = RefreshToken(self.refresh_token)
        self.employee.delete()
        type(self.user).objects.filter(pk=self.user.pk).delete()

        user, reason, _detail = classify_refresh_subject(token)

        self.assertIsNone(user)
        self.assertEqual(reason, REJECT_USER_NOT_FOUND)


class RejectionLoggingTests(RefreshBase):
    """§8 / §10 K-L, P — the log line, and what must never be in it."""

    def test_an_expired_refresh_logs_token_expired(self):
        """§10 K — the exact production shape: a token past its own
        lifetime."""
        with at_day(31):
            with self.assertLogs(AUTH_LOGGER, level="WARNING") as logs:
                response = self._refresh(self.refresh_token)

        self.assertEqual(response.status_code, 401)
        self.assertIn(f"reason={REJECT_TOKEN_EXPIRED}", "\n".join(logs.output))

    def test_a_malformed_refresh_logs_token_invalid_and_no_user(self):
        """§10 L — an unverified token must not be decoded just to name
        somebody, so no user id may appear."""
        with self.assertLogs(AUTH_LOGGER, level="WARNING") as logs:
            response = self._refresh("not-a-real-token")

        self.assertEqual(response.status_code, 401)
        blob = "\n".join(logs.output)
        self.assertIn(f"reason={REJECT_TOKEN_INVALID}", blob)
        self.assertIn("user_id=-", blob)

    def test_an_access_token_used_as_refresh_logs_token_invalid(self):
        access = str(AccessToken.for_user(self.user))
        with self.assertLogs(AUTH_LOGGER, level="WARNING") as logs:
            response = self._refresh(access)

        self.assertEqual(response.status_code, 401)
        self.assertIn(f"reason={REJECT_TOKEN_INVALID}", "\n".join(logs.output))

    def test_a_revoked_session_logs_the_reason_with_the_user_id(self):
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=999
        )
        with self.assertLogs(AUTH_LOGGER, level="WARNING") as logs:
            response = self._refresh(self.refresh_token)

        self.assertEqual(response.status_code, 401)
        blob = "\n".join(logs.output)
        self.assertIn(f"reason={REJECT_SESSION_REVOKED}", blob)
        self.assertIn(f"user_id={self.user.id}", blob)

    def test_no_raw_jwt_is_ever_logged(self):
        """§10 P — the single most important assertion in this file."""
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=999
        )
        with self.assertLogs(AUTH_LOGGER, level="WARNING") as logs:
            self._refresh(self.refresh_token)
        blob = "\n".join(logs.output)

        self.assertNotIn(self.refresh_token, blob)
        # No JWT-shaped substring at all, and no secret material.
        self.assertNotIn("eyJ", blob)
        for forbidden in ("Bearer", "Authorization", "password", "secret"):
            self.assertNotIn(forbidden, blob)

    def test_a_successful_refresh_logs_nothing_and_still_returns_200(self):
        """§9 / §10 O — behaviour is untouched; only failures are logged."""
        with mock.patch.object(
            __import__(
                "joydigi_api.api_views.auth.views", fromlist=["logger"]
            ).logger,
            "warning",
        ) as warn:
            response = self._refresh(self.refresh_token)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data.keys()), {"access", "refresh"})
        warn.assert_not_called()

    def test_the_response_body_never_reveals_the_reason(self):
        """The classification is internal. Two different causes must be
        indistinguishable to an unauthenticated caller."""
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=999
        )
        revoked = self._refresh(self.refresh_token)
        malformed = self._refresh("not-a-real-token")

        self.assertEqual(revoked.status_code, malformed.status_code)
        self.assertEqual(revoked.data, malformed.data)
        for payload in (revoked.data, malformed.data):
            for reason in (
                REJECT_SESSION_REVOKED,
                REJECT_TOKEN_INVALID,
                REJECT_TOKEN_EXPIRED,
                REJECT_USER_INACTIVE,
            ):
                self.assertNotIn(reason, str(payload))


class DiagnosticPageTests(TestCase):
    def setUp(self):
        self.company = make_company("Debug Page Co")
        self.admin_user = make_user("dbgadmin", password="secret123")
        self.admin = make_employee(
            company=self.company, email="dbgadmin@test.joydigi", user=self.admin_user
        )
        self.admin_user.user_permissions.add(
            Permission.objects.get(
                codename="view_employee", content_type__app_label="employee"
            )
        )
        self.staff_user = make_user("dbgstaff", password="secret123")
        self.staff = make_employee(
            company=self.company, email="dbgstaff@test.joydigi", user=self.staff_user
        )

    def login_admin(self):
        self.client.login(username="dbgadmin", password="secret123")

    def test_anonymous_is_denied(self):
        """§10 A"""
        response = self.client.get(DEBUG_URL)
        self.assertIn(response.status_code, (301, 302))

    def test_a_plain_employee_is_denied(self):
        """§10 B — this project's `permission_required` answers a
        non-HTMX request with 200 + `decorator_404.html` rather than 403,
        so this asserts on content reachability instead of status."""
        self.client.login(username="dbgstaff", password="secret123")
        body = self.client.get(DEBUG_URL).content.decode()

        self.assertNotIn("Chẩn đoán phiên đăng nhập", body)
        self.assertNotIn("REFRESH_TOKEN_LIFETIME", body)

    def test_an_authorized_admin_gets_the_page(self):
        self.login_admin()
        response = self.client.get(DEBUG_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Chẩn đoán phiên đăng nhập", response.content.decode())

    def test_effective_token_lifetimes_are_rendered(self):
        """§10 C-D — read from runtime settings, not from source."""
        self.login_admin()
        body = self.client.get(DEBUG_URL).content.decode()

        self.assertIn("ACCESS_TOKEN_LIFETIME", body)
        self.assertIn("REFRESH_TOKEN_LIFETIME", body)
        self.assertIn("1:00:00", body)
        self.assertIn("30 days, 0:00:00", body)

    @mock.patch.dict(
        "django.conf.settings.SIMPLE_JWT",
        {"REFRESH_TOKEN_LIFETIME": timedelta(minutes=60)},
    )
    def test_a_short_refresh_lifetime_is_flagged_as_a_mismatch(self):
        """The whole point of the page: if production really is handing
        out one-hour refresh tokens, that must be impossible to miss."""
        from rest_framework_simplejwt.settings import api_settings

        with mock.patch.object(
            api_settings, "REFRESH_TOKEN_LIFETIME", timedelta(minutes=60)
        ):
            self.login_admin()
            body = self.client.get(DEBUG_URL).content.decode()

        self.assertIn("LỆCH", body)

    def test_settings_origin_is_reported(self):
        """§10 — LOCAL_SETTINGS_IMPORT_SUPPORTED / FILE_EXISTS booleans."""
        self.login_admin()
        body = self.client.get(DEBUG_URL).content.decode()

        self.assertIn("LOCAL_SETTINGS_IMPORT_SUPPORTED", body)
        self.assertIn("LOCAL_SETTINGS_FILE_EXISTS", body)
        self.assertIn("DJANGO_SETTINGS_MODULE", body)

    def test_no_secrets_are_rendered(self):
        """§10 E-G"""
        self.login_admin()
        body = self.client.get(DEBUG_URL).content.decode()

        self.assertNotIn(settings.SECRET_KEY, body)
        for forbidden in (
            "SIGNING_KEY",
            "VERIFYING_KEY",
            "SECRET_KEY",
            "DATABASE_URL",
            "DB_PASSWORD",
            "AWS_SECRET",
            "os.environ",
        ):
            self.assertNotIn(forbidden, body, msg=f"{forbidden} leaked")

    def test_the_page_never_accepts_a_token(self):
        """§7 — no field, no parameter, no decode endpoint."""
        self.login_admin()
        body = self.client.get(DEBUG_URL).content.decode()

        self.assertNotIn('name="refresh"', body)
        self.assertNotIn('name="token"', body)
        self.assertNotIn("type=\"password\"", body)

        with open(
            "attendance/views/auth_session_debug.py", encoding="utf-8"
        ) as handle:
            source = handle.read()
        for forbidden in ("RefreshToken(", "AccessToken(", "jwt.decode", "os.environ"):
            self.assertNotIn(forbidden, source)

    def test_employee_session_fields_are_shown(self):
        """§10 I"""
        self.login_admin()
        body = self.client.get(
            DEBUG_URL, {"employee_id": self.staff.id}
        ).content.decode()

        self.assertIn("session_version", body)
        self.assertIn("is_active", body)
        self.assertIn(str(self.staff.id), body)

    def test_an_inactive_account_is_called_out(self):
        """§10 J"""
        type(self.staff_user).objects.filter(pk=self.staff_user.pk).update(
            is_active=False
        )
        self.login_admin()
        body = self.client.get(
            DEBUG_URL, {"employee_id": self.staff.id}
        ).content.decode()

        self.assertIn("tài khoản bị vô hiệu", body)

    def test_an_employee_outside_scope_is_not_disclosed(self):
        """§10 H — and the wording gives nothing away about whether the
        row exists at all."""
        other_company = make_company("Other Debug Co")
        other_user = make_user("otherdbg", password="secret123")
        other = make_employee(
            company=other_company, email="otherdbg@test.joydigi", user=other_user
        )

        self.login_admin()
        session = self.client.session
        session["selected_company"] = self.company.id
        session.save()

        body = self.client.get(
            DEBUG_URL, {"employee_id": other.id}
        ).content.decode()

        self.assertNotIn("otherdbg", body)

    def test_the_page_performs_no_writes(self):
        """§10 Q"""
        with open(
            "attendance/views/auth_session_debug.py", encoding="utf-8"
        ) as handle:
            source = handle.read()
        for forbidden in (
            ".save(",
            ".update(",
            ".delete(",
            ".create(",
            "bulk_update",
            "raw(",
            "subprocess",
            "os.system",
        ):
            self.assertNotIn(forbidden, source, msg=f"{forbidden} found")
