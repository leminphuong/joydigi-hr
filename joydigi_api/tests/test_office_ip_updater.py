"""
Phase AUTO-OFFICE-PUBLIC-IP-UPDATER.

`POST /api/internal/attendance/update-office-ip/` keeps the office's public
address on the attendance whitelist, so a modem restart stops locking every
employee out of check-in.

The whole feature is one small write guarded by one credential, and both
halves are dangerous in an obvious way: the address is taken from the request
itself, so anything that can make the server accept a request can whitelist
its own network. These tests are mostly about the ways that must not happen —
no credential, a wrong one, a replayed one, a stale one, a mobile user's JWT,
an address asserted in the body — and about the promise that a failure never
costs anybody their ability to check in.
"""

import hmac
import time
from datetime import timedelta
from hashlib import sha256

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from base.models import AttendanceAllowedIP
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_api.office_ip import STATE_KEY, apply_office_ip

URL = "/api/internal/attendance/update-office-ip/"
SECRET = "test-office-secret-not-a-real-one"
OFFICE_IPV4 = "203.0.113.7"
OTHER_IPV4 = "198.51.100.9"
OFFICE_IPV6 = "2001:db8::1"
MANUAL_RANGE = "10.10.0.0/16"


def through_proxy(client_ip):
    """Headers as production presents them: loopback peer, Cloudflare header.

    `resolve_attendance_client_ip` reads `CF-Connecting-IP` only when the peer
    is this host's own proxy, so both halves are needed for the address to be
    believed at all.
    """
    return {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": client_ip}


def signature(timestamp, nonce, secret=SECRET):
    message = ("%s.%s" % (timestamp, nonce)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, sha256).hexdigest()


@override_settings(
    OFFICE_IP_UPDATER_SECRET=SECRET,
    OFFICE_IP_UPDATER_MAX_SKEW_SECONDS=120,
    OFFICE_IP_PREVIOUS_TTL_MINUTES=120,
    CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    },
)
class OfficeIPUpdaterTests(TestCase):
    """The endpoint, end to end."""

    def setUp(self):
        cache.clear()
        self.company = make_company("Office IP Co")
        self.client_api = APIClient()
        self._nonce = 0

    # ------------------------------------------------------------- helpers
    def settings_for_company(self):
        return override_settings(OFFICE_IP_UPDATER_COMPANY_ID=self.company.pk)

    def post(self, client_ip=OFFICE_IPV4, secret=SECRET, body=None, **overrides):
        """A correctly signed request, unless a test breaks it on purpose."""
        self._nonce += 1
        timestamp = overrides.pop("timestamp", str(int(time.time())))
        nonce = overrides.pop("nonce", "nonce-%s-%s" % (id(self), self._nonce))
        headers = {
            "HTTP_X_OFFICE_IP_TIMESTAMP": timestamp,
            "HTTP_X_OFFICE_IP_NONCE": nonce,
            "HTTP_X_OFFICE_IP_SIGNATURE": signature(timestamp, nonce, secret),
        }
        headers.update(through_proxy(client_ip))
        headers.update(overrides)
        return self.client_api.post(URL, body or {}, format="json", **headers)

    def rule(self):
        return AttendanceAllowedIP.objects.filter(company_id=self.company).first()

    def allowed(self):
        rule = self.rule()
        return list((rule.additional_data or {}).get("allowed_ips") or []) if rule else []

    def state(self):
        rule = self.rule()
        return dict((rule.additional_data or {}).get(STATE_KEY) or {}) if rule else {}

    def seed_rule(self, *, enabled=True, allowed_ips=None):
        rule = AttendanceAllowedIP(
            company_id=self.company,
            is_enabled=enabled,
            additional_data={"allowed_ips": list(allowed_ips or [])},
        )
        rule.save()
        return rule

    # ------------------------------------------------------- authentication
    def test_a_request_with_no_credential_is_refused(self):
        with self.settings_for_company():
            response = self.client_api.post(URL, {}, format="json", **through_proxy(OFFICE_IPV4))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["code"], "UNAUTHORIZED")
        self.assertIsNone(self.rule(), "and nothing was written")

    def test_a_wrong_secret_is_refused(self):
        with self.settings_for_company():
            response = self.post(secret="not-the-secret")

        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.rule())

    def test_a_refusal_never_says_which_check_failed(self):
        """An attacker probing this must not learn whether the signature or
        the timestamp was the problem."""
        with self.settings_for_company():
            wrong_secret = self.post(secret="nope")
            stale = self.post(timestamp=str(int(time.time()) - 3600))
            missing = self.client_api.post(URL, {}, format="json", **through_proxy(OFFICE_IPV4))

        bodies = {str(r.data) for r in (wrong_secret, stale, missing)}
        self.assertEqual(len(bodies), 1, "the three refusals are indistinguishable")

    def test_a_stale_timestamp_is_refused(self):
        with self.settings_for_company():
            response = self.post(timestamp=str(int(time.time()) - 600))

        self.assertEqual(response.status_code, 401)

    def test_a_timestamp_from_the_future_is_refused(self):
        with self.settings_for_company():
            response = self.post(timestamp=str(int(time.time()) + 600))

        self.assertEqual(response.status_code, 401)

    def test_a_replayed_request_is_refused(self):
        """The reason this matters: the address comes from the request, so a
        replay that worked would whitelist whoever replayed it."""
        with self.settings_for_company():
            first = self.post(nonce="replay-me-once")
            self.assertEqual(first.status_code, 200)

            replay = self.post(nonce="replay-me-once", client_ip=OTHER_IPV4)

        self.assertEqual(replay.status_code, 401)
        self.assertEqual(
            self.state()["current"],
            "%s/32" % OFFICE_IPV4,
            msg="the replayer's network was not recorded",
        )

    def test_a_normal_mobile_login_cannot_update_the_whitelist(self):
        """An employee's JWT is worth nothing here — whitelisting a network is
        not something any user account may do."""
        user = make_user("office_ip_employee", password="secret123")
        make_employee(
            company=self.company, email="office_ip@test.joydigi", user=user
        )
        authenticated = APIClient()
        authenticated.force_authenticate(user=user)

        with self.settings_for_company():
            response = authenticated.post(URL, {}, format="json", **through_proxy(OFFICE_IPV4))

        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.rule())

    def test_the_feature_is_invisible_when_no_secret_is_configured(self):
        with override_settings(
            OFFICE_IP_UPDATER_SECRET="", OFFICE_IP_UPDATER_COMPANY_ID=self.company.pk
        ):
            response = self.post()

        self.assertEqual(
            response.status_code,
            404,
            msg="a deployment that does not run the agent does not advertise "
            "the endpoint",
        )
        self.assertIsNone(self.rule())

    def test_a_company_id_naming_nothing_is_a_404_not_a_crash(self):
        with override_settings(OFFICE_IP_UPDATER_COMPANY_ID=99999999):
            response = self.post()

        self.assertEqual(response.status_code, 404)

    def test_only_post_is_answered(self):
        with self.settings_for_company():
            for method in (
                self.client_api.get,
                self.client_api.put,
                self.client_api.delete,
                self.client_api.patch,
            ):
                self.assertEqual(method(URL).status_code, 405)

    # -------------------------------------------------------- the address
    def test_an_ipv4_office_is_stored_as_a_single_host(self):
        with self.settings_for_company():
            response = self.post(client_ip=OFFICE_IPV4)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "initialised")
        self.assertIn("%s/32" % OFFICE_IPV4, self.allowed())

    def test_an_ipv6_office_is_stored_as_a_single_host(self):
        with self.settings_for_company():
            response = self.post(client_ip=OFFICE_IPV6)

        self.assertEqual(response.status_code, 200)
        self.assertIn("%s/128" % OFFICE_IPV6, self.allowed())

    def test_an_address_in_the_body_is_ignored(self):
        with self.settings_for_company():
            response = self.post(
                client_ip=OFFICE_IPV4,
                body={"ip": OTHER_IPV4, "allowed_ips": [MANUAL_RANGE], "cidr": "0.0.0.0/0"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.allowed(), ["%s/32" % OFFICE_IPV4])
        joined = " ".join(self.allowed())
        self.assertNotIn(OTHER_IPV4, joined)
        self.assertNotIn("0.0.0.0", joined)

    def test_a_request_the_server_cannot_place_changes_nothing(self):
        """No Cloudflare header means no trustworthy address. Refusing leaves
        the current whitelist in force, which is the safe direction."""
        self.seed_rule(allowed_ips=["%s/32" % OFFICE_IPV4])

        with self.settings_for_company():
            response = self.post(**{"HTTP_CF_CONNECTING_IP": ""})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "CLIENT_IP_UNRESOLVED")
        self.assertEqual(self.allowed(), ["%s/32" % OFFICE_IPV4])

    def test_a_non_proxy_peer_cannot_assert_an_address(self):
        """Reaching the app directly and sending CF-Connecting-IP proves
        nothing, so the resolver refuses to read it."""
        with self.settings_for_company():
            response = self.post(**{"REMOTE_ADDR": "203.0.113.200"})

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.rule())

    # ---------------------------------------------------------- rotation
    def test_a_new_address_rotates_and_keeps_the_old_one_for_a_while(self):
        with self.settings_for_company():
            self.post(client_ip=OFFICE_IPV4)
            response = self.post(client_ip=OTHER_IPV4)

        self.assertEqual(response.data["status"], "rotated")
        self.assertTrue(response.data["previous_retained"])
        self.assertCountEqual(
            self.allowed(), ["%s/32" % OFFICE_IPV4, "%s/32" % OTHER_IPV4]
        )
        self.assertEqual(self.state()["current"], "%s/32" % OTHER_IPV4)
        self.assertEqual(self.state()["previous"], "%s/32" % OFFICE_IPV4)

    def test_the_same_address_again_is_a_no_op(self):
        with self.settings_for_company():
            self.post(client_ip=OFFICE_IPV4)
            response = self.post(client_ip=OFFICE_IPV4)

        self.assertEqual(response.data["status"], "unchanged")
        self.assertEqual(self.allowed(), ["%s/32" % OFFICE_IPV4])
        self.assertFalse(self.state()["previous"])

    def test_an_expired_stand_in_is_pruned_on_the_next_update(self):
        with self.settings_for_company():
            self.post(client_ip=OFFICE_IPV4)
            self.post(client_ip=OTHER_IPV4)

            # Age the stand-in rather than waiting two hours.
            rule = self.rule()
            state = rule.additional_data[STATE_KEY]
            state["previous_expires_at"] = (
                timezone.now() - timedelta(minutes=1)
            ).isoformat()
            rule.save()

            self.post(client_ip=OTHER_IPV4)

        self.assertEqual(self.allowed(), ["%s/32" % OTHER_IPV4])
        self.assertIsNone(self.state()["previous"])

    def test_an_administrators_own_entry_is_never_touched(self):
        """A VPN range, a second office. This feature owns what it added and
        nothing else."""
        self.seed_rule(allowed_ips=[MANUAL_RANGE])

        with self.settings_for_company():
            self.post(client_ip=OFFICE_IPV4)
            self.post(client_ip=OTHER_IPV4)
            rule = self.rule()
            rule.additional_data[STATE_KEY]["previous_expires_at"] = (
                timezone.now() - timedelta(minutes=1)
            ).isoformat()
            rule.save()
            self.post(client_ip=OTHER_IPV4)

        self.assertIn(MANUAL_RANGE, self.allowed())

    def test_an_address_an_administrator_added_by_hand_is_not_pruned(self):
        """Same value, different owner: if the office address was already on
        the list before this feature saw it, it stays after rotation."""
        self.seed_rule(allowed_ips=["%s/32" % OFFICE_IPV4])

        with self.settings_for_company():
            self.post(client_ip=OFFICE_IPV4)
            self.post(client_ip=OTHER_IPV4)
            rule = self.rule()
            rule.additional_data[STATE_KEY]["previous_expires_at"] = (
                timezone.now() - timedelta(minutes=1)
            ).isoformat()
            rule.save()
            self.post(client_ip=OTHER_IPV4)

        self.assertIn(
            "%s/32" % OFFICE_IPV4,
            self.allowed(),
            msg="it was not this feature's entry to remove",
        )

    # --------------------------------------------------- never locks anybody out
    def test_an_absent_rule_is_created_disabled(self):
        with self.settings_for_company():
            response = self.post(client_ip=OFFICE_IPV4)

        self.assertTrue(response.data["created_disabled"])
        self.assertFalse(
            self.rule().is_enabled,
            msg="a background agent must not switch a network restriction on",
        )

    def test_an_enabled_rule_stays_enabled_and_a_disabled_one_stays_disabled(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                AttendanceAllowedIP.objects.all().delete()
                self.seed_rule(enabled=enabled)
                with self.settings_for_company():
                    self.post(client_ip=OFFICE_IPV4)
                self.assertEqual(self.rule().is_enabled, enabled)

    def test_the_list_is_never_emptied(self):
        self.seed_rule(allowed_ips=[MANUAL_RANGE, "%s/32" % OFFICE_IPV4])

        with self.settings_for_company():
            for _ in range(5):
                self.post(client_ip=OTHER_IPV4)

        self.assertTrue(self.allowed())
        self.assertIn(MANUAL_RANGE, self.allowed())

    def test_repeated_updates_do_not_grow_the_list_without_end(self):
        with self.settings_for_company():
            for index in range(6):
                self.post(client_ip="203.0.113.%d" % (10 + index))

        self.assertLessEqual(
            len(self.allowed()),
            3,
            msg="at most the current address, one stand-in, and whatever was "
            "there before",
        )

    # ------------------------------------------------------------- logging
    def test_the_secret_and_the_signature_never_reach_the_log(self):
        with self.settings_for_company():
            with self.assertLogs("joydigi_api", level="INFO") as captured:
                self.post(client_ip=OFFICE_IPV4)
                self.post(secret="wrong-one")

        text = "\n".join(captured.output)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("wrong-one", text)

    def test_the_log_masks_the_office_address(self):
        with self.settings_for_company():
            with self.assertLogs("joydigi_api", level="INFO") as captured:
                self.post(client_ip=OFFICE_IPV4)

        text = "\n".join(captured.output)
        self.assertIn("203.0.x.x", text)
        self.assertNotIn(OFFICE_IPV4, text)

    def test_the_response_does_not_hand_back_a_usable_address(self):
        with self.settings_for_company():
            response = self.post(client_ip=OFFICE_IPV4)

        self.assertNotIn(OFFICE_IPV4, str(response.data))


@override_settings(
    OFFICE_IP_UPDATER_SECRET=SECRET,
    OFFICE_IP_UPDATER_MAX_SKEW_SECONDS=120,
    CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    },
)
class OfficeAgentSignatureTests(TestCase):
    """The office agent and the server must agree on the credential.

    They implement the HMAC independently — one in `tools/office_ip_updater`
    with nothing but the standard library, one in `joydigi_api.office_ip` —
    and a mismatch would fail silently in production: every run refused, the
    whitelist quietly going stale, nobody looking at a scheduled task's exit
    code. So the agent's own header-building runs here against the real
    endpoint.
    """

    def setUp(self):
        cache.clear()
        self.company = make_company("Agent Signature Co")

    @staticmethod
    def load_agent():
        import importlib.util
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        path = root / "tools" / "office_ip_updater" / "office_ip_updater.py"
        spec = importlib.util.spec_from_file_location("office_ip_updater", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_agents_own_headers_are_accepted_by_the_endpoint(self):
        agent = self.load_agent()
        headers = agent.signed_headers(SECRET)
        meta = {
            "HTTP_X_OFFICE_IP_TIMESTAMP": headers["X-Office-IP-Timestamp"],
            "HTTP_X_OFFICE_IP_NONCE": headers["X-Office-IP-Nonce"],
            "HTTP_X_OFFICE_IP_SIGNATURE": headers["X-Office-IP-Signature"],
        }
        meta.update(through_proxy(OFFICE_IPV4))

        with override_settings(OFFICE_IP_UPDATER_COMPANY_ID=self.company.pk):
            response = APIClient().post(URL, {}, format="json", **meta)

        self.assertEqual(response.status_code, 200, msg=response.data)
        self.assertEqual(response.data["status"], "initialised")

    def test_the_agent_signs_the_nonce_too_so_a_swap_is_detected(self):
        agent = self.load_agent()
        headers = agent.signed_headers(SECRET)
        meta = {
            "HTTP_X_OFFICE_IP_TIMESTAMP": headers["X-Office-IP-Timestamp"],
            # A captured signature paired with a fresh nonce must not pass.
            "HTTP_X_OFFICE_IP_NONCE": "a-different-nonce-entirely",
            "HTTP_X_OFFICE_IP_SIGNATURE": headers["X-Office-IP-Signature"],
        }
        meta.update(through_proxy(OFFICE_IPV4))

        with override_settings(OFFICE_IP_UPDATER_COMPANY_ID=self.company.pk):
            response = APIClient().post(URL, {}, format="json", **meta)

        self.assertEqual(response.status_code, 401)

    def test_the_agent_refuses_a_plain_http_url(self):
        agent = self.load_agent()
        import os

        previous = {
            key: os.environ.get(key)
            for key in ("JOYDIGI_OFFICE_IP_URL", "JOYDIGI_OFFICE_IP_SECRET")
        }
        os.environ["JOYDIGI_OFFICE_IP_URL"] = "http://checkin.example.net/api/x/"
        os.environ["JOYDIGI_OFFICE_IP_SECRET"] = SECRET
        try:
            self.assertEqual(
                agent.main(),
                1,
                msg="the credential must never go over a cleartext connection",
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_the_agent_reports_missing_configuration_without_calling_out(self):
        agent = self.load_agent()
        import os

        previous = {
            key: os.environ.get(key)
            for key in ("JOYDIGI_OFFICE_IP_URL", "JOYDIGI_OFFICE_IP_SECRET")
        }
        os.environ.pop("JOYDIGI_OFFICE_IP_URL", None)
        os.environ.pop("JOYDIGI_OFFICE_IP_SECRET", None)
        try:
            self.assertEqual(agent.main(), 1)
        finally:
            for key, value in previous.items():
                if value is not None:
                    os.environ[key] = value


class OfficeIPRotationRuleTests(TestCase):
    """`apply_office_ip` on its own — the decision, without HTTP."""

    def test_the_first_address_initialises(self):
        data, outcome = apply_office_ip({"allowed_ips": []}, "203.0.113.7/32")

        self.assertEqual(outcome, "initialised")
        self.assertEqual(data["allowed_ips"], ["203.0.113.7/32"])
        self.assertIsNone(data[STATE_KEY]["previous"])

    def test_rotation_records_when_the_stand_in_expires(self):
        now = timezone.now()
        first, _ = apply_office_ip({}, "203.0.113.7/32", now=now)
        second, outcome = apply_office_ip(
            first, "198.51.100.9/32", now=now, ttl=timedelta(minutes=30)
        )

        self.assertEqual(outcome, "rotated")
        expires = second[STATE_KEY]["previous_expires_at"]
        self.assertTrue(expires)
        self.assertGreater(
            timezone.datetime.fromisoformat(expires), now + timedelta(minutes=29)
        )

    def test_a_missing_expiry_keeps_the_stand_in_rather_than_dropping_it(self):
        """An unreadable or absent expiry must not become a reason to remove
        an address somebody may still be checking in from."""
        now = timezone.now()
        data, _ = apply_office_ip({}, "203.0.113.7/32", now=now)
        data, _ = apply_office_ip(data, "198.51.100.9/32", now=now)
        data[STATE_KEY]["previous_expires_at"] = "not a timestamp"

        data, _ = apply_office_ip(data, "198.51.100.9/32", now=now)

        self.assertIn("203.0.113.7/32", data["allowed_ips"])

    def test_flapping_between_two_addresses_keeps_both_live(self):
        """A modem alternating between two addresses must not lock out the
        half of the office that just checked in on the other one."""
        now = timezone.now()
        data, _ = apply_office_ip({}, "203.0.113.7/32", now=now)
        data, _ = apply_office_ip(data, "198.51.100.9/32", now=now)
        data, outcome = apply_office_ip(data, "203.0.113.7/32", now=now)

        self.assertEqual(outcome, "rotated")
        self.assertCountEqual(
            data["allowed_ips"], ["203.0.113.7/32", "198.51.100.9/32"]
        )

    def test_it_never_writes_a_wider_range_than_one_host(self):
        data, _ = apply_office_ip({}, "203.0.113.7/32")

        for entry in data["allowed_ips"]:
            self.assertTrue(
                entry.endswith("/32") or entry.endswith("/128"),
                msg="a whitelist entry admits the office, not its neighbours",
            )
