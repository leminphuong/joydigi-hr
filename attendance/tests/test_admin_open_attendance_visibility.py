"""
Phase 2 — Admin attendance visibility.

The report: an employee checks in on the phone, the app says it worked, and
the Admin cannot find the attendance. The row was there all along, but a
session that is still open is not validated yet, and the Admin's
"Validated Attendances" tab only listed validated rows — so the employee
appeared only after checking out and being validated.

The Validated tab now also lists sessions that are running right now,
marked "Đang làm việc" and left unvalidated. Everything here goes through
the real tab URL with a logged-in session, so the company middleware,
`manager_can_enter`, `filtersubordinates` and the template all take part —
the security of the change is tested where it actually lives.

Rows are created in the test database only.
"""

from datetime import datetime, time, timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from attendance.models import Attendance, AttendanceActivity
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

VALIDATED_TAB = "validated-attendance-tab"
VALIDATE_TAB = "validate-attendance-tab"


class AdminVisibilityBase(TestCase):
    def setUp(self):
        self.company = make_company("Visible Co")
        self.day_shift = EmployeeShift.objects.create(employee_shift="Day Shift")
        self.night_shift = EmployeeShift.objects.create(employee_shift="Night Shift")
        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)
        for d in (self.today, self.yesterday):
            self.schedule(self.day_shift, d, night=False)
            self.schedule(self.night_shift, d, night=True)

        # An HR admin: allowed to view all attendance, but not a superuser,
        # so company scoping is genuinely in force.
        self.admin_user = make_user("hradmin", password="secret123")
        self.admin_user.user_permissions.add(
            Permission.objects.get(
                codename="view_attendance", content_type__app_label="attendance"
            )
        )
        self.admin_employee = make_employee(
            company=self.company, email="hradmin@test.joydigi", user=self.admin_user
        )

        self.worker = self.employee("worker", self.company, self.day_shift)

    # ---------- fixtures ----------

    def schedule(self, shift, d, night):
        day = EmployeeShiftDay.objects.get(day=d.strftime("%A").lower())
        EmployeeShiftSchedule.objects.get_or_create(
            shift_id=shift,
            day=day,
            defaults={
                "is_night_shift": night,
                "minimum_working_hour": "08:00",
                "start_time": "22:00:00" if night else "08:00:00",
                "end_time": "06:00:00" if night else "17:00:00",
            },
        )

    def employee(self, name, company, shift, manager=None):
        user = make_user(name, password="secret123")
        employee = make_employee(
            company=company, email=f"{name}@test.joydigi", user=user
        )
        updates = {"shift_id": shift}
        if manager is not None:
            updates["reporting_manager_id"] = manager
        EmployeeWorkInformation.objects.filter(employee_id=employee).update(**updates)
        return employee

    def open_session(self, employee, d, shift, at=time(8, 0)):
        day = EmployeeShiftDay.objects.get(day=d.strftime("%A").lower())
        clock_in_attendance_and_activity(
            employee=employee,
            date_today=d,
            attendance_date=d,
            day=day,
            now=at.strftime("%H:%M"),
            shift=shift,
            minimum_hour="08:00",
            start_time=0,
            end_time=1,
            in_datetime=timezone.make_aware(datetime.combine(d, at)),
        )
        return Attendance.objects.get(employee_id=employee, attendance_date=d)

    def close(self, row, validated):
        Attendance.objects.filter(pk=row.pk).update(
            attendance_clock_out=time(17, 0),
            attendance_clock_out_date=row.attendance_date,
            attendance_validated=validated,
        )
        AttendanceActivity.objects.filter(
            employee_id=row.employee_id, attendance_date=row.attendance_date
        ).update(clock_out=time(17, 0), clock_out_date=row.attendance_date)

    def tab(self, name, user=None):
        self.client.force_login(user or self.admin_user)
        response = self.client.get(reverse(name), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        return response

    def ids(self, response):
        return [row.pk for row in response.context["object_list"]]

    def snapshot(self, row):
        row.refresh_from_db()
        return (
            row.attendance_date,
            row.attendance_clock_in,
            row.attendance_clock_out,
            row.attendance_clock_out_date,
            row.attendance_worked_hour,
            row.attendance_validated,
            row.is_validate_request,
            row.is_validate_request_approved,
        )


class OpenSessionVisibilityTests(AdminVisibilityBase):
    def test_1_todays_open_session_is_visible_and_not_validated(self):
        row = self.open_session(self.worker, self.today, self.day_shift)
        self.assertFalse(row.attendance_validated)

        response = self.tab(VALIDATED_TAB)

        self.assertIn(row.pk, self.ids(response))
        html = response.content.decode()
        # Marked as a running session, with the existing row-marker style,
        # and never with the validated marker.
        self.assertIn("Đang làm việc", html)
        self.assertIn("validated-False", html)
        row.refresh_from_db()
        self.assertFalse(row.attendance_validated)

    def test_2_a_validated_day_is_listed_exactly_as_before(self):
        # Membership only: this holds on the code before this phase too.
        row = self.open_session(self.worker, self.today, self.day_shift)
        self.close(row, validated=True)

        response = self.tab(VALIDATED_TAB)

        self.assertIn(row.pk, self.ids(response))
        row.refresh_from_db()
        self.assertTrue(row.attendance_validated)

    def test_a_validated_day_carries_the_validated_marker(self):
        # New in this phase: rows are marked, so a validated day is told
        # apart from a session that is still running.
        row = self.open_session(self.worker, self.today, self.day_shift)
        self.close(row, validated=True)

        html = self.tab(VALIDATED_TAB).content.decode()

        self.assertIn("validated-True", html)
        self.assertNotIn("validated-False", html)

    def test_3_a_finished_but_unvalidated_day_is_not_promoted(self):
        row = self.open_session(self.worker, self.today, self.day_shift)
        self.close(row, validated=False)

        self.assertNotIn(row.pk, self.ids(self.tab(VALIDATED_TAB)))
        # It stays where it always was: awaiting validation.
        self.assertIn(row.pk, self.ids(self.tab(VALIDATE_TAB)))
        row.refresh_from_db()
        self.assertFalse(row.attendance_validated)

    def test_4_an_outside_radius_request_keeps_its_approval_workflow(self):
        row = self.open_session(self.worker, self.today, self.day_shift)
        Attendance.objects.filter(pk=row.pk).update(
            is_validate_request=True, is_validate_request_approved=False
        )

        self.assertNotIn(row.pk, self.ids(self.tab(VALIDATED_TAB)))
        self.assertIn(row.pk, self.ids(self.tab(VALIDATE_TAB)))
        row.refresh_from_db()
        self.assertFalse(row.attendance_validated)
        self.assertTrue(row.is_validate_request)
        self.assertFalse(row.is_validate_request_approved)

    def test_5_yesterdays_forgotten_day_shift_is_not_shown_as_working(self):
        row = self.open_session(self.worker, self.yesterday, self.day_shift)
        before = self.snapshot(row)

        self.assertNotIn(row.pk, self.ids(self.tab(VALIDATED_TAB)))
        # Still findable where unvalidated rows always were.
        self.assertIn(row.pk, self.ids(self.tab(VALIDATE_TAB)))
        self.assertEqual(self.snapshot(row), before)

    def test_6_a_night_shift_running_past_midnight_is_visible(self):
        night_worker = self.employee("nightworker", self.company, self.night_shift)
        row = self.open_session(
            night_worker, self.yesterday, self.night_shift, at=time(22, 0)
        )
        self.assertTrue(row.is_night_shift(), "fixture must really be a night shift")

        self.assertIn(row.pk, self.ids(self.tab(VALIDATED_TAB)))

    def test_9_a_row_matching_both_conditions_appears_once(self):
        # Open *and* validated — e.g. validated by hand mid-shift.
        row = self.open_session(self.worker, self.today, self.day_shift)
        Attendance.objects.filter(pk=row.pk).update(attendance_validated=True)

        ids = self.ids(self.tab(VALIDATED_TAB))
        self.assertEqual(ids.count(row.pk), 1)

    def test_10_viewing_the_admin_tabs_writes_nothing(self):
        open_row = self.open_session(self.worker, self.today, self.day_shift)
        stale = self.open_session(
            self.employee("staleworker", self.company, self.day_shift),
            self.yesterday,
            self.day_shift,
        )
        before = (
            Attendance.objects.count(),
            AttendanceActivity.objects.count(),
            self.snapshot(open_row),
            self.snapshot(stale),
        )
        for _ in range(2):
            self.tab(VALIDATED_TAB)
            self.tab(VALIDATE_TAB)
        self.assertEqual(
            (
                Attendance.objects.count(),
                AttendanceActivity.objects.count(),
                self.snapshot(open_row),
                self.snapshot(stale),
            ),
            before,
        )

    def test_the_to_validate_tab_is_unchanged(self):
        # Adding open sessions to the Validated tab must not take them out
        # of the queue they have always been in.
        row = self.open_session(self.worker, self.today, self.day_shift)
        self.assertIn(row.pk, self.ids(self.tab(VALIDATE_TAB)))


class ScopeIsolationTests(AdminVisibilityBase):
    def test_7_another_companys_open_session_stays_hidden(self):
        other_company = make_company("Other Co")
        outsider = self.employee("outsider", other_company, self.day_shift)
        foreign = self.open_session(outsider, self.today, self.day_shift)
        mine = self.open_session(self.worker, self.today, self.day_shift)

        ids = self.ids(self.tab(VALIDATED_TAB))

        self.assertIn(mine.pk, ids)
        self.assertNotIn(foreign.pk, ids)

    def test_8_being_open_does_not_widen_a_managers_reach(self):
        manager_user = make_user("teamlead", password="secret123")
        manager = make_employee(
            company=self.company, email="teamlead@test.joydigi", user=manager_user
        )
        self.assertFalse(manager_user.has_perm("attendance.view_attendance"))

        report = self.employee("report", self.company, self.day_shift, manager=manager)
        stranger = self.employee("stranger", self.company, self.day_shift)
        report_row = self.open_session(report, self.today, self.day_shift)
        stranger_row = self.open_session(stranger, self.today, self.day_shift)

        ids = self.ids(self.tab(VALIDATED_TAB, user=manager_user))

        self.assertIn(report_row.pk, ids)
        self.assertNotIn(stranger_row.pk, ids)
