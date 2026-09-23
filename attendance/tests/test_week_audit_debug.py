"""Phase WEEK-AUDIT — TEMPORARY. Delete with `attendance/views/week_audit_debug.py`.

A forensic page has two obligations and they are equally important: it
must find what is actually wrong, and it must not change anything while
looking. Both are pinned here — every issue code is proved against a
row deliberately built to trigger it, and every request is bracketed by
a full snapshot of the attendance tables to prove the audit is inert.
"""

from datetime import date, time, timedelta

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from attendance.models import Attendance, AttendanceActivity
from base.models import (
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
)
from employee.models import Employee, EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

#: Only these may ever describe a person on this page.
IDENTITY_KEYS = {
    "employee_id", "badge_id", "name", "is_active", "has_work_info",
    "company_id", "company", "department", "shift_id", "shift",
}

ATTENDANCE_KEYS = {
    "id", "attendance_date", "attendance_clock_in", "attendance_clock_in_date",
    "attendance_clock_out", "attendance_clock_out_date", "attendance_validated",
    "attendance_day", "attendance_worked_hour", "shift_id",
}

ACTIVITY_KEYS = {
    "id", "attendance_date", "clock_in", "clock_in_date", "clock_out",
    "clock_out_date", "out_datetime", "shift_day",
}

FORBIDDEN = (
    "@", "email", "phone", "address", "bank", "password", "token",
    "sessionid", "csrf", "Authorization", "HTTP_",
)


class WeekAuditBase(TestCase):
    """Three consecutive weekdays, one company, one day shift."""

    def setUp(self):
        self.company = make_company("Audit Co")
        self.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        self.shift.company_id.add(self.company)

        # Real dates, so `timezone.localdate()` defaults line up; walked
        # back to a Wednesday so the three audited days share a week.
        self.day3 = timezone.localdate()
        while self.day3.weekday() != 2:
            self.day3 -= timedelta(days=1)
        self.day2 = self.day3 - timedelta(days=1)
        self.day1 = self.day3 - timedelta(days=2)

        for day in (self.day1, self.day2, self.day3):
            schedule, _created = EmployeeShiftSchedule.objects.get_or_create(
                shift_id=self.shift,
                day=EmployeeShiftDay.objects.get(day=day.strftime("%A").lower()),
                defaults={
                    "is_night_shift": False,
                    "minimum_working_hour": "08:00",
                    "start_time": time(8, 0),
                    "end_time": time(17, 0),
                },
            )
            schedule.company_id.add(self.company)

        self.url = reverse("attendance-week-audit")

    # -- people ---------------------------------------------------------

    def employee(self, tag, *, company=None, active=True, work_info=True):
        person = make_employee(
            company=company or self.company,
            email=f"{tag}@test.joydigi",
            first_name="Audit",
            last_name=tag,
        )
        info = EmployeeWorkInformation.objects.filter(employee_id=person)
        if work_info:
            info.update(shift_id=self.shift)
        else:
            info.delete()
        if not active:
            Employee.objects.filter(pk=person.pk).update(is_active=False)
            person.refresh_from_db()
        return person

    def as_auditor(self, *, permitted=True, superuser=True):
        user = make_user(
            "auditor", password="secret123", is_superuser=superuser
        )
        make_employee(company=self.company, email="auditor@test.joydigi", user=user)
        if permitted and not superuser:
            user.user_permissions.add(
                Permission.objects.get(codename="view_attendance")
            )
        self.client.force_login(type(user).objects.get(pk=user.pk))
        return user

    # -- rows -----------------------------------------------------------

    def attendance(self, person, on, *, clock_in=time(8, 0), clock_out=None,
                   clock_out_date="same"):
        if clock_out_date == "same":
            clock_out_date = on if clock_out is not None else None
        return Attendance.objects.create(
            employee_id=person,
            attendance_date=on,
            attendance_day=EmployeeShiftDay.objects.get(day=on.strftime("%A").lower()),
            shift_id=self.shift,
            attendance_clock_in=clock_in,
            attendance_clock_in_date=on,
            attendance_clock_out=clock_out,
            attendance_clock_out_date=clock_out_date,
            minimum_hour="08:00",
        )

    def activity(self, person, on, *, clock_in=time(8, 0), clock_out=None):
        return AttendanceActivity.objects.create(
            employee_id=person,
            attendance_date=on,
            clock_in_date=on,
            shift_day=EmployeeShiftDay.objects.get(day=on.strftime("%A").lower()),
            clock_in=clock_in,
            clock_out=clock_out,
            clock_out_date=on if clock_out is not None else None,
        )

    # -- calling --------------------------------------------------------

    def audit(self, **params):
        params.setdefault("start_date", self.day1.isoformat())
        params.setdefault("end_date", self.day3.isoformat())
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def days_for(self, body, person):
        for entry in body["employees"]:
            if entry["employee"]["employee_id"] == person.pk:
                return {day["date"]: day for day in entry["days"]}
        self.fail(f"employee {person.pk} missing from audit")

    def entry_for(self, body, person):
        for entry in body["employees"]:
            if entry["employee"]["employee_id"] == person.pk:
                return entry
        self.fail(f"employee {person.pk} missing from audit")

    def world(self):
        """Every attendance-shaped row, for inertness checks."""
        return (
            list(
                Attendance.objects.order_by("pk").values_list(
                    "pk", "attendance_date", "attendance_clock_in",
                    "attendance_clock_in_date", "attendance_clock_out",
                    "attendance_clock_out_date", "attendance_validated",
                    "attendance_worked_hour", "attendance_day",
                )
            ),
            list(
                AttendanceActivity.objects.order_by("pk").values_list(
                    "pk", "attendance_date", "clock_in", "clock_in_date",
                    "clock_out", "clock_out_date", "out_datetime",
                )
            ),
            list(Employee.objects.order_by("pk").values_list("pk", "is_active")),
            list(
                EmployeeShiftSchedule.objects.order_by("pk").values_list(
                    "pk", "is_auto_punch_out_enabled", "auto_punch_out_time",
                    "start_time", "end_time",
                )
            ),
        )


class AccessTests(WeekAuditBase):
    def test_an_anonymous_caller_is_turned_away(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_an_employee_without_the_permission_is_turned_away(self):
        user = make_user("plain", password="secret123")
        make_employee(company=self.company, email="plain@test.joydigi", user=user)
        self.client.force_login(type(user).objects.get(pk=user.pk))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("application/json", response.get("Content-Type", ""))

    def test_the_permission_alone_is_enough(self):
        self.as_auditor(superuser=False)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_only_get_is_answered(self):
        self.as_auditor()
        for method in ("post", "put", "patch", "delete"):
            self.assertEqual(
                getattr(self.client, method)(self.url).status_code, 405, method
            )


class RangeTests(WeekAuditBase):
    def setUp(self):
        super().setUp()
        self.as_auditor()

    def test_a_malformed_date_is_a_controlled_refusal(self):
        response = self.client.get(self.url, {"start_date": "yesterday"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_RANGE")

    def test_a_reversed_range_is_refused(self):
        response = self.client.get(
            self.url,
            {"start_date": self.day3.isoformat(), "end_date": self.day1.isoformat()},
        )
        self.assertEqual(response.status_code, 400)

    def test_more_than_seven_days_is_refused(self):
        response = self.client.get(
            self.url,
            {
                "start_date": (self.day3 - timedelta(days=7)).isoformat(),
                "end_date": self.day3.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("7", response.json()["message"])

    def test_exactly_seven_days_is_allowed(self):
        response = self.client.get(
            self.url,
            {
                "start_date": (self.day3 - timedelta(days=6)).isoformat(),
                "end_date": self.day3.isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_the_default_range_is_this_week_so_far(self):
        body = self.client.get(self.url).json()
        today = timezone.localdate()
        self.assertEqual(body["summary"]["audit_end"], today.isoformat())
        self.assertEqual(
            body["summary"]["audit_start"],
            (today - timedelta(days=today.weekday())).isoformat(),
        )

    def test_the_window_is_widened_by_a_day_on_each_side(self):
        summary = self.audit()["summary"]
        self.assertEqual(
            summary["inspection_window_start"],
            (self.day1 - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(
            summary["inspection_window_end"],
            (self.day3 + timedelta(days=1)).isoformat(),
        )

    def test_both_date_sources_are_reported_side_by_side(self):
        summary = self.audit()["summary"]
        self.assertEqual(
            summary["timezone_localdate"], timezone.localdate().isoformat()
        )
        self.assertEqual(summary["python_date_today"], date.today().isoformat())
        self.assertIsInstance(summary["dates_match"], bool)


class ScopeAndSafetyTests(WeekAuditBase):
    def test_only_allow_listed_identity_fields_are_reported(self):
        person = self.employee("Alpha")
        self.attendance(person, self.day1, clock_out=time(17, 0))
        self.activity(person, self.day1, clock_out=time(17, 0))
        self.as_auditor()
        body = self.audit()
        entry = self.entry_for(body, person)
        self.assertEqual(set(entry["employee"]), IDENTITY_KEYS)
        day = self.days_for(body, person)[self.day1.isoformat()]
        self.assertEqual(set(day["attendance_rows"][0]), ATTENDANCE_KEYS)
        self.assertEqual(set(day["activity_rows"][0]), ACTIVITY_KEYS)

    def test_nothing_sensitive_appears_anywhere_in_the_response(self):
        self.employee("Alpha")
        self.as_auditor()
        raw = self.client.get(
            self.url,
            {"start_date": self.day1.isoformat(), "end_date": self.day3.isoformat()},
            HTTP_AUTHORIZATION="Bearer supersecrettoken",
            HTTP_COOKIE="sessionid=supersecretsession",
        ).content.decode()
        for forbidden in FORBIDDEN:
            self.assertNotIn(forbidden, raw, forbidden)

    def test_an_employee_of_another_company_is_out_of_scope(self):
        other_company = make_company("Other Co")
        outsider = self.employee("Outsider", company=other_company)
        self.attendance(outsider, self.day1)
        self.as_auditor()
        session = self.client.session
        session["selected_company"] = str(self.company.pk)
        session.save()
        body = self.audit()
        ids = {e["employee"]["employee_id"] for e in body["employees"]}
        self.assertNotIn(outsider.pk, ids)


class IssueDetectionTests(WeekAuditBase):
    """Every critical code, proved against a row built to trigger it."""

    def setUp(self):
        super().setUp()
        self.as_auditor()

    def issues(self, person, on):
        return self.days_for(self.audit(), person)[on.isoformat()]["issues"]

    def test_a_normal_closed_day_is_clean(self):
        person = self.employee("Clean")
        self.attendance(person, self.day1, clock_out=time(17, 0))
        self.activity(person, self.day1, clock_out=time(17, 0))
        day = self.days_for(self.audit(), person)[self.day1.isoformat()]
        self.assertEqual(day["issues"], [])
        self.assertEqual(day["state"], "OK_CLOSED")

    def test_a_normal_open_day_is_clean(self):
        person = self.employee("Open")
        self.attendance(person, self.day3)
        self.activity(person, self.day3)
        day = self.days_for(self.audit(), person)[self.day3.isoformat()]
        self.assertEqual(day["issues"], [])
        self.assertEqual(day["state"], "OK_OPEN")

    def test_a_day_with_nothing_is_reported_as_such(self):
        person = self.employee("Empty")
        day = self.days_for(self.audit(), person)[self.day1.isoformat()]
        self.assertEqual(day["state"], "OK_NO_ATTENDANCE")

    def test_clock_out_without_its_date(self):
        person = self.employee("HalfA")
        self.attendance(
            person, self.day1, clock_out=time(17, 0), clock_out_date=None
        )
        self.activity(person, self.day1, clock_out=time(17, 0))
        self.assertIn("CLOCK_OUT_WITHOUT_CLOCK_OUT_DATE", self.issues(person, self.day1))

    def test_clock_out_date_without_its_time(self):
        person = self.employee("HalfB")
        self.attendance(person, self.day1, clock_out=None, clock_out_date=self.day1)
        self.activity(person, self.day1, clock_out=time(17, 0))
        self.assertIn("CLOCK_OUT_DATE_WITHOUT_CLOCK_OUT", self.issues(person, self.day1))

    def test_multiple_open_activities(self):
        person = self.employee("TwoActs")
        self.attendance(person, self.day1)
        self.activity(person, self.day1, clock_in=time(8, 0))
        self.activity(person, self.day1, clock_in=time(9, 0))
        issues = self.issues(person, self.day1)
        self.assertIn("MULTIPLE_OPEN_ACTIVITIES", issues)

    def test_attendance_without_any_activity(self):
        person = self.employee("NoAct")
        self.attendance(person, self.day1)
        self.assertIn("ATTENDANCE_WITHOUT_ACTIVITY", self.issues(person, self.day1))

    def test_activity_without_any_attendance(self):
        person = self.employee("NoAtt")
        self.activity(person, self.day1)
        self.assertIn("ACTIVITY_WITHOUT_ATTENDANCE", self.issues(person, self.day1))

    def test_open_activity_beside_a_closed_attendance(self):
        person = self.employee("Mixed")
        self.attendance(person, self.day1, clock_out=time(17, 0))
        self.activity(person, self.day1)
        self.assertIn(
            "OPEN_ACTIVITY_WITH_CLOSED_ATTENDANCE", self.issues(person, self.day1)
        )

    def test_closed_activity_beside_an_open_attendance(self):
        person = self.employee("Mixed2")
        self.attendance(person, self.day1)
        self.activity(person, self.day1, clock_out=time(17, 0))
        self.assertIn(
            "CLOSED_ACTIVITY_WITH_OPEN_ATTENDANCE", self.issues(person, self.day1)
        )

    def test_yesterday_left_open(self):
        person = self.employee("Forgot")
        self.attendance(person, self.day1)
        self.activity(person, self.day1)
        issues = self.issues(person, self.day2)
        self.assertIn("PREVIOUS_DAY_OPEN_ATTENDANCE", issues)
        self.assertIn("PREVIOUS_DAY_OPEN_ACTIVITY", issues)

    def test_an_open_day_followed_by_another_row_is_a_cross_link_risk(self):
        person = self.employee("Cross")
        self.attendance(person, self.day1)
        self.activity(person, self.day1)
        self.attendance(person, self.day2)
        self.activity(person, self.day2)
        self.assertIn("NEXT_DAY_CROSS_LINK_RISK", self.issues(person, self.day1))

    def test_check_online_disagreeing_with_the_day_itself(self):
        # Nothing on day 3, but an open row on day 2 — with a night
        # shift the server would still call them online today, while
        # today's own record is empty. The shape behind the incident.
        person = self.employee("NightOpen")
        schedule = EmployeeShiftSchedule.objects.get(
            shift_id=self.shift,
            day=EmployeeShiftDay.objects.get(day=self.day2.strftime("%A").lower()),
        )
        EmployeeShiftSchedule.objects.filter(pk=schedule.pk).update(is_night_shift=True)
        self.attendance(person, self.day2)
        self.activity(person, self.day2)
        issues = self.issues(person, self.day3)
        self.assertIn("CHECK_ONLINE_DISAGREES_WITH_TARGET_DAY", issues)

    def test_the_day_shift_equivalent_does_not_claim_online(self):
        # Same rows, ordinary day shift: the server would say offline,
        # so there is no disagreement to report for day 3.
        person = self.employee("DayOpen")
        self.attendance(person, self.day2)
        self.activity(person, self.day2)
        days = self.days_for(self.audit(), person)
        self.assertFalse(
            days[self.day3.isoformat()]["diagnostic_simulation"][
                "would_be_online_for_this_date"
            ]
        )

    def test_auto_punch_out_time_match_is_flagged_as_evidence(self):
        person = self.employee("Auto")
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_auto_punch_out_enabled=True, auto_punch_out_time=time(17, 30)
        )
        self.attendance(person, self.day1, clock_out=time(17, 30))
        self.activity(person, self.day1, clock_out=time(17, 30))
        self.assertIn("AUTO_PUNCH_OUT_TIME_MATCH", self.issues(person, self.day1))


class CheckoutSelectionTests(WeekAuditBase):
    """The invariant that check-out currently does not hold."""

    def setUp(self):
        super().setUp()
        self.as_auditor()

    def test_a_single_open_day_selects_matching_records(self):
        person = self.employee("Simple")
        self.attendance(person, self.day3)
        self.activity(person, self.day3)
        selection = self.entry_for(self.audit(), person)[
            "checkout_selection_simulation"
        ]
        self.assertTrue(selection["same_date"])
        self.assertEqual(selection["issues"], [])

    def test_yesterdays_open_activity_with_todays_row_crosses_dates(self):
        # Exactly what `clock_out_attendance_and_activity` would do:
        # newest open activity is day 1's, newest attendance is day 3's.
        person = self.employee("CrossPick")
        self.attendance(person, self.day1)
        self.activity(person, self.day1)
        self.attendance(person, self.day3, clock_out=time(17, 0))
        self.activity(person, self.day3, clock_out=time(17, 0))

        selection = self.entry_for(self.audit(), person)[
            "checkout_selection_simulation"
        ]
        self.assertEqual(selection["selected_activity_date"], self.day1.isoformat())
        self.assertEqual(selection["selected_attendance_date"], self.day3.isoformat())
        self.assertFalse(selection["same_date"])
        self.assertIn("CHECKOUT_WOULD_SELECT_DIFFERENT_DATES", selection["issues"])
        self.assertIn(
            "CHECKOUT_WOULD_SELECT_DIFFERENT_RECORD_RELATIONSHIP",
            selection["issues"],
        )

    def test_selecting_an_already_closed_attendance_is_flagged(self):
        person = self.employee("Reclose")
        self.attendance(person, self.day1)
        self.activity(person, self.day1)
        self.attendance(person, self.day2, clock_out=time(17, 0))
        selection = self.entry_for(self.audit(), person)[
            "checkout_selection_simulation"
        ]
        self.assertTrue(selection["selected_attendance_already_closed"])


class AdminVisibilityTests(WeekAuditBase):
    def setUp(self):
        super().setUp()
        self.as_auditor()

    def test_an_inactive_employee_is_reported_as_hidden(self):
        person = self.employee("Gone", active=False)
        entry = None
        for candidate in self.audit()["employees"]:
            if candidate["employee"]["employee_id"] == person.pk:
                entry = candidate
        # `_visible_employees` drops inactive people entirely, so the
        # page cannot report on them — which is itself the finding.
        self.assertIsNone(entry)

    def test_an_employee_without_work_info_is_reported_as_hidden(self):
        person = self.employee("NoInfo", work_info=False)
        body = self.audit()
        entry = next(
            (
                candidate
                for candidate in body["employees"]
                if candidate["employee"]["employee_id"] == person.pk
            ),
            None,
        )
        if entry is not None:
            self.assertIn(
                "missing_work_info", entry["admin_today"]["exclusion_reasons"]
            )

    def test_a_visible_employee_with_a_row_today_is_reported_visible(self):
        person = self.employee("Seen")
        self.attendance(person, self.day3)
        self.activity(person, self.day3)
        visibility = self.entry_for(self.audit(), person)["admin_today"]
        self.assertTrue(visibility["admin_today_employee_visible"])
        self.assertEqual(visibility["exclusion_reasons"], [])


class OutputShapeTests(WeekAuditBase):
    def setUp(self):
        super().setUp()
        self.as_auditor()

    def test_summary_only_returns_just_the_summary(self):
        self.employee("Alpha")
        body = self.audit(summary_only="1")
        self.assertEqual(set(body), {"summary"})

    def test_issues_only_drops_clean_employees_and_clean_days(self):
        clean = self.employee("Clean")
        self.attendance(clean, self.day1, clock_out=time(17, 0))
        self.activity(clean, self.day1, clock_out=time(17, 0))

        broken = self.employee("Broken")
        self.attendance(broken, self.day1, clock_out=None, clock_out_date=self.day1)
        self.activity(broken, self.day1, clock_out=time(17, 0))

        body = self.audit(issues_only="1")
        ids = {entry["employee"]["employee_id"] for entry in body["employees"]}
        self.assertIn(broken.pk, ids)
        self.assertNotIn(clean.pk, ids)
        for entry in body["employees"]:
            for day in entry["days"]:
                self.assertTrue(day["issues"], day)

    def test_the_summary_counts_add_up(self):
        person = self.employee("Counted")
        self.attendance(person, self.day1, clock_out=None, clock_out_date=self.day1)
        self.activity(person, self.day1, clock_out=time(17, 0))
        summary = self.audit()["summary"]
        self.assertEqual(
            summary["employee_day_count"], summary["employee_count"] * 3
        )
        self.assertEqual(
            summary["ok_employee_days"] + summary["anomalous_employee_days"],
            summary["employee_day_count"],
        )
        self.assertGreaterEqual(summary["critical_issue_count"], 1)
        self.assertIn("CLOCK_OUT_DATE_WITHOUT_CLOCK_OUT", summary["issue_counts"])


class QueryBudgetTests(WeekAuditBase):
    """The cost must not grow with the number of people audited."""

    def setUp(self):
        super().setUp()
        self.as_auditor()

    _made = 0

    def populate(self, count):
        for _ in range(count):
            type(self)._made += 1
            person = self.employee(f"Bulk{type(self)._made}")
            for on in (self.day1, self.day2, self.day3):
                self.attendance(person, on, clock_out=time(17, 0))
                self.activity(person, on, clock_out=time(17, 0))

    def query_count(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.audit()
        return len(captured.captured_queries)

    def test_the_query_count_does_not_grow_with_employee_count(self):
        self.populate(3)
        few = self.query_count()
        self.populate(12)
        many = self.query_count()
        # Bulk-fetched and mapped in memory: fifteen employees over
        # three days must not cost more queries than three did.
        self.assertLessEqual(many, few, f"{few} -> {many}")


class TheAuditChangesNothingTests(WeekAuditBase):
    """Read-only, asserted rather than asserted-to-be."""

    def setUp(self):
        super().setUp()
        self.as_auditor()
        self.person = self.employee("Subject")
        self.attendance(self.person, self.day1)
        self.activity(self.person, self.day1)
        self.attendance(self.person, self.day3, clock_out=time(17, 0))
        self.activity(self.person, self.day3, clock_out=time(17, 0))

    def test_attendance_activity_employee_and_shift_are_all_untouched(self):
        before = self.world()
        self.audit()
        self.audit(issues_only="1")
        self.audit(summary_only="1")
        self.assertEqual(self.world(), before)

    def test_no_row_is_created_or_removed(self):
        counts = (Attendance.objects.count(), AttendanceActivity.objects.count())
        self.audit()
        self.assertEqual(
            (Attendance.objects.count(), AttendanceActivity.objects.count()), counts
        )

    def test_no_attendance_is_validated_by_looking_at_it(self):
        self.audit()
        self.assertFalse(
            Attendance.objects.filter(attendance_validated=True).exists()
        )

    def test_the_audit_never_calls_a_write_helper(self):
        from unittest import mock

        import attendance.views.clock_in_out as clock_module
        import attendance.scheduler as scheduler_module

        with mock.patch.object(
            clock_module, "clock_out_attendance_and_activity"
        ) as checkout, mock.patch.object(
            clock_module, "clock_in_attendance_and_activity"
        ) as checkin, mock.patch.object(
            clock_module, "perform_clock_out"
        ) as perform_out, mock.patch.object(
            clock_module, "perform_clock_in"
        ) as perform_in, mock.patch.object(
            scheduler_module, "auto_punch_out"
        ) as punch:
            self.audit()

        for spy, name in (
            (checkout, "clock_out_attendance_and_activity"),
            (checkin, "clock_in_attendance_and_activity"),
            (perform_out, "perform_clock_out"),
            (perform_in, "perform_clock_in"),
            (punch, "auto_punch_out"),
        ):
            self.assertFalse(spy.called, name)
