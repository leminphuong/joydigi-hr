"""Phase ONLINE-2 — attendance from somewhere else, with permission.

The feature is small; the ways it could go wrong are not. Two of them
matter more than the rest, and most of this file exists for them.

**An approved request must not turn an office day into a remote one.**
Somebody who is allowed to work from home this week, but came in
anyway and checked in on the office network, has an office session.
`OfficeSessionCannotBeClosedRemotelyTests` is the test that says so,
and it is the one to read first: if it ever goes green for the wrong
reason, check-out has become a general way around the company network
restriction for anyone holding any approved request.

**Permission withdrawn at lunchtime must not trap anybody.** The
opposite failure is just as real — an employee who legitimately
started their day at home, whose request is canceled at 14:00, must
still be able to close the session at 17:00.
`CanceledAfterCheckInTests` covers it.

Addresses here are RFC 5737 documentation ranges. No customer network
appears in this file; the ranges a company enforces live in
`AttendanceAllowedIP` and are configured by an administrator.
"""

from datetime import timedelta

from decimal import Decimal
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.methods.remote_work import (
    _accuracy,
    _coordinate,
    _method_code,
    _text,
    _upsert_evidence,
    approved_remote_request,
    remote_check_in_evidence,
)
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceEvidence,
    RemoteWorkRequest,
)
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.models import (
    AttendanceAllowedIP,
    CheckInLocation,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    OfficeWifi,
)
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

CLOCK_IN = "/api/attendance/clock-in/"
CLOCK_OUT = "/api/attendance/clock-out/"

#: The office's public address, as the allow-list would hold it.
OFFICE_IPV4 = "198.51.100.10"
OFFICE_CIDR = "198.51.100.10/32"

#: A home connection, or a phone on mobile data. Anywhere else.
HOME_IPV4 = "203.0.113.45"

TRUSTED_PEER = "127.0.0.1"


def through_proxy(client_ip, peer=TRUSTED_PEER):
    """Headers as production's proxy chain delivers them."""
    return {"REMOTE_ADDR": peer, "HTTP_CF_CONNECTING_IP": client_ip}


class RemoteBase(TestCase):
    """One employee, one company, the office network enforced."""

    def setUp(self):
        self.company = make_company("Remote Co")
        self.user = make_user("remoteuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="remote@test.joydigi", user=self.user
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
        AttendanceAllowedIP.objects.create(
            company_id=self.company,
            is_enabled=True,
            additional_data={"allowed_ips": [OFFICE_CIDR]},
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.fresh_user())

    def fresh_user(self):
        # The cached Employee on `self.user` predates its work
        # information, so its company reads as None and every
        # company-scoped check is silently skipped.
        return type(self.user).objects.get(pk=self.user.pk)

    # -- fixtures --------------------------------------------------------

    def remote_request(self, *, approved=True, canceled=False, is_active=True,
                       start=None, end=None, employee=None):
        return RemoteWorkRequest.objects.create(
            employee_id=employee or self.employee,
            start_date=start or self.today,
            end_date=end or self.today,
            approved=approved,
            canceled=canceled,
            is_active=is_active,
        )

    def office_session(self, minutes_ago=120):
        """A session opened the ordinary way — no evidence row at all."""
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
        return Attendance.objects.get(employee_id=self.employee)

    def backdate_open_activity(self, minutes=120):
        """Age the open session past the 30-minute check-out minimum."""
        started = timezone.localtime() - timedelta(minutes=minutes)
        AttendanceActivity.objects.filter(
            employee_id=self.employee, clock_out__isnull=True
        ).update(clock_in=started.time(), in_datetime=started)
        Attendance.objects.filter(employee_id=self.employee).update(
            attendance_clock_in=started.time()
        )

    def remote_session(self, **kwargs):
        """A session genuinely opened through the remote path."""
        request = self.remote_request(**kwargs)
        response = self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4))
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.backdate_open_activity()
        return request, Attendance.objects.get(employee_id=self.employee)

    def world(self):
        """Every row attendance can write, for zero-mutation checks."""
        return (
            list(
                Attendance.objects.order_by("pk").values_list(
                    "pk",
                    "attendance_clock_in",
                    "attendance_clock_out",
                    "attendance_clock_out_date",
                    "is_validate_request",
                )
            ),
            list(
                AttendanceActivity.objects.order_by("pk").values_list(
                    "pk", "clock_out", "out_datetime"
                )
            ),
            AttendanceEvidence.objects.count(),
        )

    def assert_refused(self, response):
        self.assertEqual(response.status_code, 400, getattr(response, "data", None))
        self.assertEqual(response.data["code"], "WIFI_NOT_ALLOWED")


# ======================================================================
# A. Authorization — what counts as permission
# ======================================================================


class AuthorizationTests(RemoteBase):
    """1-9: `approved_remote_request` answers on four conditions."""

    def test_1_approved_active_uncanceled_covering_today(self):
        request = self.remote_request()
        self.assertEqual(approved_remote_request(self.employee, self.today), request)

    def test_2_no_request_at_all(self):
        self.assertIsNone(approved_remote_request(self.employee, self.today))

    def test_3_pending_request_is_not_permission(self):
        self.remote_request(approved=False)
        self.assertIsNone(approved_remote_request(self.employee, self.today))

    def test_4_canceled_request_is_not_permission(self):
        # `request_status()` reads `canceled` as "Rejected" — there is no
        # separate rejected flag, so this one boolean covers both.
        self.remote_request(approved=True, canceled=True)
        self.assertIsNone(approved_remote_request(self.employee, self.today))

    def test_5_inactive_request_is_not_permission(self):
        self.remote_request(is_active=False)
        self.assertIsNone(approved_remote_request(self.employee, self.today))

    def test_6_another_employees_request_is_not_permission(self):
        other_user = make_user("otheruser", password="secret123")
        other = make_employee(
            company=self.company, email="other@test.joydigi", user=other_user
        )
        self.remote_request(employee=other)
        self.assertIsNone(approved_remote_request(self.employee, self.today))

    def test_7_a_request_that_ended_yesterday_does_not_cover_today(self):
        yesterday = self.today - timedelta(days=1)
        self.remote_request(start=yesterday - timedelta(days=3), end=yesterday)
        self.assertIsNone(approved_remote_request(self.employee, self.today))

    def test_8_a_request_that_starts_tomorrow_does_not_cover_today(self):
        tomorrow = self.today + timedelta(days=1)
        self.remote_request(start=tomorrow, end=tomorrow + timedelta(days=3))
        self.assertIsNone(approved_remote_request(self.employee, self.today))

    def test_9_a_multi_day_request_covering_today(self):
        request = self.remote_request(
            start=self.today - timedelta(days=2), end=self.today + timedelta(days=2)
        )
        self.assertEqual(approved_remote_request(self.employee, self.today), request)

    def test_boundary_days_of_the_range_are_inclusive(self):
        first = self.remote_request(start=self.today, end=self.today + timedelta(days=5))
        self.assertEqual(approved_remote_request(self.employee, self.today), first)
        first.delete()
        last = self.remote_request(start=self.today - timedelta(days=5), end=self.today)
        self.assertEqual(approved_remote_request(self.employee, self.today), last)


# ======================================================================
# B. The office path must not move
# ======================================================================


class OfficeUnchangedTests(RemoteBase):
    """11-13: nothing about an office punch changes."""

    def test_11_no_request_office_network_still_works(self):
        # Phase ONLINE-2F changed what this asserts. It used to require
        # that an office punch wrote no evidence at all; the blocker
        # that review found was caused by exactly that silence, so an
        # office punch now records itself as OFFICE. The attendance
        # behaviour it guards — a plain office check-in succeeds — is
        # unchanged.
        response = self.client.post(CLOCK_IN, **through_proxy(OFFICE_IPV4))
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_OFFICE)
        self.assertIsNone(evidence.remote_work_request)

    def test_12_canceled_request_does_not_break_office_attendance(self):
        self.remote_request(approved=True, canceled=True)
        response = self.client.post(CLOCK_IN, **through_proxy(OFFICE_IPV4))
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_OFFICE)
        self.assertIsNone(evidence.remote_work_request)

    def test_13_approved_request_does_not_make_an_office_punch_remote(self):
        # Permission is permission to work elsewhere, not a declaration
        # that today was worked elsewhere. Somebody who came in anyway
        # has an office session.
        self.remote_request()
        response = self.client.post(CLOCK_IN, **through_proxy(OFFICE_IPV4))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(
            AttendanceEvidence.objects.filter(
                attendance_mode=AttendanceEvidence.MODE_REMOTE
            ).exists()
        )
        # Phase ONLINE-2F — the positive half of the same statement:
        # the session is recorded as OFFICE and carries no permission.
        attendance = Attendance.objects.get(employee_id=self.employee)
        evidence = AttendanceEvidence.objects.get(
            attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_OFFICE)
        self.assertIsNone(evidence.remote_work_request)
        self.assertIsNone(remote_check_in_evidence(attendance))

    def test_canceled_request_does_not_break_office_checkout(self):
        self.remote_request(approved=True, canceled=True)
        self.office_session()
        response = self.client.post(CLOCK_OUT, **through_proxy(OFFICE_IPV4))
        self.assertEqual(response.status_code, 200, response.data)

    def test_office_wifi_still_required_for_an_office_punch(self):
        OfficeWifi.objects.create(
            company_id=self.company, name="Office", ssid="JOYDIGI-OFFICE"
        )
        before = self.world()
        response = self.client.post(
            CLOCK_IN, {"wifi_ssid": "SomeoneElses-WiFi"}, **through_proxy(OFFICE_IPV4)
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "WIFI_NOT_ALLOWED")
        self.assertEqual(self.world(), before)


# ======================================================================
# C. Remote check-in
# ======================================================================


class RemoteCheckInTests(RemoteBase):
    """14-19: permission opens the session, and says so in writing."""

    def test_14_approved_request_allows_check_in_from_elsewhere(self):
        self.remote_request()
        response = self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4))
        self.assertEqual(response.status_code, 200, response.data)

    def test_15_check_in_evidence_is_written(self):
        request = self.remote_request()
        self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4))
        attendance = Attendance.objects.get(employee_id=self.employee)
        evidence = AttendanceEvidence.objects.get(
            attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_REMOTE)
        self.assertEqual(evidence.remote_work_request, request)
        self.assertIsNotNone(evidence.captured_at)

    def test_16_a_remote_punch_does_not_enter_the_approval_queue(self):
        # It was approved once already, before the day began. Asking
        # again would be a second approval for the same decision.
        self.remote_request()
        self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4))
        attendance = Attendance.objects.get(employee_id=self.employee)
        self.assertFalse(attendance.is_validate_request)
        self.assertFalse(attendance.is_validate_request_approved)

    def test_17_no_request_from_elsewhere_is_refused_exactly_as_before(self):
        before = self.world()
        self.assert_refused(self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4)))
        self.assertEqual(self.world(), before)

    def test_18_a_request_that_is_not_permission_does_not_help(self):
        other_user = make_user("stranger", password="secret123")
        other = make_employee(
            company=self.company, email="stranger@test.joydigi", user=other_user
        )
        for kwargs in (
            {"approved": False},
            {"approved": True, "canceled": True},
            {"is_active": False},
            {"start": self.today - timedelta(days=5),
             "end": self.today - timedelta(days=1)},
            {"start": self.today + timedelta(days=1),
             "end": self.today + timedelta(days=5)},
            {"employee": other},
        ):
            RemoteWorkRequest.objects.all().delete()
            self.remote_request(**kwargs)
            before = self.world()
            self.assert_refused(
                self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4))
            )
            self.assertEqual(self.world(), before, kwargs)

    def test_19_a_refused_remote_check_in_writes_nothing_at_all(self):
        before = self.world()
        self.assert_refused(self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4)))
        self.assertEqual(self.world(), before)
        self.assertFalse(Attendance.objects.exists())
        self.assertFalse(AttendanceActivity.objects.exists())
        self.assertFalse(AttendanceEvidence.objects.exists())

    def test_home_wifi_does_not_have_to_match_an_office_network(self):
        OfficeWifi.objects.create(
            company_id=self.company, name="Office", ssid="JOYDIGI-OFFICE"
        )
        self.remote_request()
        response = self.client.post(
            CLOCK_IN, {"wifi_ssid": "Home-WiFi-5G"}, **through_proxy(HOME_IPV4)
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.wifi_ssid, "Home-WiFi-5G")
        self.assertEqual(evidence.method, AttendanceEvidence.METHOD_WIFI)

    def test_an_invalid_proof_is_still_refused_with_permission_in_hand(self):
        # Permission answers "you are not at the office". It does not
        # answer "this proof does not check out".
        self.remote_request()
        before = self.world()
        response = self.client.post(
            CLOCK_IN, {"verification_proof": "not-a-proof"},
            **through_proxy(HOME_IPV4),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "VERIFICATION_REQUIRED")
        self.assertEqual(self.world(), before)

    def test_malformed_coordinates_are_still_refused_with_permission(self):
        self.remote_request()
        before = self.world()
        response = self.client.post(
            CLOCK_IN, {"latitude": "not-a-number", "longitude": "10"},
            **through_proxy(HOME_IPV4),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "LOCATION_INVALID")
        self.assertEqual(self.world(), before)


# ======================================================================
# D. Remote check-out — the security-critical half
# ======================================================================


class RemoteCheckOutTests(RemoteBase):
    """20-22: a session opened remotely may be closed remotely."""

    def test_20_remote_session_can_be_closed_from_elsewhere(self):
        self.remote_session()
        response = self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4))
        self.assertEqual(response.status_code, 200, response.data)

    def test_21_check_out_evidence_is_written(self):
        _request, attendance = self.remote_session()
        self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4))
        evidence = AttendanceEvidence.objects.get(
            attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_OUT
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_REMOTE)

    def test_22_check_out_carries_the_same_request_as_check_in(self):
        request, attendance = self.remote_session()
        # A second, also-valid request exists; the pair must still
        # describe the one authorisation the session began under.
        self.remote_request(start=self.today, end=self.today + timedelta(days=3))
        self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4))
        check_in = AttendanceEvidence.objects.get(
            attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_IN
        )
        check_out = AttendanceEvidence.objects.get(
            attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_OUT
        )
        self.assertEqual(check_in.remote_work_request, request)
        self.assertEqual(check_out.remote_work_request, request)


class OfficeSessionCannotBeClosedRemotelyTests(RemoteBase):
    """23-24: the bypass this feature must never become.

    Read this class before changing anything in `perform_clock_out`.
    """

    def test_23_office_session_plus_approved_request_cannot_close_remotely(self):
        # Checked in at the office. Holds an approved request for today.
        # Tries to close the day from a phone on mobile data. The
        # request is permission to *start* elsewhere, and this session
        # did not start elsewhere.
        attendance = self.office_session()
        self.remote_request()
        self.assertIsNone(remote_check_in_evidence(attendance))

        before = self.world()
        self.assert_refused(self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4)))
        self.assertEqual(self.world(), before)

        attendance.refresh_from_db()
        self.assertIsNone(attendance.attendance_clock_out)
        self.assertIsNone(attendance.attendance_clock_out_date)

    def test_23b_the_same_office_session_still_closes_at_the_office(self):
        self.office_session()
        self.remote_request()
        response = self.client.post(CLOCK_OUT, **through_proxy(OFFICE_IPV4))
        self.assertEqual(response.status_code, 200, response.data)

    def test_24_a_client_cannot_declare_itself_remote(self):
        attendance = self.office_session()
        request = self.remote_request()
        before = self.world()
        self.assert_refused(
            self.client.post(
                CLOCK_OUT,
                {
                    "remote": True,
                    "online": "true",
                    "attendance_mode": "REMOTE",
                    "remote_work_request_id": request.pk,
                },
                **through_proxy(HOME_IPV4),
            )
        )
        self.assertEqual(self.world(), before)
        self.assertIsNone(remote_check_in_evidence(attendance))

    def test_a_client_cannot_declare_itself_remote_at_check_in_either(self):
        before = self.world()
        self.assert_refused(
            self.client.post(
                CLOCK_IN,
                {"remote": True, "attendance_mode": "REMOTE", "online": "true"},
                **through_proxy(HOME_IPV4),
            )
        )
        self.assertEqual(self.world(), before)

    def test_no_evidence_means_no_remote_checkout_even_for_the_same_day(self):
        self.office_session()
        self.remote_request()
        self.assertFalse(
            AttendanceEvidence.objects.filter(
                attendance_mode=AttendanceEvidence.MODE_REMOTE
            ).exists()
        )
        self.assert_refused(self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4)))


class CanceledAfterCheckInTests(RemoteBase):
    """25: withdrawing permission must not strand an open session."""

    def test_25_canceled_after_check_in_can_still_check_out_remotely(self):
        request, _attendance = self.remote_session()
        # 14:00 — the manager withdraws the permission.
        request.canceled = True
        request.save()
        self.assertIsNone(approved_remote_request(self.employee, self.today))

        # 17:00 — the employee is still at home, with a session open.
        response = self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4))
        self.assertEqual(response.status_code, 200, response.data)

    def test_25b_but_a_new_check_in_is_no_longer_authorised(self):
        request, _attendance = self.remote_session()
        self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4))
        request.canceled = True
        request.save()
        # Same day, same employee, session already closed: permission is
        # required again, and is now gone.
        self.assert_refused(self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4)))


# ======================================================================
# E. What the record contains
# ======================================================================


class EvidenceTests(RemoteBase):
    """26-30: the server writes it, not the client."""

    def office_location(self):
        return CheckInLocation.objects.create(
            company_id=self.company,
            name="HQ",
            latitude="21.028511",
            longitude="105.804817",
            radius_meters=200,
        )

    def test_26_distance_is_computed_from_coordinates(self):
        location = self.office_location()
        self.remote_request()
        # Roughly 1.5km north of HQ.
        response = self.client.post(
            CLOCK_IN,
            {"latitude": "21.042000", "longitude": "105.804817"},
            **through_proxy(HOME_IPV4),
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.location, location)
        self.assertIsNotNone(evidence.distance_meters)
        self.assertGreater(evidence.distance_meters, 1000)
        self.assertLess(evidence.distance_meters, 2000)
        self.assertEqual(evidence.method, AttendanceEvidence.METHOD_LOCATION)

    def test_27_a_client_supplied_distance_is_ignored(self):
        self.office_location()
        self.remote_request()
        self.client.post(
            CLOCK_IN,
            {
                "latitude": "21.042000",
                "longitude": "105.804817",
                "distance": 7,
                "distance_meters": 7,
            },
            **through_proxy(HOME_IPV4),
        )
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertNotEqual(evidence.distance_meters, 7)
        self.assertGreater(evidence.distance_meters, 1000)

    def test_28_missing_coordinates_do_not_block_a_remote_punch(self):
        # Coordinates are description, not authority. A handset that
        # reports its network but cannot get a fix — indoors, location
        # services off — still checks in, and the record simply says
        # nothing about where.
        self.office_location()
        self.remote_request()
        response = self.client.post(
            CLOCK_IN, {"wifi_ssid": "Home-WiFi"}, **through_proxy(HOME_IPV4)
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertIsNone(evidence.latitude)
        self.assertIsNone(evidence.longitude)
        self.assertIsNone(evidence.distance_meters)
        self.assertIsNone(evidence.location)

    def test_28b_a_punch_carrying_no_evidence_at_all_is_still_refused(self):
        # Pins down a boundary rather than blessing it. Once a company
        # has configured locations, `validate_checkin_source` refuses a
        # punch that supplies nothing whatsoever, with the same
        # VERIFICATION_REQUIRED it uses for a proof that did not check
        # out. The two are indistinguishable by code, so remote
        # permission deliberately overrides neither — widening it would
        # also admit forged proofs. Reported as a limitation; changing
        # it needs a decision, not a quiet edit here.
        self.office_location()
        self.remote_request()
        response = self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "VERIFICATION_REQUIRED")

    def test_29_client_ip_comes_from_the_resolver_not_the_payload(self):
        self.remote_request()
        self.client.post(
            CLOCK_IN,
            {"client_ip": "10.0.0.1", "ip": "10.0.0.1"},
            **through_proxy(HOME_IPV4),
        )
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.client_ip, HOME_IPV4)

    def test_29b_an_unresolvable_origin_records_no_address(self):
        # A caller whose peer is not this host's proxy: the forwarded
        # header is unverifiable, so Phase 3B answers None and the
        # record says nothing rather than something untrue. The punch
        # itself is authorised by the request, not by the address.
        self.remote_request()
        response = self.client.post(
            CLOCK_IN, REMOTE_ADDR="198.51.100.77", HTTP_CF_CONNECTING_IP=HOME_IPV4
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertIsNone(evidence.client_ip)

    def test_30_one_evidence_row_per_attendance_per_action(self):
        _request, attendance = self.remote_session()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AttendanceEvidence.objects.create(
                    attendance=attendance,
                    action=AttendanceEvidence.ACTION_CHECK_IN,
                    attendance_mode=AttendanceEvidence.MODE_REMOTE,
                    captured_at=timezone.localtime(),
                )

    def test_a_second_remote_check_in_updates_rather_than_duplicates(self):
        # Attendance is unique per employee per date, so checking in,
        # out and in again lands on the same row. The record must keep
        # describing the session that is currently open.
        self.remote_session()
        self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4))
        response = self.client.post(
            CLOCK_IN, {"wifi_ssid": "Second-Network"}, **through_proxy(HOME_IPV4)
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            AttendanceEvidence.objects.filter(
                action=AttendanceEvidence.ACTION_CHECK_IN
            ).count(),
            1,
        )
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.wifi_ssid, "Second-Network")


# ======================================================================
# F. Phase ONLINE-2F — the check-in row describes the CURRENT session
# ======================================================================


class CurrentSessionEvidenceTests(RemoteBase):
    """The blocker ONLINE-2R found, and the invariant that closes it.

    `Attendance` is unique per employee per date and
    `unique(attendance, action)` allows one check-in row per
    attendance, so the row cannot be a history — it is a statement
    about the session that is open *now*. Everything here exists to
    prove that statement stays true when a day contains more than one
    session.
    """

    def test_a_office_check_in_after_a_remote_session_blocks_remote_checkout(self):
        # THE BLOCKER, in the order it actually happens: work the
        # morning from home, come into the office after lunch, then try
        # to close the day from mobile data on the way home.
        request = self.remote_request()

        # 08:00 — from home.
        self.assertEqual(
            self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4)).status_code, 200
        )
        attendance = Attendance.objects.get(employee_id=self.employee)
        self.assertEqual(remote_check_in_evidence(attendance).remote_work_request,
                         request)

        # 12:00 — closed from home.
        self.backdate_open_activity()
        self.assertEqual(
            self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4)).status_code, 200
        )

        # 14:00 — back at the office. Same Attendance row is reopened.
        self.assertEqual(
            self.client.post(CLOCK_IN, **through_proxy(OFFICE_IPV4)).status_code, 200
        )
        self.assertEqual(Attendance.objects.filter(employee_id=self.employee).count(), 1)
        evidence = AttendanceEvidence.objects.get(
            attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_OFFICE)
        self.assertIsNone(evidence.remote_work_request)
        self.assertIsNone(remote_check_in_evidence(attendance))

        # 18:00 — mobile data. The morning's permission is spent; this
        # session was opened at the office.
        self.backdate_open_activity()
        before = self.world()
        self.assert_refused(self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4)))
        self.assertEqual(self.world(), before)
        attendance.refresh_from_db()
        self.assertIsNone(attendance.attendance_clock_out)

        # ...and it still closes from the office.
        self.assertEqual(
            self.client.post(CLOCK_OUT, **through_proxy(OFFICE_IPV4)).status_code, 200
        )

    def test_b_approved_request_plus_office_check_in_records_office(self):
        self.remote_request()
        self.assertEqual(
            self.client.post(CLOCK_IN, **through_proxy(OFFICE_IPV4)).status_code, 200
        )
        attendance = Attendance.objects.get(employee_id=self.employee)
        evidence = AttendanceEvidence.objects.get(
            attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_OFFICE)
        self.assertIsNone(evidence.remote_work_request)
        self.assertIsNone(remote_check_in_evidence(attendance))

    def test_c_office_session_then_a_remote_session_the_same_day(self):
        # The other direction: the row must be able to change from
        # OFFICE to REMOTE too, or somebody who came in first could
        # never work the afternoon from home.
        self.assertEqual(
            self.client.post(CLOCK_IN, **through_proxy(OFFICE_IPV4)).status_code, 200
        )
        attendance = Attendance.objects.get(employee_id=self.employee)
        self.assertIsNone(remote_check_in_evidence(attendance))
        self.backdate_open_activity()
        self.assertEqual(
            self.client.post(CLOCK_OUT, **through_proxy(OFFICE_IPV4)).status_code, 200
        )

        request = self.remote_request()
        self.assertEqual(
            self.client.post(CLOCK_IN, **through_proxy(HOME_IPV4)).status_code, 200
        )
        evidence = AttendanceEvidence.objects.get(
            attendance=attendance, action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_REMOTE)
        self.assertEqual(evidence.remote_work_request, request)

        self.backdate_open_activity()
        self.assertEqual(
            self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4)).status_code, 200
        )

    def test_reopening_a_session_retires_the_previous_check_out_row(self):
        # A leftover CHECK_OUT would claim the newly opened session was
        # already closed.
        self.remote_session()
        self.assertEqual(
            self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4)).status_code, 200
        )
        self.assertTrue(
            AttendanceEvidence.objects.filter(
                action=AttendanceEvidence.ACTION_CHECK_OUT
            ).exists()
        )
        self.assertEqual(
            self.client.post(CLOCK_IN, **through_proxy(OFFICE_IPV4)).status_code, 200
        )
        self.assertFalse(
            AttendanceEvidence.objects.filter(
                action=AttendanceEvidence.ACTION_CHECK_OUT
            ).exists()
        )

    def test_a_remote_check_out_row_on_its_own_authorises_nothing(self):
        # Authorisation reads the check-in row and only the check-in
        # row. A CHECK_OUT marked REMOTE must not stand in for it.
        attendance = self.office_session()
        self.remote_request()
        AttendanceEvidence.objects.create(
            attendance=attendance,
            action=AttendanceEvidence.ACTION_CHECK_OUT,
            attendance_mode=AttendanceEvidence.MODE_REMOTE,
            captured_at=timezone.localtime(),
        )
        self.assertIsNone(remote_check_in_evidence(attendance))
        self.assert_refused(self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4)))

    def test_another_attendances_remote_evidence_authorises_nothing(self):
        colleague_user = make_user("colleague", password="secret123")
        colleague = make_employee(
            company=self.company, email="colleague@test.joydigi", user=colleague_user
        )
        day = EmployeeShiftDay.objects.get(day=self.today.strftime("%A").lower())
        started = timezone.localtime() - timedelta(minutes=120)
        clock_in_attendance_and_activity(
            employee=colleague,
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
        theirs = Attendance.objects.get(employee_id=colleague)
        AttendanceEvidence.objects.create(
            attendance=theirs,
            action=AttendanceEvidence.ACTION_CHECK_IN,
            attendance_mode=AttendanceEvidence.MODE_REMOTE,
            captured_at=timezone.localtime(),
        )

        mine = self.office_session()
        self.remote_request()
        self.assertIsNotNone(remote_check_in_evidence(theirs))
        self.assertIsNone(remote_check_in_evidence(mine))
        self.assert_refused(self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4)))


# ======================================================================
# G. Phase ONLINE-2H — no row lock, and nothing the database will refuse
# ======================================================================


class NoRowLockTests(RemoteBase):
    """The evidence write must not take a `FOR UPDATE` lock.

    `update_or_create` looks like the obvious tool and is not: Django
    5.2 implements it as `select_for_update().get_or_create(...)`. This
    project forbids `FOR UPDATE` on the attendance path because a
    previous release took production down with it.

    SQLite cannot prove this — it has no `FOR UPDATE` to render, so an
    absence in the emitted SQL would mean nothing. These tests block
    the ORM API itself instead, which is backend-independent.
    """

    def test_check_in_never_calls_select_for_update(self):
        self.remote_request()

        def forbidden(self_qs, *args, **kwargs):
            raise AssertionError("select_for_update() reached on the punch path")

        with patch.object(QuerySet, "select_for_update", forbidden):
            response = self.client.post(
                CLOCK_IN, {"wifi_ssid": "Home"}, **through_proxy(HOME_IPV4)
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(AttendanceEvidence.objects.exists())

    def test_office_check_in_and_remote_check_out_never_lock(self):
        def forbidden(self_qs, *args, **kwargs):
            raise AssertionError("select_for_update() reached on the punch path")

        self.remote_session()
        with patch.object(QuerySet, "select_for_update", forbidden):
            response = self.client.post(CLOCK_OUT, **through_proxy(HOME_IPV4))
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(
                self.client.post(CLOCK_IN, **through_proxy(OFFICE_IPV4)).status_code,
                200,
            )

    def test_the_upsert_updates_an_existing_row_in_place(self):
        attendance = self.office_session()
        first = _upsert_evidence(
            attendance,
            AttendanceEvidence.ACTION_CHECK_IN,
            {
                "attendance_mode": AttendanceEvidence.MODE_OFFICE,
                "captured_at": timezone.localtime(),
            },
        )
        second = _upsert_evidence(
            attendance,
            AttendanceEvidence.ACTION_CHECK_IN,
            {
                "attendance_mode": AttendanceEvidence.MODE_REMOTE,
                "captured_at": timezone.localtime(),
            },
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AttendanceEvidence.objects.count(), 1)
        second.refresh_from_db()
        self.assertEqual(second.attendance_mode, AttendanceEvidence.MODE_REMOTE)

    def test_a_lost_insert_race_recovers_by_updating_the_winner(self):
        # The row already exists, but the first lookup is made to miss —
        # exactly what a concurrent request would see. The INSERT then
        # raises a REAL IntegrityError from the unique constraint (not a
        # mocked one), and the recovery path must find the winning row
        # and write onto it rather than letting the punch fail.
        attendance = self.office_session()
        AttendanceEvidence.objects.create(
            attendance=attendance,
            action=AttendanceEvidence.ACTION_CHECK_IN,
            attendance_mode=AttendanceEvidence.MODE_OFFICE,
            captured_at=timezone.localtime(),
        )
        real_filter = AttendanceEvidence.objects.filter
        seen = {"n": 0}

        def missing_once(*args, **kwargs):
            seen["n"] += 1
            if seen["n"] == 1:
                return AttendanceEvidence.objects.none()
            return real_filter(*args, **kwargs)

        with patch.object(AttendanceEvidence.objects, "filter", missing_once):
            evidence = _upsert_evidence(
                attendance,
                AttendanceEvidence.ACTION_CHECK_IN,
                {
                    "attendance_mode": AttendanceEvidence.MODE_REMOTE,
                    "captured_at": timezone.localtime(),
                },
            )
        self.assertGreaterEqual(seen["n"], 2)
        self.assertEqual(AttendanceEvidence.objects.count(), 1)
        evidence.refresh_from_db()
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_REMOTE)

    def test_an_unrelated_integrity_error_is_not_swallowed(self):
        # A constraint failure that is NOT the expected unique race must
        # surface, not be absorbed as "somebody beat me to it".
        attendance = self.office_session()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _upsert_evidence(
                    attendance,
                    AttendanceEvidence.ACTION_CHECK_IN,
                    {
                        "attendance_mode": AttendanceEvidence.MODE_OFFICE,
                        "captured_at": None,  # NOT NULL column
                    },
                )


class ClientValueHardeningTests(RemoteBase):
    """Nothing a client sends may reach a column that would refuse it.

    The write happens inside the punch transaction, so a value
    PostgreSQL rejects does not just lose the evidence — it aborts the
    check-in. SQLite accepts over-long strings silently, so every
    assertion here is about the application layer, never about what the
    test database happens to tolerate.
    """

    # -- unit level: the normalisers themselves ---------------------------

    def test_coordinate_boundaries(self):
        self.assertEqual(_coordinate("90", 90), Decimal("90"))
        self.assertEqual(_coordinate("-90", 90), Decimal("-90"))
        self.assertIsNone(_coordinate("90.000001", 90))
        self.assertIsNone(_coordinate("-90.000001", 90))
        self.assertEqual(_coordinate("180", 180), Decimal("180"))
        self.assertEqual(_coordinate("-180", 180), Decimal("-180"))
        self.assertIsNone(_coordinate("180.000001", 180))
        self.assertIsNone(_coordinate("-180.000001", 180))

    def test_coordinate_rejects_what_the_column_cannot_hold(self):
        for bad in ("1e9", "99999", "inf", "-inf", "nan", "not-a-number", None, ""):
            self.assertIsNone(_coordinate(bad, 90), bad)

    def test_accuracy_rules(self):
        self.assertEqual(_accuracy("0"), 0.0)
        self.assertEqual(_accuracy("12.5"), 12.5)
        self.assertIsNone(_accuracy("-1"))
        self.assertIsNone(_accuracy("inf"))
        self.assertIsNone(_accuracy("wat"))
        self.assertEqual(_accuracy("1000000"), 1000000.0)

    def test_text_is_dropped_not_truncated(self):
        limit = AttendanceEvidence._meta.get_field("wifi_bssid").max_length
        self.assertEqual(limit, 17)
        exact = "A" * limit
        self.assertEqual(_text(exact, "wifi_bssid"), exact)
        self.assertIsNone(_text("A" * (limit + 1), "wifi_bssid"))
        self.assertIsNone(_text("A" * 5000, "wifi_bssid"))

        ssid_limit = AttendanceEvidence._meta.get_field("wifi_ssid").max_length
        self.assertEqual(_text("S" * ssid_limit, "wifi_ssid"), "S" * ssid_limit)
        self.assertIsNone(_text("S" * (ssid_limit + 1), "wifi_ssid"))

    def test_method_must_be_a_declared_choice(self):
        self.assertEqual(
            _method_code(AttendanceEvidence.METHOD_WIFI),
            AttendanceEvidence.METHOD_WIFI,
        )
        self.assertIsNone(_method_code("SOMETHING_ELSE"))
        self.assertIsNone(_method_code("X" * 500))
        self.assertIsNone(_method_code(None))

    # -- the reachable production paths -----------------------------------

    def test_an_office_punch_survives_an_oversized_bssid(self):
        # HIGH-2, in the shape that actually reaches production: an
        # OfficeWifi row with a blank BSSID matches ANY reported BSSID
        # (the documented default), so the punch is accepted as OFFICE
        # and the raw value used to reach a varchar(17).
        OfficeWifi.objects.create(
            company_id=self.company, name="Office", ssid="JOYDIGI-OFFICE", bssid=""
        )
        response = self.client.post(
            CLOCK_IN,
            {"wifi_ssid": "JOYDIGI-OFFICE", "wifi_bssid": "B" * 400},
            **through_proxy(OFFICE_IPV4),
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.attendance_mode, AttendanceEvidence.MODE_OFFICE)
        self.assertIsNone(evidence.wifi_bssid)
        self.assertEqual(evidence.wifi_ssid, "JOYDIGI-OFFICE")

    def test_a_remote_punch_survives_an_oversized_ssid(self):
        self.remote_request()
        response = self.client.post(
            CLOCK_IN,
            {"wifi_ssid": "S" * 4000, "wifi_bssid": "AA:BB:CC:DD:EE:FF"},
            **through_proxy(HOME_IPV4),
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertIsNone(evidence.wifi_ssid)
        self.assertEqual(evidence.wifi_bssid, "AA:BB:CC:DD:EE:FF")

    def test_a_latitude_with_no_longitude_cannot_reach_the_column(self):
        # The GPS branch of validate_checkin_source only runs when BOTH
        # coordinates are present, so a lone out-of-range latitude was
        # never range-checked anywhere before this phase.
        self.remote_request()
        response = self.client.post(
            CLOCK_IN, {"latitude": "99999"}, **through_proxy(HOME_IPV4)
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertIsNone(evidence.latitude)
        self.assertIsNone(evidence.longitude)
        self.assertIsNone(evidence.distance_meters)

    def test_a_valid_coordinate_pair_is_still_stored(self):
        self.remote_request()
        response = self.client.post(
            CLOCK_IN,
            {"latitude": "21.028511", "longitude": "105.804817"},
            **through_proxy(HOME_IPV4),
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertEqual(evidence.latitude, Decimal("21.028511"))
        self.assertEqual(evidence.longitude, Decimal("105.804817"))

    def test_a_negative_accuracy_is_dropped_without_failing_the_punch(self):
        self.remote_request()
        response = self.client.post(
            CLOCK_IN,
            {"wifi_ssid": "Home", "accuracy": "-5"},
            **through_proxy(HOME_IPV4),
        )
        self.assertEqual(response.status_code, 200, response.data)
        evidence = AttendanceEvidence.objects.get(
            action=AttendanceEvidence.ACTION_CHECK_IN
        )
        self.assertIsNone(evidence.accuracy)

    def test_an_invalid_proof_is_still_invalid_after_hardening(self):
        # Normalisation must never turn a refusal into an acceptance.
        self.remote_request()
        before = self.world()
        response = self.client.post(
            CLOCK_IN,
            {"verification_proof": "P" * 5000},
            **through_proxy(HOME_IPV4),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "VERIFICATION_REQUIRED")
        self.assertEqual(self.world(), before)

    def test_an_office_wifi_mismatch_is_still_refused_after_hardening(self):
        OfficeWifi.objects.create(
            company_id=self.company, name="Office", ssid="JOYDIGI-OFFICE"
        )
        before = self.world()
        response = self.client.post(
            CLOCK_IN, {"wifi_ssid": "S" * 4000}, **through_proxy(OFFICE_IPV4)
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "WIFI_NOT_ALLOWED")
        self.assertEqual(self.world(), before)
