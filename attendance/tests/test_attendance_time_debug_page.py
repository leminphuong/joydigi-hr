"""Phase ATT-TIME-2H — the temporary web diagnostic page.

The operator has no SSH access, so ATT-TIME-2G's `ATT_TIME_DEBUG` log
lines are unreadable in production. This page surfaces the same evidence
in the browser. Because it renders runtime internals, the tests below
care as much about what it must *never* show as about what it shows.
"""

from datetime import date, time, timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from attendance.models import Attendance, AttendanceActivity
from base.models import EmployeeShift, EmployeeShiftDay
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

URL = "/attendance/attendance-time-debug/"

SECRET_LOOKING_PROOF = "proof-token-should-never-render"


class TimeDebugPageBase(TestCase):
    def setUp(self):
        self.company = make_company("Debug Page Co")
        self.day = date.today() - timedelta(days=1)

        self.admin_user = make_user("debugadmin", password="secret123")
        self.admin = make_employee(
            company=self.company, email="admin@test.joydigi", user=self.admin_user
        )
        self.admin_user.user_permissions.add(
            Permission.objects.get(
                codename="view_attendance", content_type__app_label="attendance"
            )
        )

        self.staff_user = make_user("debugstaff", password="secret123")
        self.staff = make_employee(
            company=self.company, email="staff@test.joydigi", user=self.staff_user
        )

        self.shift = EmployeeShift.objects.create(employee_shift="Debug Page Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.staff).update(
            shift_id=self.shift
        )
        self.shift_day = EmployeeShiftDay.objects.get(
            day=self.day.strftime("%A").lower()
        )

        self.attendance = Attendance.objects.create(
            employee_id=self.staff,
            attendance_date=self.day,
            shift_id=self.shift,
            attendance_day=self.shift_day,
            attendance_clock_in_date=self.day,
            attendance_clock_in=time(11, 43),
            minimum_hour="08:00",
        )

    def login_admin(self):
        self.client.login(username="debugadmin", password="secret123")


class AccessControlTests(TimeDebugPageBase):
    def test_anonymous_is_denied(self):
        """§10.1"""
        response = self.client.get(URL)
        self.assertNotEqual(response.status_code, 200)
        self.assertIn(response.status_code, (301, 302))

    def test_a_plain_employee_is_denied(self):
        """§10.2 — this project's `permission_required` answers a
        non-HTMX request with a 200 carrying `decorator_404.html` rather
        than a 403, so the assertion is content-based: what matters is
        that none of the diagnostic content is reachable."""
        self.client.login(username="debugstaff", password="secret123")
        response = self.client.get(URL)
        body = response.content.decode()

        self.assertNotIn("DJANGO LOCAL NOW", body)
        self.assertNotIn("TZ PROCESS ENV", body)
        self.assertNotIn("Chẩn đoán giờ chấm công", body)

    def test_an_authorized_admin_gets_the_page(self):
        """§10.3"""
        self.login_admin()
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Chẩn đoán giờ chấm công", response.content.decode())


class RuntimePanelTests(TimeDebugPageBase):
    def test_runtime_values_are_rendered(self):
        """§10.4-7"""
        self.login_admin()
        body = self.client.get(URL).content.decode()

        self.assertIn("TIME_ZONE", body)
        self.assertIn("Asia/Ho_Chi_Minh", body)
        self.assertIn("USE_TZ", body)
        self.assertIn("DJANGO LOCAL NOW", body)
        self.assertIn("UTC OFFSET", body)
        self.assertIn("UTC NOW", body)
        self.assertIn("SERVER DATE", body)
        self.assertIn("TZ PROCESS ENV", body)

    def test_the_rendered_offset_is_seven_hours(self):
        self.login_admin()
        body = self.client.get(URL).content.decode()
        self.assertIn("7:00:00", body)

    @override_settings(TIME_ZONE="Asia/Ho_Chi_Minh")
    def test_vietnam_runtime_reports_ok(self):
        """§10.16"""
        self.login_admin()
        body = self.client.get(URL).content.decode()
        self.assertIn("OK — Django runtime đang dùng giờ Việt Nam", body)

    @override_settings(TIME_ZONE="Asia/Kolkata")
    def test_kolkata_runtime_reports_error(self):
        """§10.15 — the exact production symptom, classified loudly."""
        self.login_admin()
        body = self.client.get(URL).content.decode()
        self.assertIn("ERROR", body)
        self.assertIn("Asia/Kolkata", body)
        self.assertNotIn("OK — Django runtime đang dùng giờ Việt Nam", body)

    @override_settings(TIME_ZONE="Europe/Paris")
    def test_any_other_timezone_reports_a_warning(self):
        self.login_admin()
        body = self.client.get(URL).content.decode()
        self.assertIn("WARNING", body)


class SecretSafetyTests(TimeDebugPageBase):
    def test_no_secrets_are_rendered(self):
        """§10.8-11 — the page reads `os.environ.get("TZ")` only; it
        never dumps the environment or settings."""
        from django.conf import settings

        self.login_admin()
        body = self.client.get(URL).content.decode()

        # `csrftoken`/`sessionid` are deliberately not asserted on: they
        # come from the shared `settings.html` base layout every admin
        # page renders, not from this view, and a CSRF token is not a
        # credential of the kind this page must protect.
        self.assertNotIn(settings.SECRET_KEY, body)
        for forbidden in (
            "SECRET_KEY",
            "DATABASE_URL",
            "DB_PASSWORD",
            "PASSWORD",
            "Authorization",
            "Bearer",
            "verification_proof",
            "refresh_token",
            "access_token",
            "VPS_",
        ):
            self.assertNotIn(forbidden, body, msg=f"{forbidden} leaked onto the page")

    def test_the_view_reads_only_the_tz_environment_variable(self):
        """§10.8 — enforced by reading the source: a single, named
        lookup, never `os.environ` as a whole."""
        with open("attendance/views/time_debug.py", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('os.environ.get("TZ")', source)
        # Every `os.environ` mention must be that one named lookup —
        # counting bare occurrences would also count the docstring that
        # explains why it is the only one.
        self.assertEqual(
            source.count("os.environ"), source.count('os.environ.get("TZ")')
        )
        for forbidden in ("os.environ.items", "dict(os.environ", "environ.copy"):
            self.assertNotIn(forbidden, source)

    def test_the_page_offers_no_mutating_action(self):
        """§11 — read-only: no form, and no shell."""
        self.login_admin()
        body = self.client.get(URL).content.decode()
        self.assertNotIn("<form", body.lower())

        with open("attendance/views/time_debug.py", encoding="utf-8") as handle:
            source = handle.read()
        # Checks for the primitives that could *run* something. The
        # module docstring legitimately mentions journalctl by name when
        # explaining why this page exists, so the words themselves are
        # not what makes a page dangerous — the ability to execute is.
        for forbidden in (
            "subprocess",
            "os.system",
            "os.popen",
            "shell=True",
            "check_output",
        ):
            self.assertNotIn(forbidden, source)
        # No writes of any kind.
        for forbidden in (".save(", ".delete(", ".update(", ".create("):
            self.assertNotIn(forbidden, source)


class AttendanceRowTests(TimeDebugPageBase):
    def test_recent_attendance_is_rendered(self):
        """§10.12"""
        self.login_admin()
        body = self.client.get(URL).content.decode()

        self.assertIn(str(self.attendance.id), body)
        self.assertIn("11:43", body)
        # The date is rendered as an explicit ISO string by the view, not
        # left to the template's locale-dependent default formatting.
        self.assertIn(self.day.isoformat(), body)

    def test_an_aware_datetime_is_converted_with_django_localtime(self):
        """§10.14 — the activity's aware `in_datetime` must be shown in
        the active timezone, computed by Django rather than by adding a
        fixed offset."""
        from django.utils import timezone as django_timezone

        instant = django_timezone.now()
        AttendanceActivity.objects.create(
            employee_id=self.staff,
            attendance_date=self.day,
            clock_in_date=self.day,
            shift_day=self.shift_day,
            clock_in=instant.time(),
            in_datetime=instant,
        )

        self.login_admin()
        body = self.client.get(URL).content.decode()

        expected = django_timezone.localtime(instant).isoformat()
        self.assertIn(expected, body)

    def test_no_manual_offset_is_applied_anywhere(self):
        with open("attendance/views/time_debug.py", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in (
            "timedelta(hours=7)",
            "timedelta(hours=1, minutes=30)",
            "timedelta(minutes=90)",
            "hours=5, minutes=30",
        ):
            self.assertNotIn(forbidden, source)

    def test_company_scope_is_respected(self):
        """§10.13 — rows come from `Attendance.objects`, the
        company-scoped manager, so a session pinned to one company never
        sees another company's attendance."""
        other_company = make_company("Other Debug Co")
        other_user = make_user("otherstaff", password="secret123")
        other_employee = make_employee(
            company=other_company,
            email="otherstaff@test.joydigi",
            user=other_user,
        )
        other_attendance = Attendance.objects.create(
            employee_id=other_employee,
            attendance_date=self.day,
            shift_id=self.shift,
            attendance_day=self.shift_day,
            attendance_clock_in_date=self.day,
            attendance_clock_in=time(6, 6),
            minimum_hour="08:00",
        )

        self.login_admin()
        session = self.client.session
        session["selected_company"] = self.company.id
        session.save()

        body = self.client.get(URL).content.decode()

        # Asserted on values unique to each row rather than on a bare id,
        # which would also match unrelated markup elsewhere on the page.
        self.assertIn("11:43", body)
        self.assertNotIn("06:06", body)
        self.assertNotIn("otherstaff", body)
        self.assertIsNotNone(other_attendance.id)
