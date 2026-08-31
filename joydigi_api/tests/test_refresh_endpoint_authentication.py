"""Phase AUTH-6H — the refresh endpoint must not authenticate the caller.

### The production bug this file reproduces

An employee logs in, uses the app, leaves it for an hour. The access
token expires. The Flutter client does exactly the right thing: it sees
a 401, calls `POST /api/auth/token/refresh/` once, carrying a refresh
token that is still valid for 30 days.

Production answers **401**, the client classifies the session as revoked
and returns to Login — and the AUTH-6G.3 rejection recorder logs
*nothing*, even though AUTH-6G.3A proved the recorder's file is
writable and works.

That silence is the clue. The recorder lives inside
`TokenRefreshAPIView.post()`. If nothing was recorded, the view never
ran — so the 401 came from *before* it:

* `DEFAULT_AUTHENTICATION_CLASSES` is set globally to
  `SessionVersionJWTAuthentication` (joydigi/settings/base.py).
* `TokenRefreshAPIView` overrides only `permission_classes`; it never
  overrides `authentication_classes`, so DRF still authenticates the
  request.
* `AuthInterceptor.onRequest` (Flutter) attaches
  `Authorization: Bearer <access>` to **every** request — its
  `_isAuthEndpoint` exclusion applies to `onError` only, not to the
  outgoing header.

So the refresh call arrives carrying the *expired access token*, DRF's
`perform_authentication()` rejects it during `dispatch()`, and the view
body — the part that actually knows how to validate a refresh token —
is never reached. The refresh token was fine all along.

The endpoint is meant to authenticate the caller **by the refresh token
in the body**, never by an Authorization header. Requiring a valid
access token to obtain a new access token is circular: it makes refresh
work only while it is not needed.
"""

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api.tests.test_token_refresh_sliding import at_day

REFRESH_URL = "/api/auth/token/refresh/"
LOGIN_URL = "/api/auth/login/"


class RefreshBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Refresh Auth Co")
        self.password = "secret123"
        self.user = make_user("refreshauth", password=self.password)
        self.employee = make_employee(
            company=self.company,
            email="refreshauth@test.joydigi",
            user=self.user,
        )
        login = self.client.post(
            LOGIN_URL,
            {"username": "refreshauth", "password": self.password},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.access = login.data["access"]
        self.refresh = login.data["refresh"]

    def _refresh(self, raw=None, authorization=None):
        kwargs = {}
        if authorization is not None:
            kwargs["HTTP_AUTHORIZATION"] = authorization
        return self.client.post(
            REFRESH_URL,
            {"refresh": raw if raw is not None else self.refresh},
            format="json",
            **kwargs,
        )


class ExpiredAuthorizationHeaderTests(RefreshBase):
    """§B — the exact production condition."""

    def test_an_expired_access_header_must_not_block_a_valid_refresh(self):
        """The reproduction.

        One hour later the access token is expired and the refresh token
        has 29 more days. This is precisely the request the app makes,
        and it must succeed.
        """
        with at_day(1):  # access (1h) expired, refresh (30d) still valid
            response = self._refresh(authorization=f"Bearer {self.access}")

        self.assertEqual(
            response.status_code,
            200,
            msg=(
                "refresh was refused because of the stale Authorization "
                "header, not because of the refresh token"
            ),
        )
        self.assertEqual(set(response.data.keys()), {"access", "refresh"})

    def test_the_refreshed_pair_actually_works(self):
        """Not just a 200 — the new access token must authenticate."""
        with at_day(1):
            response = self._refresh(authorization=f"Bearer {self.access}")
            self.assertEqual(response.status_code, 200)

            self.client.credentials(
                HTTP_AUTHORIZATION=f"Bearer {response.data['access']}"
            )
            api = self.client.get("/api/employee/employee-type/")

        self.assertEqual(api.status_code, 200)

    def test_no_authorization_header_still_works(self):
        response = self._refresh()
        self.assertEqual(response.status_code, 200)

    def test_a_garbage_authorization_header_is_ignored(self):
        """The header is not this endpoint's business at all."""
        response = self._refresh(authorization="Bearer not-a-token")
        self.assertEqual(response.status_code, 200)

    def test_an_authorization_header_for_another_user_is_ignored(self):
        """Identity comes from the refresh token in the body, never from
        the header — so a header naming somebody else must neither grant
        nor deny anything."""
        other = make_user("refreshother", password=self.password)
        make_employee(
            company=self.company,
            email="refreshother@test.joydigi",
            user=other,
        )
        other_access = str(RefreshToken.for_user(other).access_token)

        response = self._refresh(authorization=f"Bearer {other_access}")

        self.assertEqual(response.status_code, 200)
        # The minted pair belongs to the refresh token's subject.
        self.assertEqual(
            RefreshToken(response.data["refresh"])["user_id"], self.user.id
        )


class RefreshTokenStillFullyValidatedTests(RefreshBase):
    """§L 4-8 — dropping header authentication must not loosen anything
    about the refresh token itself."""

    def test_an_expired_refresh_is_still_rejected(self):
        with at_day(31):
            response = self._refresh()
        self.assertEqual(response.status_code, 401)

    def test_a_malformed_refresh_is_still_rejected(self):
        self.assertEqual(self._refresh(raw="not-a-token").status_code, 401)

    def test_a_missing_refresh_is_still_rejected(self):
        response = self.client.post(REFRESH_URL, {}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_an_access_token_used_as_refresh_is_still_rejected(self):
        self.assertEqual(self._refresh(raw=self.access).status_code, 401)

    def test_a_wrong_signature_refresh_is_still_rejected(self):
        head, payload, _sig = self.refresh.split(".")
        forged = f"{head}.{payload}.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        self.assertEqual(self._refresh(raw=forged).status_code, 401)

    def test_an_inactive_user_is_still_rejected(self):
        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self._refresh().status_code, 401)

    def test_a_session_version_mismatch_is_still_rejected(self):
        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=9999
        )
        self.assertEqual(self._refresh().status_code, 401)

    def test_a_valid_refresh_does_not_bump_session_version(self):
        self.user.refresh_from_db(fields=["session_version"])
        before = self.user.session_version

        self.assertEqual(self._refresh().status_code, 200)

        self.user.refresh_from_db(fields=["session_version"])
        self.assertEqual(self.user.session_version, before)


class RevocationRegressionTests(RefreshBase):
    """§L 9-10 — the revocation rules that must survive the fix."""

    def test_admin_force_logout_still_invalidates_the_refresh(self):
        from django.db.models import F

        type(self.user).objects.filter(pk=self.user.pk).update(
            session_version=F("session_version") + 1
        )
        # Even with a *fresh* Authorization header, revocation wins.
        self.assertEqual(
            self._refresh(authorization=f"Bearer {self.access}").status_code,
            401,
        )

    def test_a_second_device_login_invalidates_the_first_refresh(self):
        first_refresh = self.refresh

        second = APIClient().post(
            LOGIN_URL,
            {"username": "refreshauth", "password": self.password},
            format="json",
        )
        self.assertEqual(second.status_code, 200)

        self.assertEqual(self._refresh(raw=first_refresh).status_code, 401)
        # ...and the newest device still works.
        self.assertEqual(
            self._refresh(raw=second.data["refresh"]).status_code, 200
        )


class ProtectedEndpointsStillAuthenticateTests(RefreshBase):
    """The fix must be scoped to the refresh endpoint only — ordinary
    protected endpoints must keep rejecting an expired access token."""

    def test_a_protected_endpoint_still_rejects_an_expired_access_token(self):
        with at_day(1):
            self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access}")
            response = self.client.get("/api/employee/employee-type/")

        self.assertEqual(response.status_code, 401)

    def test_a_protected_endpoint_still_rejects_a_missing_token(self):
        self.client.credentials()
        response = self.client.get("/api/employee/employee-type/")
        self.assertEqual(response.status_code, 401)


class LoginEndpointTests(RefreshBase):
    """The same defect lived on `LoginAPIView`, and for the same reason:
    an endpoint that issues credentials must not demand them first.

    In practice the app clears its session before showing Login, so the
    header is usually absent — but an employee arriving with a stale
    token must still be able to sign in rather than meet an unexplained
    401.
    """

    def test_login_works_with_a_stale_authorization_header(self):
        with at_day(1):  # the held access token is now expired
            response = self.client.post(
                LOGIN_URL,
                {"username": "refreshauth", "password": self.password},
                format="json",
                HTTP_AUTHORIZATION=f"Bearer {self.access}",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_login_works_with_a_garbage_authorization_header(self):
        response = self.client.post(
            LOGIN_URL,
            {"username": "refreshauth", "password": self.password},
            format="json",
            HTTP_AUTHORIZATION="Bearer nonsense",
        )
        self.assertEqual(response.status_code, 200)

    def test_wrong_credentials_are_still_refused(self):
        """Dropping header authentication must not weaken the password
        check."""
        response = self.client.post(
            LOGIN_URL,
            {"username": "refreshauth", "password": "wrong-password"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)


class MintedClaimLifetimeTests(RefreshBase):
    """§E — read the lifetimes off the tokens themselves.

    The runtime settings say 60 minutes / 30 days, but settings are a
    statement of intent; these assertions check what `mint_token_pair`
    actually stamps into the claims, which is what the server later
    validates against.
    """

    def test_a_minted_refresh_lives_for_thirty_days(self):
        from datetime import timedelta

        from joydigi_api.api_views.auth_tokens import mint_token_pair

        _access, refresh = mint_token_pair(self.user)

        lifetime = timedelta(seconds=refresh["exp"] - refresh["iat"])
        self.assertEqual(lifetime, timedelta(days=30))

    def test_a_minted_access_lives_for_one_hour(self):
        from datetime import timedelta

        from joydigi_api.api_views.auth_tokens import mint_token_pair

        access, _refresh = mint_token_pair(self.user)

        lifetime = timedelta(seconds=access["exp"] - access["iat"])
        self.assertEqual(lifetime, timedelta(hours=1))

    def test_the_login_response_refresh_also_lives_thirty_days(self):
        """The token the app actually receives, not just one minted in a
        test helper."""
        from datetime import timedelta

        token = RefreshToken(self.refresh)
        self.assertEqual(
            timedelta(seconds=token["exp"] - token["iat"]), timedelta(days=30)
        )
        self.assertEqual(token["token_type"], "refresh")
        self.assertEqual(token["user_id"], self.user.id)
        self.assertIn("jti", token.payload)
        self.assertIn("session_version", token.payload)


class MintedClaimLifetimeTests(RefreshBase):
    """§E — read the lifetimes off the tokens the server actually mints,
    rather than trusting the settings block.

    AUTH-6G.2 read `REFRESH_TOKEN_LIFETIME = 30 days` from the running
    production process, but that only proves what the *setting* says. If
    `mint_token_pair` ever stamped a shorter `exp`, the symptom would
    look identical. These assertions close that gap by decoding the
    claims server-side. No raw token is printed.
    """

    def test_a_minted_refresh_lives_thirty_days(self):
        from datetime import timedelta

        from rest_framework_simplejwt.utils import datetime_from_epoch

        from joydigi_api.api_views.auth_tokens import mint_token_pair

        _access, refresh = mint_token_pair(self.user)
        life = datetime_from_epoch(refresh["exp"]) - datetime_from_epoch(
            refresh["iat"]
        )

        self.assertEqual(life, timedelta(days=30))

    def test_a_minted_access_lives_one_hour(self):
        from datetime import timedelta

        from rest_framework_simplejwt.utils import datetime_from_epoch

        from joydigi_api.api_views.auth_tokens import mint_token_pair

        access, _refresh = mint_token_pair(self.user)
        life = datetime_from_epoch(access["exp"]) - datetime_from_epoch(
            access["iat"]
        )

        self.assertEqual(life, timedelta(hours=1))

    def test_the_refresh_returned_by_the_endpoint_also_lives_thirty_days(self):
        """The same check on the token a real client receives."""
        from datetime import timedelta

        from rest_framework_simplejwt.utils import datetime_from_epoch

        response = self._refresh()
        self.assertEqual(response.status_code, 200)

        issued = RefreshToken(response.data["refresh"])
        life = datetime_from_epoch(issued["exp"]) - datetime_from_epoch(
            issued["iat"]
        )

        self.assertEqual(life, timedelta(days=30))
        self.assertEqual(issued["token_type"], "refresh")
        self.assertEqual(issued["user_id"], self.user.id)
        self.assertIn("jti", issued)
        self.assertIn("session_version", issued)
