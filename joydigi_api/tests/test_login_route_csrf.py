"""
Phase 1.8 regression test.

Confirms `POST /api/auth/login/` actually resolves to and runs
`LoginAPIView` — not the catch-all `custom404` view. Before the fix in
`joydigi_api/apps.py` (dynamic URL registration inserted "api/" *after*
the catch-all `re_path(r"^.*$", custom404)` instead of before it),
every request under `/api/` silently fell through to `custom404`. Since
`custom404` is an ordinary Django view (not `csrf_exempt`), that
misroute also meant every `/api/` POST was subject to Django's normal
CSRF checks — exactly the "CSRF verification failed" HTML page observed
on the real device and reproduced against production in Phase 1.7.

`enforce_csrf_checks=True` is deliberate here: it's what makes this
test fail loudly (an HTML CSRF page, not JSON) if the routing bug
regresses, instead of silently passing via a CSRF-blind test client.
"""

from django.test import Client, TestCase
from django.urls import resolve

from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api.api_views.auth.views import LoginAPIView


class LoginRouteResolutionTests(TestCase):
    """`/api/auth/login/` must resolve to the real DRF view, not the
    catch-all 404 handler."""

    def test_login_path_resolves_to_login_api_view(self):
        match = resolve("/api/auth/login/")
        self.assertIs(match.func.cls, LoginAPIView)

    def test_login_view_callback_is_csrf_exempt(self):
        match = resolve("/api/auth/login/")
        self.assertTrue(getattr(match.func, "csrf_exempt", False))


class LoginRouteCsrfRegressionTests(TestCase):
    """End-to-end: a real POST with CSRF checks enforced (as a browser
    or a strict proxy would apply them) must reach the API and get a
    JSON response — never Django's HTML CSRF-failure page."""

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.company = make_company("Phase18 Co")
        self.password = "diag-local-only-123"
        self.user = make_user("phase18_diag_user", password=self.password)
        make_employee(
            company=self.company,
            email="phase18_diag@test.joydigi",
            user=self.user,
        )

    def _assert_reached_the_api(self, response):
        body = response.content.decode("utf-8", errors="replace")
        self.assertNotIn("CSRF verification failed", body)
        self.assertNotIn("Referer checking failed", body)
        self.assertNotIn("CSRF cookie not set", body)
        self.assertIn(
            response["Content-Type"],
            ("application/json",),
            msg=f"expected a JSON API response, got Content-Type="
            f"{response.get('Content-Type')!r}, status={response.status_code}, "
            f"body[:200]={body[:200]!r}",
        )

    def test_login_with_csrf_enforced_reaches_the_api_and_authenticates(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "phase18_diag_user", "password": self.password},
            content_type="application/json",
        )
        self._assert_reached_the_api(response)
        self.assertEqual(response.status_code, 200)

    def test_login_with_csrf_enforced_and_bad_password_gets_json_401(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "phase18_diag_user", "password": "wrong-password"},
            content_type="application/json",
        )
        self._assert_reached_the_api(response)
        self.assertEqual(response.status_code, 401)
