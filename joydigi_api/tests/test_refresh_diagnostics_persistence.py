"""Phase AUTH-6G.3A — why was the diagnostic store empty?

AUTH-6G.5 reproduced a real production rejection (refresh -> 401,
`RefreshRevoked`, back to Login) and the admin page still showed
"Chưa ghi nhận lần từ chối nào." The write path swallows every error by
design — a diagnostic must never fail an employee's login — and the
operator has no SSH to read the traceback, so a silent write failure is
invisible.

This phase makes the store's runtime state visible on the page and adds
a POST-only button that writes one clearly-marked synthetic event, so
the write -> read -> display chain can be proven on production in
seconds instead of after another hour-long reproduction.

These tests pin the two things that were actually in doubt (writer and
reader agree on one absolute path; every rejection branch reaches the
recorder) and the safety rules around the new button.
"""

import os
from unittest import mock

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api.api_views import refresh_diagnostics
from joydigi_api.api_views.auth_tokens import (
    REJECT_SESSION_REVOKED,
    REJECT_TOKEN_EXPIRED,
    REJECT_TOKEN_INVALID,
    REJECT_USER_INACTIVE,
    REJECT_USER_NOT_FOUND,
)

DEBUG_URL = "/attendance/auth-session-debug/"
REFRESH_URL = "/api/auth/token/refresh/"
LOGIN_URL = "/api/auth/login/"


class PathConsistencyTests(TestCase):
    """§9 A-B — the failure mode this phase was written to rule out."""

    def test_the_diagnostic_path_is_absolute(self):
        self.assertTrue(os.path.isabs(refresh_diagnostics._path()))

    def test_writer_and_reader_resolve_the_same_path(self):
        """Both go through the one helper, so they cannot diverge —
        asserted by proving a redirected path affects both."""
        seen = []
        real = refresh_diagnostics._path()

        with mock.patch.object(
            refresh_diagnostics, "_path", side_effect=lambda: seen.append(1) or real
        ):
            refresh_diagnostics.record_rejection(reason=REJECT_TOKEN_INVALID)
            refresh_diagnostics.recent_rejections()

        # One call from the writer, one from the reader: neither computes
        # a path of its own.
        self.assertGreaterEqual(len(seen), 2)

    def test_the_module_computes_its_path_in_exactly_one_place(self):
        with open(
            "joydigi_api/api_views/refresh_diagnostics.py", encoding="utf-8"
        ) as handle:
            source = handle.read()
        self.assertEqual(source.count("def _path()"), 1)
        # No other construction of the filename anywhere.
        self.assertEqual(source.count("FILENAME"), 2)  # definition + use in _path


class RejectionBranchCoverageTests(TestCase):
    """§9 K — every rejection reason must reach the recorder, not just
    the logger."""

    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Branch Co")
        self.password = "secret123"
        self.user = make_user("branchuser", password=self.password)
        self.employee = make_employee(
            company=self.company, email="branch@test.joydigi", user=self.user
        )
        login = self.client.post(
            LOGIN_URL,
            {"username": "branchuser", "password": self.password},
            format="json",
        )
        self.refresh_token = login.data["refresh"]

    def _refresh_and_capture(self, raw):
        # Patched where it is *used*, not where it is defined:
        # `auth/views.py` does `from ..refresh_diagnostics import
        # record_rejection`, which binds the function into that module's
        # namespace at import time, so patching the source module would
        # have no effect on the call the view actually makes.
        with mock.patch(
            "joydigi_api.api_views.auth.views.record_rejection"
        ) as recorder:
            response = self.client.post(
                REFRESH_URL, {"refresh": raw}, format="json"
            )
        return response, recorder

    def test_token_invalid_reaches_the_recorder(self):
        response, recorder = self._refresh_and_capture("not-a-token")
        self.assertEqual(response.status_code, 401)
        recorder.assert_called_once()
        self.assertEqual(
            recorder.call_args.kwargs["reason"], REJECT_TOKEN_INVALID
        )

    def test_session_mismatch_reaches_the_recorder_with_both_versions(self):
        token_version = RefreshToken(self.refresh_token)["session_version"]
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=token_version + 1
        )

        response, recorder = self._refresh_and_capture(self.refresh_token)

        self.assertEqual(response.status_code, 401)
        kwargs = recorder.call_args.kwargs
        self.assertEqual(kwargs["reason"], REJECT_SESSION_REVOKED)
        self.assertEqual(kwargs["token_session_version"], token_version)
        self.assertEqual(kwargs["current_session_version"], token_version + 1)

    def test_inactive_user_reaches_the_recorder(self):
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
        response, recorder = self._refresh_and_capture(self.refresh_token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            recorder.call_args.kwargs["reason"], REJECT_USER_INACTIVE
        )

    def test_missing_user_reaches_the_recorder(self):
        token = self.refresh_token
        self.employee.delete()
        type(self.user).objects.filter(pk=self.user.pk).delete()

        response, recorder = self._refresh_and_capture(token)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            recorder.call_args.kwargs["reason"], REJECT_USER_NOT_FOUND
        )

    def test_expired_token_reaches_the_recorder(self):
        from joydigi_api.tests.test_token_refresh_sliding import at_day

        with at_day(31):
            response, recorder = self._refresh_and_capture(self.refresh_token)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            recorder.call_args.kwargs["reason"], REJECT_TOKEN_EXPIRED
        )

    def test_a_writer_oserror_never_changes_the_auth_response(self):
        """§9 L — the guarantee the whole design rests on."""
        with mock.patch.object(
            refresh_diagnostics, "_path", side_effect=OSError("read-only fs")
        ):
            response = self.client.post(
                REFRESH_URL, {"refresh": "not-a-token"}, format="json"
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            set(response.data.keys()), {"error"}
        )

    def test_a_valid_refresh_is_unaffected(self):
        """§9 J"""
        response = self.client.post(
            REFRESH_URL, {"refresh": self.refresh_token}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data.keys()), {"access", "refresh"})


class AdminPageStatusTests(TestCase):
    def setUp(self):
        self.diag_path = os.path.join(
            str(__import__("django.conf", fromlist=["settings"]).settings.BASE_DIR),
            f"test-persist-{id(self)}.jsonl",
        )
        patcher = mock.patch.object(
            refresh_diagnostics, "_path", return_value=self.diag_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(
            lambda: os.path.exists(self.diag_path) and os.remove(self.diag_path)
        )

        self.company = make_company("Status Co")
        self.admin_user = make_user("statusadmin", password="secret123")
        make_employee(
            company=self.company,
            email="statusadmin@test.joydigi",
            user=self.admin_user,
        )
        self.admin_user.user_permissions.add(
            Permission.objects.get(
                codename="view_employee", content_type__app_label="employee"
            )
        )
        self.plain_user = make_user("statusplain", password="secret123")
        make_employee(
            company=self.company,
            email="statusplain@test.joydigi",
            user=self.plain_user,
        )

    def login_admin(self):
        self.client.login(username="statusadmin", password="secret123")

    def test_the_page_reports_file_status(self):
        """§9 C"""
        self.login_admin()
        body = self.client.get(DEBUG_URL).content.decode()

        for field in (
            "DIAGNOSTIC_FILE_ABSOLUTE_PATH",
            "DIAGNOSTIC_PARENT_EXISTS",
            "DIAGNOSTIC_PARENT_WRITABLE",
            "DIAGNOSTIC_FILE_EXISTS",
            "DIAGNOSTIC_FILE_READABLE",
            "DIAGNOSTIC_FILE_WRITABLE",
            "events_read_count",
            "file_size_bytes",
            "last_file_modified_time",
        ):
            self.assertIn(field, body)

    def test_a_get_never_writes(self):
        """§9 I — the rule that keeps this page safe to reload."""
        self.login_admin()
        self.client.get(DEBUG_URL)
        self.assertFalse(os.path.exists(self.diag_path))

    def test_the_test_event_button_writes_and_shows_up(self):
        """§9 G"""
        self.login_admin()
        response = self.client.post(DEBUG_URL, {"write_test_event": "1"})
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("DIAGNOSTIC_TEST", body)
        self.assertIn("Ghi thử THÀNH CÔNG", body)
        self.assertEqual(
            refresh_diagnostics.recent_rejections()[0]["reason"],
            refresh_diagnostics.REASON_DIAGNOSTIC_TEST,
        )

    def test_a_failed_test_write_is_reported_not_swallowed(self):
        """The point of the button: a silent failure must become loud."""
        self.login_admin()
        # Fails the path lookup rather than patching `open` globally,
        # which would also break template loading and mask the point.
        with mock.patch.object(
            refresh_diagnostics,
            "_path",
            side_effect=OSError("read-only file system"),
        ):
            body = self.client.post(
                DEBUG_URL, {"write_test_event": "1"}
            ).content.decode()

        self.assertIn("THẤT BẠI", body)
        self.assertIn("read-only file system", body)

    def test_the_test_event_carries_no_token_or_secret(self):
        """§9 H"""
        self.login_admin()
        self.client.post(DEBUG_URL, {"write_test_event": "1"})

        with open(self.diag_path, encoding="utf-8") as handle:
            stored = handle.read()

        self.assertNotIn("eyJ", stored)
        for forbidden in ("Bearer", "Authorization", "password", "secret"):
            self.assertNotIn(forbidden, stored)

    def test_the_test_reason_is_not_part_of_the_auth_classifier(self):
        """§7 — the refresh endpoint can never produce it."""
        with open(
            "joydigi_api/api_views/auth_tokens.py", encoding="utf-8"
        ) as handle:
            self.assertNotIn("DIAGNOSTIC_TEST", handle.read())

    def test_anonymous_cannot_write_a_test_event(self):
        """§9 D"""
        response = self.client.post(DEBUG_URL, {"write_test_event": "1"})
        self.assertIn(response.status_code, (301, 302))
        self.assertFalse(os.path.exists(self.diag_path))

    def test_a_plain_employee_cannot_write_a_test_event(self):
        """§9 E"""
        self.client.login(username="statusplain", password="secret123")
        self.client.post(DEBUG_URL, {"write_test_event": "1"})
        self.assertFalse(os.path.exists(self.diag_path))

    def test_the_form_carries_a_csrf_token(self):
        """§9 F — a normal Django POST form, CSRF included."""
        self.login_admin()
        body = self.client.get(DEBUG_URL).content.decode()
        self.assertIn("csrfmiddlewaretoken", body)
        self.assertIn('name="write_test_event"', body)

    def test_the_page_still_performs_no_database_writes(self):
        """§9 M-N"""
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
            "migrations",
        ):
            self.assertNotIn(forbidden, source, msg=f"{forbidden} found")
