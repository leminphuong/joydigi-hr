"""Phase AUTH-6D: the mobile session must slide forward on every
successful refresh, so an employee who opens the app at least once
inside any 30-day window never sees Login again just because time
passed — while every AUTH-6B revocation rule stays exactly as strict.

`test_token_refresh.py` already covers the contract and the revocation
rules (admin force logout, second-device login, inactive user). This
file adds the part that was never asserted: the *expiry arithmetic*.

### How simulated time works here
SimpleJWT reads the clock exactly once per token, in `Token.__init__`
(`self.current_time = aware_utcnow()`), and both `set_exp` (minting) and
`check_exp` (validation) are defined in terms of that one value — see
`rest_framework_simplejwt/tokens.py`. Patching that one symbol moves
minting and validation together, with no database writes, no settings
changes and no real waiting.

PyJWT runs its *own* `iat`/`exp` checks inside `TokenBackend.decode`,
against its own module-level clock (`jwt/api_jwt.py`'s
`datetime.now(tz=timezone.utc)`), so that one has to move in step —
otherwise a token legitimately issued at simulated day 29 looks like it
was issued in the future and is rejected as invalid. Both clocks are
therefore advanced together below.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import aware_utcnow, datetime_from_epoch

import jwt.api_jwt as pyjwt_api
import rest_framework_simplejwt.tokens as jwt_tokens

from joydigi.testkit import make_company, make_employee, make_user
from rest_framework.test import APIClient

REFRESH_URL = "/api/auth/token/refresh/"
LOGIN_URL = "/api/auth/login/"


@contextmanager
def at_day(day):
    """Runs the block as if `day` days had passed since the test started.

    Advances SimpleJWT's clock *and* PyJWT's, which are independent: the
    former decides `exp`/`iat` when minting and re-checks `exp` in
    `Token.verify()`, the latter re-checks both inside
    `TokenBackend.decode`. Moving only one makes a token minted at a
    simulated future moment look like it was issued in the future, and
    PyJWT rejects it as invalid — a simulation artifact, not a real
    backend behaviour.
    """
    moment = aware_utcnow() + timedelta(days=day)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    with mock.patch.object(
        jwt_tokens, "aware_utcnow", return_value=moment
    ), mock.patch.object(pyjwt_api, "datetime", _FrozenDatetime):
        yield moment


def refresh_exp(raw_refresh):
    """The `exp` of a raw refresh token, read without validating it —
    an expired token must still be inspectable for these assertions."""
    return datetime_from_epoch(RefreshToken(raw_refresh, verify=False)["exp"])


class TokenLifetimeSettingsTests(TestCase):
    """§16.1/§16.2 — the fix must not be a longer bearer token."""

    def test_access_token_lifetime_is_still_sixty_minutes(self):
        self.assertEqual(
            settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"], timedelta(minutes=60)
        )

    def test_refresh_token_lifetime_is_still_thirty_days(self):
        self.assertEqual(
            settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"], timedelta(days=30)
        )


class SlidingRefreshTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Sliding Co")
        self.password = "secret123"
        self.user = make_user("sliding_user", password=self.password)
        make_employee(
            company=self.company,
            email="sliding_user@test.joydigi",
            user=self.user,
        )
        self.login = self.client.post(
            LOGIN_URL,
            {"username": "sliding_user", "password": self.password},
            format="json",
        )
        self.assertEqual(self.login.status_code, 200)
        self.original_refresh = self.login.data["refresh"]

    def _refresh(self, raw_refresh):
        return self.client.post(
            REFRESH_URL, {"refresh": raw_refresh}, format="json"
        )

    def test_valid_refresh_returns_an_access_token(self):
        """§16.3"""
        response = self._refresh(self.original_refresh)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["access"])

    def test_valid_refresh_returns_a_brand_new_refresh_token(self):
        """§16.4 — a renewal, not the same string echoed back."""
        response = self._refresh(self.original_refresh)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["refresh"])
        self.assertNotEqual(response.data["refresh"], self.original_refresh)

    def test_new_refresh_expiry_slides_forward(self):
        """§16.5 — the heart of the phase: the renewed refresh token
        expires ~30 days from the refresh event, not from the login."""
        with at_day(29):
            response = self._refresh(self.original_refresh)
        self.assertEqual(response.status_code, 200)

        old_exp = refresh_exp(self.original_refresh)
        new_exp = refresh_exp(response.data["refresh"])

        self.assertGreater(new_exp, old_exp)
        # ~day 29 + 30 days = ~day 59, i.e. 29 days beyond the original.
        self.assertAlmostEqual(
            (new_exp - old_exp).total_seconds(),
            timedelta(days=29).total_seconds(),
            delta=60,
        )

    def test_refresh_does_not_change_session_version(self):
        """§3 — a silent refresh must never revoke anything. Only login
        and the admin force-logout action may move this number."""
        # Re-read first: the login in `setUp` already bumped this in the
        # database (single-device rule), so the in-memory copy is stale.
        self.user.refresh_from_db(fields=["session_version"])
        before = self.user.session_version
        response = self._refresh(self.original_refresh)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db(fields=["session_version"])
        self.assertEqual(self.user.session_version, before)

    def test_both_renewed_tokens_carry_the_current_session_version(self):
        """§16.6"""
        response = self._refresh(self.original_refresh)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db(fields=["session_version"])

        new_refresh = RefreshToken(response.data["refresh"])
        self.assertEqual(
            new_refresh["session_version"], self.user.session_version
        )
        # The access token is checked through the same claim by
        # SessionVersionJWTAuthentication.
        self.assertEqual(
            new_refresh.access_token["session_version"],
            self.user.session_version,
        )

    def test_refresh_on_day_29_succeeds(self):
        """§9 Case A / §16.7"""
        with at_day(29):
            response = self._refresh(self.original_refresh)
        self.assertEqual(response.status_code, 200)

    def test_renewed_token_still_works_after_the_original_would_have_died(self):
        """§9 Case B / §16.8 — day 0 login, used on day 29, then day 58.

        The second refresh happens 28 days after the original token's
        own 30-day expiry, which proves the session is being carried by
        the *renewed* token rather than the login's."""
        with at_day(29):
            first = self._refresh(self.original_refresh)
        self.assertEqual(first.status_code, 200)

        with at_day(58):
            second = self._refresh(first.data["refresh"])
        self.assertEqual(
            second.status_code,
            200,
            msg="a session used on day 29 must still be alive on day 58",
        )

        # ...and the original token really is long dead by then.
        with at_day(58):
            stale = self._refresh(self.original_refresh)
        self.assertEqual(stale.status_code, 401)

        # Day 58 + 30 = ~day 88, exactly the "renewed again" step.
        third_exp = refresh_exp(second.data["refresh"])
        self.assertAlmostEqual(
            (third_exp - refresh_exp(self.original_refresh)).total_seconds(),
            timedelta(days=58).total_seconds(),
            delta=60,
        )

    def test_refresh_after_thirty_days_of_inactivity_is_rejected(self):
        """§9 Case C / §16.9 — the desired end of a truly idle session."""
        with at_day(31):
            response = self._refresh(self.original_refresh)
        self.assertEqual(response.status_code, 401)

    def test_repeated_use_keeps_the_session_alive_indefinitely(self):
        """The plain-language promise: open the app at least once every
        30 days and you stay logged in. Walked out over ~5 months."""
        raw = self.original_refresh
        for day in (29, 58, 87, 116, 145):
            with at_day(day):
                response = self._refresh(raw)
            self.assertEqual(
                response.status_code,
                200,
                msg=f"session should still be alive on day {day}",
            )
            raw = response.data["refresh"]
