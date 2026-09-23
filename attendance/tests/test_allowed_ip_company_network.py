"""Phase 3B FINAL — attendance restricted to the company's own network.

Two things have to hold at once, and they pull against each other.

An employee standing on the office Wi-Fi must be able to check in, which
is the half that has been broken: the synthetic request the mobile API
builds has no `META`, so the address resolved as `None` and *every*
range refused — `0.0.0.0/0` included.

And an employee anywhere else must not be able to check in by *claiming*
to be on the office network. Behind Cloudflare, Django never sees the
real peer; everything about the client is a header, and a header is
something a caller can write. So the spoofing tests below matter more
than the happy path: the happy path failing is an outage, this failing
is the feature not existing.

Addresses here are test data. `183.80.87.150` is written in these tests
because it is the address the company was observed using, and a test that
demonstrates the intended behaviour should use the real shape of it —
but it appears nowhere in application code, in settings, or in any
migration. The ranges a company enforces live in `AttendanceAllowedIP`
and are configured by an administrator, never by this codebase.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.methods.client_ip import (
    TRUSTED_PROXY_PEERS,
    client_ip_is_allowed,
    resolve_attendance_client_ip,
)
from attendance.methods.utils import Request
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceLateComeEarlyOut,
)
from attendance.views.clock_in_out import (
    clock_in_attendance_and_activity,
    perform_clock_in,
)
from base.models import (
    AttendanceAllowedIP,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
)
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

CLOCK_IN = "/api/attendance/clock-in/"
CLOCK_OUT = "/api/attendance/clock-out/"

#: The company's observed public address. Test data only.
COMPANY_IPV4 = "183.80.87.150"
COMPANY_CIDR = "183.80.87.150/32"

#: Somewhere else entirely — a phone on mobile data, a home connection.
OUTSIDE_IPV4 = "203.0.113.45"

#: An IPv6 example range and two addresses, one in it and one not. Taken
#: from the documentation block (RFC 3849), never a real network.
COMPANY_IPV6_CIDR = "2001:db8:1234::/48"
INSIDE_IPV6 = "2001:db8:1234:5::9"
OUTSIDE_IPV6 = "2001:db8:9999::1"

#: What production's nginx looks like from Django's side.
TRUSTED_PEER = "127.0.0.1"

#: What a caller reaching gunicorn directly would look like.
UNTRUSTED_PEER = "198.51.100.77"


def through_proxy(client_ip, peer=TRUSTED_PEER):
    """Headers as production's proxy chain delivers them."""
    return {"REMOTE_ADDR": peer, "HTTP_CF_CONNECTING_IP": client_ip}


class ResolverTests(TestCase):
    """The trust rule, on its own, before any attendance is involved."""

    def resolve(self, **meta):
        class FakeRequest:
            pass

        request = FakeRequest()
        request.META = meta
        return resolve_attendance_client_ip(request)

    # -- A: the real path ------------------------------------------------

    def test_a_trusted_peer_and_a_valid_header_gives_the_client(self):
        resolved = self.resolve(**through_proxy(COMPANY_IPV4))
        self.assertEqual(str(resolved), COMPANY_IPV4)

    def test_ipv6_resolves_as_ipv6(self):
        resolved = self.resolve(**through_proxy(INSIDE_IPV6))
        self.assertEqual(resolved.version, 6)
        self.assertEqual(str(resolved), INSIDE_IPV6)

    def test_ipv6_loopback_is_also_a_trusted_peer(self):
        # A host that resolves `localhost` to ::1 reaches the same proxy
        # by a different route.
        self.assertIn("::1", TRUSTED_PROXY_PEERS)
        self.assertEqual(
            str(self.resolve(**through_proxy(COMPANY_IPV4, peer="::1"))),
            COMPANY_IPV4,
        )

    # -- B, C: spoofing --------------------------------------------------

    def test_b_a_direct_caller_cannot_claim_the_company_address(self):
        self.assertIsNone(
            self.resolve(
                REMOTE_ADDR=UNTRUSTED_PEER, HTTP_CF_CONNECTING_IP=COMPANY_IPV4
            )
        )

    def test_c_a_direct_caller_cannot_claim_it_through_forwarded_for(self):
        self.assertIsNone(
            self.resolve(
                REMOTE_ADDR=UNTRUSTED_PEER,
                HTTP_X_FORWARDED_FOR=f"{COMPANY_IPV4}, 172.70.1.1",
            )
        )

    def test_forwarded_for_is_never_read_even_behind_the_proxy(self):
        # Cloudflare appends to whatever the client sent, so the leading
        # entries are the client's own writing. There is no fallback.
        self.assertIsNone(
            self.resolve(
                REMOTE_ADDR=TRUSTED_PEER,
                HTTP_X_FORWARDED_FOR=f"{COMPANY_IPV4}, 172.70.1.1",
            )
        )

    def test_x_real_ip_is_never_read(self):
        # Production shows it holding the Cloudflare edge, not the client.
        self.assertIsNone(
            self.resolve(REMOTE_ADDR=TRUSTED_PEER, HTTP_X_REAL_IP=COMPANY_IPV4)
        )

    # -- D, E, F: failing closed ----------------------------------------

    def test_d_a_malformed_address_resolves_to_nothing(self):
        for bad in ("not-an-ip", "999.999.999.999", "183.80.87", "", "   "):
            self.assertIsNone(self.resolve(**through_proxy(bad)), bad)

    def test_e_a_missing_header_resolves_to_nothing(self):
        self.assertIsNone(self.resolve(REMOTE_ADDR=TRUSTED_PEER))

    def test_f_two_addresses_in_the_header_resolve_to_nothing(self):
        # Ambiguity is refused rather than resolved by picking one.
        self.assertIsNone(
            self.resolve(**through_proxy(f"{COMPANY_IPV4}, {OUTSIDE_IPV4}"))
        )

    def test_a_request_with_no_meta_at_all_resolves_to_nothing(self):
        class Bare:
            pass

        self.assertIsNone(resolve_attendance_client_ip(Bare()))

    def test_the_synthetic_request_resolves_to_nothing_by_default(self):
        shim = Request(user=None, date=None, time=None, datetime=None)
        self.assertIsNone(resolve_attendance_client_ip(shim))

    def test_a_carried_value_is_still_parsed(self):
        shim = Request(
            user=None, date=None, time=None, datetime=None, client_ip="nonsense"
        )
        self.assertIsNone(resolve_attendance_client_ip(shim))

        shim = Request(
            user=None, date=None, time=None, datetime=None, client_ip=COMPANY_IPV4
        )
        self.assertEqual(str(resolve_attendance_client_ip(shim)), COMPANY_IPV4)


class MembershipTests(TestCase):
    """Which addresses a configured list contains."""

    def allowed(self, ip_text, entries):
        from ipaddress import ip_address

        return client_ip_is_allowed(ip_address(ip_text), entries)

    def test_an_exact_ipv4_is_matched_by_its_slash_32(self):
        self.assertTrue(self.allowed(COMPANY_IPV4, [COMPANY_CIDR]))
        self.assertFalse(self.allowed(OUTSIDE_IPV4, [COMPANY_CIDR]))

    def test_a_bare_address_behaves_as_slash_32(self):
        self.assertTrue(self.allowed(COMPANY_IPV4, [COMPANY_IPV4]))

    def test_an_ipv4_prefix_matches_its_members(self):
        self.assertTrue(self.allowed("183.80.87.9", ["183.80.87.0/24"]))
        self.assertFalse(self.allowed("183.80.88.9", ["183.80.87.0/24"]))

    def test_an_ipv6_prefix_matches_its_members(self):
        self.assertTrue(self.allowed(INSIDE_IPV6, [COMPANY_IPV6_CIDR]))
        self.assertFalse(self.allowed(OUTSIDE_IPV6, [COMPANY_IPV6_CIDR]))

    def test_an_ipv4_list_does_not_match_an_ipv6_client(self):
        # The operational trap: an employee whose phone prefers IPv6 on
        # the office Wi-Fi is refused by an IPv4-only list. Refusing is
        # correct — the list genuinely does not contain them — but it is
        # why both families have to be configured.
        self.assertFalse(self.allowed(INSIDE_IPV6, [COMPANY_CIDR]))

    def test_a_broken_entry_does_not_hide_the_rest_of_the_list(self):
        self.assertTrue(
            self.allowed(COMPANY_IPV4, ["not-a-cidr", None, COMPANY_CIDR])
        )

    def test_nothing_is_allowed_without_an_address(self):
        self.assertFalse(client_ip_is_allowed(None, [COMPANY_CIDR, "0.0.0.0/0"]))

    def test_the_catch_all_ranges_really_catch_all(self):
        # The case that proved the bug: these used to refuse everything.
        self.assertTrue(self.allowed(OUTSIDE_IPV4, ["0.0.0.0/0"]))
        self.assertTrue(self.allowed(OUTSIDE_IPV6, ["::/0"]))


class AttendanceNetworkBase(TestCase):
    """One employee, one company, through the real API."""

    def setUp(self):
        self.company = make_company("Network Co")
        self.user = make_user("networkuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="network@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )
        self.today = timezone.localdate()
        day = EmployeeShiftDay.objects.get(day=self.today.strftime("%A").lower())
        EmployeeShiftSchedule.objects.get_or_create(
            shift_id=self.shift,
            day=day,
            defaults={
                "is_night_shift": False,
                "minimum_working_hour": "08:00",
                "start_time": "08:00:00",
                "end_time": "17:00:00",
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.fresh_user())

    def fresh_user(self):
        # The object `make_employee` returned still caches the Employee
        # from before its work information existed, so its company reads
        # as None and every company-scoped check is silently skipped.
        return type(self.user).objects.get(pk=self.user.pk)

    def enforce(self, *entries):
        AttendanceAllowedIP.objects.create(
            company_id=self.company,
            is_enabled=True,
            additional_data={"allowed_ips": list(entries)},
        )

    def open_session(self, minutes_ago=120):
        day = EmployeeShiftDay.objects.get(day=self.today.strftime("%A").lower())
        started = timezone.localtime() - timedelta(minutes=minutes_ago)
        clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=self.today,
            attendance_date=self.today,
            day=day,
            now=started.strftime("%H:%M"),
            shift=self.shift,
            minimum_hour="08:00",
            start_time=0,
            end_time=1,
            in_datetime=started,
        )

    def world(self):
        """Every row attendance can write, for zero-mutation checks."""
        return (
            list(
                Attendance.objects.order_by("pk").values_list(
                    "pk",
                    "attendance_clock_in",
                    "attendance_clock_out",
                    "attendance_clock_out_date",
                    "attendance_worked_hour",
                    "attendance_validated",
                )
            ),
            list(
                AttendanceActivity.objects.order_by("pk").values_list(
                    "pk", "clock_out", "out_datetime"
                )
            ),
            AttendanceLateComeEarlyOut.objects.count(),
        )

    def post(self, url, **meta):
        return self.client.post(url, **meta)

    def assert_refused(self, response):
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertEqual(response.data["code"], "WIFI_NOT_ALLOWED")
        self.assertIn("không được phép", response.data["message"])


class DisabledIsUnchangedTests(AttendanceNetworkBase):
    """1: with no restriction configured, nothing about IP applies."""

    def test_check_in_succeeds_with_no_allowed_ip_row(self):
        self.assertEqual(self.post(CLOCK_IN).status_code, 200)

    def test_check_in_succeeds_when_the_restriction_is_switched_off(self):
        AttendanceAllowedIP.objects.create(
            company_id=self.company,
            is_enabled=False,
            additional_data={"allowed_ips": [COMPANY_CIDR]},
        )
        # No proxy headers at all, from an address nowhere near the list.
        self.assertEqual(self.post(CLOCK_IN).status_code, 200)

    def test_check_out_succeeds_when_the_restriction_is_switched_off(self):
        AttendanceAllowedIP.objects.create(
            company_id=self.company, is_enabled=False, additional_data={}
        )
        self.open_session()
        self.assertEqual(self.post(CLOCK_OUT).status_code, 200)


class CompanyNetworkTests(AttendanceNetworkBase):
    """2–5, 11, 12: on the office network and off it, both directions."""

    def test_2_the_company_address_may_check_in(self):
        self.enforce(COMPANY_CIDR)
        response = self.post(CLOCK_IN, **through_proxy(COMPANY_IPV4))
        self.assertEqual(response.status_code, 200, response.data)

    def test_12_the_company_address_may_check_out(self):
        self.enforce(COMPANY_CIDR)
        self.open_session()
        response = self.post(CLOCK_OUT, **through_proxy(COMPANY_IPV4))
        self.assertEqual(response.status_code, 200, response.data)

    def test_3_another_address_may_not_check_in(self):
        self.enforce(COMPANY_CIDR)
        self.assert_refused(self.post(CLOCK_IN, **through_proxy(OUTSIDE_IPV4)))

    def test_3_another_address_may_not_check_out(self):
        self.enforce(COMPANY_CIDR)
        self.open_session()
        self.assert_refused(self.post(CLOCK_OUT, **through_proxy(OUTSIDE_IPV4)))

    def test_4_an_address_inside_the_company_prefix_may_check_in(self):
        self.enforce("183.80.87.0/24")
        self.assertEqual(
            self.post(CLOCK_IN, **through_proxy("183.80.87.9")).status_code, 200
        )

    def test_5_ipv6_inside_and_outside_the_configured_prefix(self):
        self.enforce(COMPANY_IPV6_CIDR)
        self.assert_refused(self.post(CLOCK_IN, **through_proxy(OUTSIDE_IPV6)))
        self.assertEqual(
            self.post(CLOCK_IN, **through_proxy(INSIDE_IPV6)).status_code, 200
        )

    def test_both_families_may_be_configured_together(self):
        self.enforce(COMPANY_CIDR, COMPANY_IPV6_CIDR)
        self.assertEqual(
            self.post(CLOCK_IN, **through_proxy(INSIDE_IPV6)).status_code, 200
        )


class FailClosedTests(AttendanceNetworkBase):
    """6, 7: an origin that cannot be established is refused."""

    def test_6_no_proxy_headers_at_all_is_refused(self):
        self.enforce(COMPANY_CIDR, "0.0.0.0/0", "::/0")
        self.assert_refused(self.post(CLOCK_IN))

    def test_7_a_malformed_address_is_refused(self):
        self.enforce("0.0.0.0/0", "::/0")
        self.assert_refused(
            self.post(CLOCK_IN, **through_proxy("183.80.87.150.99"))
        )

    def test_an_ambiguous_header_is_refused(self):
        self.enforce("0.0.0.0/0", "::/0")
        self.assert_refused(
            self.post(CLOCK_IN, **through_proxy(f"{COMPANY_IPV4}, {OUTSIDE_IPV4}"))
        )

    def test_an_empty_allowed_list_refuses_everyone(self):
        self.enforce()
        self.assert_refused(self.post(CLOCK_IN, **through_proxy(COMPANY_IPV4)))


class SpoofingTests(AttendanceNetworkBase):
    """9, 10: claiming the office network must not be enough."""

    def test_9_a_direct_caller_forging_the_cloudflare_header_is_refused(self):
        self.enforce(COMPANY_CIDR)
        self.assert_refused(
            self.post(
                CLOCK_IN,
                REMOTE_ADDR=UNTRUSTED_PEER,
                HTTP_CF_CONNECTING_IP=COMPANY_IPV4,
            )
        )

    def test_10_a_direct_caller_forging_forwarded_for_is_refused(self):
        self.enforce(COMPANY_CIDR)
        self.assert_refused(
            self.post(
                CLOCK_IN,
                REMOTE_ADDR=UNTRUSTED_PEER,
                HTTP_X_FORWARDED_FOR=f"{COMPANY_IPV4}, 172.70.1.1",
            )
        )

    def test_forged_forwarded_for_behind_the_proxy_is_also_refused(self):
        # Cloudflare appends rather than replaces, so even here the
        # leading entry is the client's own writing.
        self.enforce(COMPANY_CIDR)
        self.assert_refused(
            self.post(
                CLOCK_IN,
                REMOTE_ADDR=TRUSTED_PEER,
                HTTP_X_FORWARDED_FOR=f"{COMPANY_IPV4}, 172.70.1.1",
            )
        )

    def test_a_forged_x_real_ip_is_refused(self):
        self.enforce(COMPANY_CIDR)
        self.assert_refused(
            self.post(
                CLOCK_IN, REMOTE_ADDR=TRUSTED_PEER, HTTP_X_REAL_IP=COMPANY_IPV4
            )
        )

    def test_check_out_is_protected_by_the_same_rule(self):
        self.enforce(COMPANY_CIDR)
        self.open_session()
        self.assert_refused(
            self.post(
                CLOCK_OUT,
                REMOTE_ADDR=UNTRUSTED_PEER,
                HTTP_CF_CONNECTING_IP=COMPANY_IPV4,
            )
        )


class NothingIsWrittenOnRefusalTests(AttendanceNetworkBase):
    """16, 17: a refusal changes nothing at all."""

    def test_16_a_refused_check_in_creates_no_row(self):
        self.enforce(COMPANY_CIDR)
        before = self.world()
        self.assert_refused(self.post(CLOCK_IN, **through_proxy(OUTSIDE_IPV4)))
        self.assertEqual(self.world(), before)
        self.assertFalse(Attendance.objects.exists())
        self.assertFalse(AttendanceActivity.objects.exists())

    def test_17_a_refused_check_out_leaves_the_session_open(self):
        self.enforce(COMPANY_CIDR)
        self.open_session()
        before = self.world()
        self.assert_refused(self.post(CLOCK_OUT, **through_proxy(OUTSIDE_IPV4)))
        self.assertEqual(self.world(), before)
        attendance = Attendance.objects.get(employee_id=self.employee)
        self.assertIsNone(attendance.attendance_clock_out)
        self.assertIsNone(attendance.attendance_clock_out_date)

    def test_a_spoofed_refusal_also_writes_nothing(self):
        self.enforce(COMPANY_CIDR)
        before = self.world()
        self.assert_refused(
            self.post(
                CLOCK_IN,
                REMOTE_ADDR=UNTRUSTED_PEER,
                HTTP_CF_CONNECTING_IP=COMPANY_IPV4,
            )
        )
        self.assertEqual(self.world(), before)


class InternalCallerTests(AttendanceNetworkBase):
    """13, 14: the callers that have no employee-side network origin."""

    def test_13_a_trusted_device_is_still_exempt(self):
        # `attendance.scheduler`'s auto-punch-out. Internal
        # infrastructure, no network origin to check, and the existing
        # contract — left exactly as it was.
        self.enforce(COMPANY_CIDR)
        _attendance, allowed, reason = perform_clock_in(
            Request(
                user=self.fresh_user(),
                date=self.today,
                time=timezone.localtime().time(),
                datetime=timezone.localtime(),
                trusted_device=True,
            )
        )
        self.assertTrue(allowed, reason)

    def test_14_an_import_without_an_origin_fails_closed(self):
        # The biometric spreadsheet import. It carries no `client_ip`
        # because the only address it has is the administrator's, and
        # attributing their network to somebody else's punch would let
        # one upload from the office authorise attendance that never
        # happened there. Refusing is the pre-existing behaviour and the
        # correct one; it is asserted so it cannot be loosened by
        # accident.
        self.enforce(COMPANY_CIDR)
        _attendance, allowed, reason = perform_clock_in(
            Request(
                user=self.fresh_user(),
                date=self.today,
                time=timezone.localtime().time(),
                datetime=timezone.localtime(),
            )
        )
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "WIFI_NOT_ALLOWED")

    def test_an_import_is_unaffected_when_no_restriction_is_configured(self):
        _attendance, allowed, _reason = perform_clock_in(
            Request(
                user=self.fresh_user(),
                date=self.today,
                time=timezone.localtime().time(),
                datetime=timezone.localtime(),
            )
        )
        self.assertTrue(allowed)


class RefusalShapeTests(AttendanceNetworkBase):
    """15: the refusal itself is unchanged."""

    def test_15_the_code_and_message_are_the_existing_controlled_ones(self):
        self.enforce(COMPANY_CIDR)
        response = self.post(CLOCK_IN, **through_proxy(OUTSIDE_IPV4))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "WIFI_NOT_ALLOWED")
        self.assertEqual(
            response.data["message"],
            "Mạng hiện tại của bạn không được phép dùng để chấm công.",
        )

    def test_a_refusal_is_never_a_server_error(self):
        self.enforce(COMPANY_CIDR)
        for meta in (
            {},
            through_proxy("garbage"),
            through_proxy(f"{COMPANY_IPV4}, {OUTSIDE_IPV4}"),
            {"REMOTE_ADDR": UNTRUSTED_PEER, "HTTP_CF_CONNECTING_IP": COMPANY_IPV4},
        ):
            response = self.post(CLOCK_IN, **meta)
            self.assertEqual(response.status_code, 400, meta)

    def test_the_company_address_is_not_written_into_the_codebase(self):
        # The ranges belong to configuration. If this ever fails, someone
        # has hard-coded a customer's network into the application.
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        for module in (
            root / "attendance" / "methods" / "client_ip.py",
            root / "attendance" / "views" / "clock_in_out.py",
            root / "joydigi_api" / "api_views" / "attendance" / "views.py",
            root / "joydigi" / "settings" / "base.py",
        ):
            self.assertNotIn(
                COMPANY_IPV4, module.read_text(encoding="utf-8"), str(module)
            )
