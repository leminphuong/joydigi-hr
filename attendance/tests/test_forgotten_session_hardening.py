"""Phase FUTURE-SAFE — hardening of forgotten day-shift finalization.

A month-long production audit found nineteen forgotten day sessions that
nothing had ever closed, and could not tell from the outside whether the
feature was switched off or simply failing. Three properties made that
possible, and this file pins all three:

* an off switch that said nothing at all, so a deployment that never set
  the cutoff looked identical to one where the job was working;
* one unreasonable session taking its neighbours down with it, because
  the loop had no per-session boundary;
* a crash on an incomplete employee record, because the shared check-out
  path reads the work information without a guard.

Nothing here changes *what is eligible*. The policy — after day
rollover, inside the cutoff period, day shifts only, never a malformed
row, never an unpairable activity, never a night shift — is unchanged,
and its own tests in `test_forgotten_session_finalization.py` and
`test_finalization_policy_cutoff.py` still stand. `HardeningBoundaryTests`
asserts that the freeze held.

`FinalizationBase` is imported rather than re-created so these tests run
against exactly the fixture the policy tests use; it defines no test
methods of its own, so nothing is collected twice.
"""

import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.db.models.query import QuerySet
from django.test import override_settings
from django.utils import timezone

from attendance.methods.session import (
    CUTOFF_CONFIGURED,
    CUTOFF_INVALID,
    CUTOFF_NOT_CONFIGURED,
    finalization_cutoff_state,
)
from attendance.models import Attendance, AttendanceActivity
from attendance.scheduler import forgotten_session_finalization
from attendance.views.clock_in_out import (
    FINALIZATION_ERROR,
    MISSING_EMPLOYEE_PROFILE,
    finalize_forgotten_sessions,
    perform_clock_out,
)
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_employee, make_user

from attendance.tests.test_forgotten_session_finalization import (
    CLOCK_IN,
    END,
    START,
    FinalizationBase,
)

#: Where the scheduler's logger actually publishes — `attendance.scheduler`
#: imports it from `base.backends`, so that is the name to capture.
SCHEDULER_LOG = "base.backends"

#: `finalize_forgotten_sessions` uses its own module logger.
FINALIZE_LOG = "attendance.views.clock_in_out"


class CutoffVisibilityTests(FinalizationBase):
    """An unusable cutoff must be loud — and must still change nothing."""

    def setUp(self):
        super().setUp()
        # These tests own the setting, so the base fixture's cutoff is
        # lifted for their duration and restored afterwards.
        self._cutoff.disable()
        self.addCleanup(self._cutoff.enable)
        self.attendance, self.activity = self.open_session(self.day1)
        self.before = self.snapshot(self.attendance, self.activity)

    def run_job(self):
        with self.assertLogs(SCHEDULER_LOG, level="WARNING") as captured:
            forgotten_session_finalization()
        return "\n".join(captured.output)

    def test_a_missing_cutoff_is_reported_and_writes_nothing(self):
        with override_settings():
            del settings.ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF
            logged = self.run_job()

        self.assertIn("DISABLED", logged)
        self.assertIn(CUTOFF_NOT_CONFIGURED, logged)
        self.assertEqual(self.snapshot(self.attendance, self.activity), self.before)

    def test_an_empty_cutoff_is_reported_and_writes_nothing(self):
        with override_settings(ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=""):
            logged = self.run_job()

        self.assertIn(CUTOFF_NOT_CONFIGURED, logged)
        self.assertIn("ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF", logged)
        self.assertEqual(self.snapshot(self.attendance, self.activity), self.before)

    def test_an_invalid_cutoff_is_reported_and_writes_nothing(self):
        with override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF="24-09-2026"
        ):
            logged = self.run_job()

        self.assertIn(CUTOFF_INVALID, logged)
        self.assertEqual(self.snapshot(self.attendance, self.activity), self.before)

    def test_the_invalid_value_itself_is_never_logged(self):
        typed_by_hand = "31/02/2026-not-a-date"
        with override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=typed_by_hand
        ):
            logged = self.run_job()

        self.assertNotIn(typed_by_hand, logged)
        self.assertNotIn("31/02", logged)

    def test_a_usable_cutoff_does_not_report_the_feature_as_disabled(self):
        with override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=self.day0.isoformat()
        ):
            self.assertEqual(finalization_cutoff_state(), CUTOFF_CONFIGURED)
            with self.assertLogs(SCHEDULER_LOG, level="WARNING") as captured:
                # An unrelated line so `assertLogs` has something to
                # capture even when the job itself stays silent.
                logging.getLogger(SCHEDULER_LOG).warning("probe")
                forgotten_session_finalization()

        self.assertNotIn("DISABLED", "\n".join(captured.output))

    def test_the_state_helper_never_returns_the_value(self):
        for raw in ("", "   ", "nonsense", "2026-09-24"):
            with self.subTest(raw=raw):
                with override_settings(
                    ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=raw
                ):
                    state = finalization_cutoff_state()
                self.assertIn(
                    state,
                    {CUTOFF_CONFIGURED, CUTOFF_NOT_CONFIGURED, CUTOFF_INVALID},
                )

    def test_the_state_helper_tells_the_three_cases_apart(self):
        cases = {
            "": CUTOFF_NOT_CONFIGURED,
            "   ": CUTOFF_NOT_CONFIGURED,
            "not-a-date": CUTOFF_INVALID,
            "2026-09-24": CUTOFF_CONFIGURED,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                with override_settings(
                    ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=raw
                ):
                    self.assertEqual(finalization_cutoff_state(), expected)


class PerSessionIsolationTests(FinalizationBase):
    """One session that fails must not cost the employee the others."""

    def test_a_raising_session_does_not_stop_a_later_clean_one(self):
        bad, bad_activity = self.open_session(self.day0)
        good, good_activity = self.open_session(self.day1)
        real = perform_clock_out

        def explode_on_the_older(request, *args, **kwargs):
            if request.date == self.day0:
                raise RuntimeError("row is unreasonable")
            return real(request, *args, **kwargs)

        with mock.patch(
            "attendance.views.clock_in_out.perform_clock_out",
            side_effect=explode_on_the_older,
        ):
            finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual([s.session_date for s in finalized], [self.day1])
        self.assertIn(FINALIZATION_ERROR, [reason for _row, reason in blocked])

        bad.refresh_from_db()
        bad_activity.refresh_from_db()
        self.assertIsNone(bad.attendance_clock_out, "the bad row is untouched")
        self.assertIsNone(bad_activity.clock_out)

        good.refresh_from_db()
        good_activity.refresh_from_db()
        self.assertIsNotNone(good.attendance_clock_out, "the good row closed")
        self.assertIsNotNone(good_activity.clock_out)

    def test_a_failure_writes_no_half_finished_pair(self):
        attendance, activity = self.open_session(self.day1)
        before = self.snapshot(attendance, activity)

        def close_then_explode(request, *args, **kwargs):
            Attendance.objects.filter(pk=attendance.pk).update(
                attendance_clock_out=END
            )
            raise RuntimeError("died after touching the row")

        with mock.patch(
            "attendance.views.clock_in_out.perform_clock_out",
            side_effect=close_then_explode,
        ):
            _finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertIn(FINALIZATION_ERROR, [reason for _row, reason in blocked])
        self.assertEqual(
            self.snapshot(attendance, activity),
            before,
            "the savepoint must undo the partial write",
        )

    def test_a_refusal_leaves_the_row_exactly_as_it_was(self):
        attendance, activity = self.open_session(self.day1)
        before = self.snapshot(attendance, activity)

        with mock.patch(
            "attendance.views.clock_in_out.perform_clock_out",
            return_value=(None, False, {"code": "SOMETHING_REFUSED"}),
        ):
            finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(finalized, [])
        self.assertIn("SOMETHING_REFUSED", [reason for _row, reason in blocked])
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_the_failure_is_logged_without_the_exception_message(self):
        self.open_session(self.day1)
        with mock.patch(
            "attendance.views.clock_in_out.perform_clock_out",
            side_effect=RuntimeError("row content nobody should see"),
        ):
            with self.assertLogs(FINALIZE_LOG, level="WARNING") as captured:
                finalize_forgotten_sessions(self.employee)

        logged = "\n".join(captured.output)
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("row content nobody should see", logged)

    def test_every_blocked_reason_is_logged(self):
        # Two open activities: unpairable, and therefore refused.
        self.open_session(self.day1)
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=self.day1,
            clock_in_date=self.day1,
            shift_day=self.day_of(self.day1),
            clock_in=time(13, 0),
            in_datetime=timezone.make_aware(
                datetime.combine(self.day1, time(13, 0))
            ),
        )

        with self.assertLogs(FINALIZE_LOG, level="WARNING") as captured:
            _finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertTrue(blocked)
        self.assertIn("skipped", "\n".join(captured.output))


class IncompleteProfileTests(FinalizationBase):
    """A half-built employee record must be reported, never a crash."""

    def test_an_employee_with_no_linked_user_is_blocked_not_crashed(self):
        attendance, activity = self.open_session(self.day1)
        before = self.snapshot(attendance, activity)
        type(self.employee).objects.filter(pk=self.employee.pk).update(
            employee_user_id=None
        )
        employee = type(self.employee).objects.get(pk=self.employee.pk)

        finalized, blocked = finalize_forgotten_sessions(employee)

        self.assertEqual(finalized, [])
        self.assertIn(
            MISSING_EMPLOYEE_PROFILE, [reason for _row, reason in blocked]
        )
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_an_employee_with_no_work_information_is_blocked(self):
        attendance, activity = self.open_session(self.day1)
        before = self.snapshot(attendance, activity)
        EmployeeWorkInformation.objects.filter(
            employee_id=self.employee
        ).delete()
        employee = type(self.employee).objects.get(pk=self.employee.pk)

        finalized, blocked = finalize_forgotten_sessions(employee)

        self.assertEqual(finalized, [])
        self.assertIn(
            MISSING_EMPLOYEE_PROFILE, [reason for _row, reason in blocked]
        )
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_the_scheduled_job_survives_an_incomplete_profile(self):
        self.open_session(self.day1)
        type(self.employee).objects.filter(pk=self.employee.pk).update(
            employee_user_id=None
        )

        forgotten_session_finalization()  # must not raise

    def test_one_broken_employee_does_not_stop_another(self):
        self.open_session(self.day1)
        type(self.employee).objects.filter(pk=self.employee.pk).update(
            employee_user_id=None
        )

        other_user = make_user("finalizeuser2", password="secret123")
        other = make_employee(
            company=self.company,
            email="finalize2@test.joydigi",
            user=other_user,
        )
        EmployeeWorkInformation.objects.filter(employee_id=other).update(
            shift_id=self.shift
        )
        row = Attendance.objects.create(
            employee_id=other,
            attendance_date=self.day1,
            attendance_day=self.day_of(self.day1),
            shift_id=self.shift,
            attendance_clock_in=START,
            attendance_clock_in_date=self.day1,
            minimum_hour="08:00",
        )
        AttendanceActivity.objects.create(
            employee_id=other,
            attendance_date=self.day1,
            clock_in_date=self.day1,
            shift_day=self.day_of(self.day1),
            clock_in=START,
            in_datetime=timezone.make_aware(
                datetime.combine(self.day1, START)
            ),
        )

        forgotten_session_finalization()

        row.refresh_from_db()
        self.assertIsNotNone(
            row.attendance_clock_out,
            "the healthy employee's forgotten day must still be closed",
        )


class CheckInIsNeverTrappedTests(FinalizationBase):
    """Yesterday's mess must never cost somebody today's check-in."""

    def check_in_today(self):
        return self.client.post(CLOCK_IN, {}, format="json")

    def test_a_raising_finalization_still_lets_the_employee_check_in(self):
        self.open_session(self.day1)

        with mock.patch(
            "attendance.views.clock_in_out.finalize_forgotten_sessions",
            side_effect=RuntimeError("finalization exploded"),
        ):
            response = self.check_in_today()

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertTrue(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists()
        )

    def test_a_blocked_old_session_still_lets_the_employee_check_in(self):
        # Two open activities on the forgotten day: unpairable, so
        # finalization refuses it by design.
        attendance, activity = self.open_session(self.day1)
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=self.day1,
            clock_in_date=self.day1,
            shift_day=self.day_of(self.day1),
            clock_in=time(13, 0),
            in_datetime=timezone.make_aware(
                datetime.combine(self.day1, time(13, 0))
            ),
        )
        before = self.snapshot(attendance, activity)

        response = self.check_in_today()

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(
            self.snapshot(attendance, activity),
            before,
            "the blocked session is left exactly as it was",
        )

    def test_the_old_session_never_becomes_todays_session(self):
        old, _activity = self.open_session(self.day1)

        self.check_in_today()

        today_row = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertNotEqual(today_row.pk, old.pk)
        self.assertEqual(today_row.attendance_date, self.today)

    def test_a_successful_finalization_precedes_the_new_day(self):
        old, old_activity = self.open_session(self.day1)

        response = self.check_in_today()

        self.assertEqual(response.status_code, 200, response.content[:300])
        old.refresh_from_db()
        old_activity.refresh_from_db()
        self.assertEqual(old.attendance_clock_out_date, self.day1)
        self.assertEqual(old_activity.clock_out_date, self.day1)
        self.assertTrue(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists()
        )

    def test_an_incomplete_profile_does_not_block_the_new_day(self):
        self.open_session(self.day1)
        EmployeeWorkInformation.objects.filter(
            employee_id=self.employee
        ).update(shift_id=self.shift)

        response = self.check_in_today()

        self.assertEqual(response.status_code, 200, response.content[:300])


class HardeningBoundaryTests(FinalizationBase):
    """What this phase deliberately did not change."""

    def test_todays_own_open_session_is_still_never_finalized(self):
        attendance, activity = self.open_session(self.today)
        before = self.snapshot(attendance, activity)

        finalize_forgotten_sessions(
            self.employee,
            now=timezone.make_aware(
                datetime.combine(self.today, time(23, 59))
            ),
        )

        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_a_pre_cutoff_session_is_still_protected(self):
        old_date = self.day0 - timedelta(days=5)
        from base.models import EmployeeShiftDay, EmployeeShiftSchedule

        EmployeeShiftSchedule.objects.update_or_create(
            shift_id=self.shift,
            day=EmployeeShiftDay.objects.get(
                day=old_date.strftime("%A").lower()
            ),
            defaults={
                "is_night_shift": False,
                "minimum_working_hour": "08:00",
                "start_time": START,
                "end_time": END,
            },
        )
        attendance, activity = self.open_session(old_date)
        before = self.snapshot(attendance, activity)

        finalize_forgotten_sessions(self.employee)

        self.assertEqual(
            self.snapshot(attendance, activity),
            before,
            "history predating the cutoff is never repaired",
        )

    def test_an_expired_night_shift_is_still_never_finalized(self):
        from base.models import EmployeeShiftSchedule

        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_night_shift=True, start_time=time(22, 0), end_time=time(6, 0)
        )
        attendance, activity = self.open_session(self.day0, clock_in=time(22, 0))
        before = self.snapshot(attendance, activity)

        finalize_forgotten_sessions(self.employee)

        self.assertEqual(
            self.snapshot(attendance, activity),
            before,
            "night-shift policy is frozen in this phase",
        )

    def test_a_legitimate_night_shift_crossing_midnight_is_untouched(self):
        from base.models import EmployeeShiftSchedule

        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_night_shift=True, start_time=time(22, 0), end_time=time(6, 0)
        )
        attendance, activity = self.open_session(self.day1, clock_in=time(22, 0))
        before = self.snapshot(attendance, activity)

        # 03:00 the next morning: inside the configured window.
        finalize_forgotten_sessions(
            self.employee,
            now=timezone.make_aware(
                datetime.combine(self.today, time(3, 0))
            ),
        )

        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_a_malformed_row_is_still_never_repaired(self):
        attendance, activity = self.open_session(self.day1)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=END
        )
        before = self.snapshot(attendance, activity)

        finalize_forgotten_sessions(self.employee)

        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_no_row_lock_is_taken_anywhere_on_the_hardened_path(self):
        self.open_session(self.day1)
        calls = []
        original = QuerySet.select_for_update

        def spy(self, *args, **kwargs):
            calls.append(True)
            return original(self, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            finalize_forgotten_sessions(self.employee)
            forgotten_session_finalization()

        self.assertEqual(calls, [], "finalization must take no row lock")

    def test_the_hardened_modules_contain_no_lock_call(self):
        for path in (
            "attendance/views/clock_in_out.py",
            "attendance/scheduler.py",
            "attendance/methods/session.py",
        ):
            source = Path(path).read_text(encoding="utf-8")
            code = "\n".join(
                line
                for line in source.splitlines()
                if not line.strip().startswith("#")
            )
            with self.subTest(path=path):
                self.assertNotIn("select_for_update(", code)

    def test_nothing_here_offers_a_historical_repair_entry_point(self):
        source = Path("attendance/views/clock_in_out.py").read_text(
            encoding="utf-8"
        )
        for forbidden in ("def repair_", "def backfill_", "def fix_historical"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
