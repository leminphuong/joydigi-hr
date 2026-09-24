"""Phase FIX A.1B — automatic finalization has a start date.

FIX A.1 gave the system permission to close a day somebody forgot. This
phase bounds that permission in time.

Sessions from before the cutover are historical. They may be perfectly
clean and perfectly expired and they are still never touched: nobody
working on those days was told a scheduled job would close their shift
for them, and some of those rows are exactly what the original incident
left behind. They belong to an administrator, reviewed by hand.

Two tests carry most of the weight. The boundary — the cutoff day itself
must be *inside* the policy, not the last day outside it — and the NV006
shape, where a protected historical session must survive untouched
*without* costing the employee their new workday. The first is this
phase's decision; the second is what FIX A exists for, and losing it
would be a far worse regression than an untidy old row.

Fixture dates only. No production data is read or written anywhere here.
"""

from datetime import datetime, time, timedelta

from django.test import override_settings
from django.utils import timezone

from attendance.methods.session import (
    EXPIRED_NORMAL,
    HISTORICAL_PROTECTED,
    classify_open_session,
    finalization_cutoff,
)
from attendance.models import Attendance, AttendanceActivity
from attendance.scheduler import auto_punch_out, forgotten_session_finalization
from attendance.views.clock_in_out import finalize_forgotten_sessions
from base.models import EmployeeShiftSchedule

from attendance.tests.test_forgotten_session_finalization import (
    CLOCK_IN,
    END,
    FinalizationBase,
)


class PolicyCutoffTests(FinalizationBase):
    """Where the policy begins, and what lies before it."""

    def at_cutoff(self, on):
        return override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=on.isoformat()
        )

    # -- configuration --------------------------------------------------

    def test_an_unset_cutoff_disables_finalization_entirely(self):
        attendance, activity = self.open_session(self.day1)
        before = self.snapshot(attendance, activity)

        with override_settings(ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=""):
            self.assertIsNone(finalization_cutoff())
            finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(finalized, [])
        self.assertEqual(
            [reason for _row, reason in blocked], [HISTORICAL_PROTECTED]
        )
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_an_unparseable_cutoff_fails_safe_rather_than_open(self):
        attendance, activity = self.open_session(self.day1)
        before = self.snapshot(attendance, activity)

        for nonsense in ("yesterday", "2026-13-40", "24/09/2026", "0"):
            with override_settings(
                ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=nonsense
            ):
                self.assertIsNone(finalization_cutoff(), nonsense)
                finalized, _blocked = finalize_forgotten_sessions(self.employee)
                self.assertEqual(finalized, [], nonsense)

        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_a_valid_cutoff_parses_to_a_date(self):
        with self.at_cutoff(self.day1):
            self.assertEqual(finalization_cutoff(), self.day1)

    # -- the boundary ---------------------------------------------------

    def test_the_day_before_the_cutoff_is_protected(self):
        attendance, activity = self.open_session(self.day0)
        before = self.snapshot(attendance, activity)

        with self.at_cutoff(self.day1):
            finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(finalized, [])
        self.assertEqual(
            [reason for _row, reason in blocked], [HISTORICAL_PROTECTED]
        )
        self.assertEqual(self.snapshot(attendance, activity), before)
        attendance.refresh_from_db()
        self.assertIsNone(attendance.attendance_clock_out)
        self.assertIsNone(attendance.attendance_clock_out_date)

    def test_the_cutoff_day_itself_is_eligible(self):
        # Inclusive: the cutoff is the *first* date the policy governs,
        # not the last date it does not.
        attendance, _activity = self.open_session(self.day1)

        with self.at_cutoff(self.day1):
            finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(len(finalized), 1)
        self.assertEqual(blocked, [])
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, END)
        self.assertEqual(attendance.attendance_clock_out_date, self.day1)

    def test_a_clean_protected_session_is_still_protected(self):
        # Protection does not depend on the row being untidy: a perfectly
        # valid, perfectly expired historical session is left alone.
        attendance, activity = self.open_session(self.day0)
        self.assertEqual(
            classify_open_session(attendance, timezone.now()), EXPIRED_NORMAL
        )
        before = self.snapshot(attendance, activity)

        with self.at_cutoff(self.day1):
            finalize_forgotten_sessions(self.employee)

        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_protected_and_eligible_days_are_handled_independently(self):
        protected, protected_activity = self.open_session(self.day0)
        eligible, _eligible_activity = self.open_session(self.day1)
        protected_before = self.snapshot(protected, protected_activity)

        with self.at_cutoff(self.day1):
            finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual([s.session_date for s in finalized], [self.day1])
        self.assertEqual(
            [reason for _row, reason in blocked], [HISTORICAL_PROTECTED]
        )
        self.assertEqual(
            self.snapshot(protected, protected_activity), protected_before
        )
        eligible.refresh_from_db()
        self.assertEqual(eligible.attendance_clock_out_date, self.day1)

    # -- the scheduler --------------------------------------------------

    def test_the_scheduler_does_nothing_without_a_cutoff(self):
        attendance, activity = self.open_session(self.day1)
        before = self.snapshot(attendance, activity)

        with override_settings(ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=""):
            forgotten_session_finalization()

        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_the_scheduler_does_not_even_query_without_a_cutoff(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.open_session(self.day1)
        with override_settings(ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=""):
            with CaptureQueriesContext(connection) as captured:
                forgotten_session_finalization()
        self.assertEqual(len(captured.captured_queries), 0)

    def test_the_scheduler_leaves_protected_sessions_alone(self):
        protected, protected_activity = self.open_session(self.day0)
        before = self.snapshot(protected, protected_activity)

        with self.at_cutoff(self.day1):
            forgotten_session_finalization()

        self.assertEqual(self.snapshot(protected, protected_activity), before)

    def test_the_scheduler_closes_an_eligible_session(self):
        attendance, _activity = self.open_session(self.day1)

        with self.at_cutoff(self.day1):
            forgotten_session_finalization()

        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, END)


class HistoricalSessionDoesNotTrapAnyoneTests(FinalizationBase):
    """The NV006 shape, with synthetic data."""

    def at_cutoff(self, on):
        return override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=on.isoformat()
        )

    def test_a_protected_historical_session_survives_a_new_days_check_in(self):
        historical, historical_activity = self.open_session(self.day0)
        before = self.snapshot(historical, historical_activity)

        with self.at_cutoff(self.day1):
            response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 200, response.data)

        # Untouched, field by field.
        self.assertEqual(self.snapshot(historical, historical_activity), before)
        historical.refresh_from_db()
        historical_activity.refresh_from_db()
        self.assertIsNone(historical.attendance_clock_out)
        self.assertIsNone(historical.attendance_clock_out_date)
        self.assertIsNone(historical_activity.clock_out)
        self.assertIsNone(historical_activity.clock_out_date)

    def test_today_is_created_anyway_with_its_own_pair(self):
        historical, _activity = self.open_session(self.day0)

        with self.at_cutoff(self.day1):
            response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 200, response.data)
        todays = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNone(todays.attendance_clock_out)
        self.assertNotEqual(todays.pk, historical.pk)

        todays_activity = AttendanceActivity.objects.get(
            employee_id=self.employee,
            attendance_date=self.today,
            clock_out__isnull=True,
        )
        # No cross-date pairing: the activity belongs to today's row.
        self.assertEqual(todays_activity.attendance_date, todays.attendance_date)

    def test_the_fallback_finalizes_only_the_post_cutoff_day(self):
        protected, protected_activity = self.open_session(self.day0)
        eligible, _eligible_activity = self.open_session(self.day1)
        protected_before = self.snapshot(protected, protected_activity)

        with self.at_cutoff(self.day1):
            response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            self.snapshot(protected, protected_activity), protected_before
        )
        eligible.refresh_from_db()
        self.assertEqual(eligible.attendance_clock_out, END)
        self.assertEqual(eligible.attendance_clock_out_date, self.day1)


class NightShiftCutoffTests(FinalizationBase):
    """The cutoff is keyed to the session's own date."""

    def test_a_night_shift_is_judged_by_its_start_not_its_checkout_date(self):
        # Began the day before the cutoff, ends on the cutoff day. Still
        # protected: the policy is keyed to the date the session belongs
        # to, never the calendar date its check-out would land on.
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_night_shift=True, start_time=time(22, 0), end_time=time(6, 0)
        )
        night, night_activity = self.open_session(self.day0, clock_in=time(22, 0))
        before = self.snapshot(night, night_activity)

        with override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=self.day1.isoformat()
        ):
            finalized, blocked = finalize_forgotten_sessions(
                self.employee,
                now=timezone.make_aware(
                    datetime.combine(self.today, time(9, 0))
                ),
            )

        self.assertEqual(finalized, [])
        self.assertEqual(
            [reason for _row, reason in blocked], [HISTORICAL_PROTECTED]
        )
        self.assertEqual(self.snapshot(night, night_activity), before)

    def test_a_post_cutoff_night_shift_still_crosses_midnight_normally(self):
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_night_shift=True, start_time=time(22, 0), end_time=time(6, 0)
        )
        night, night_activity = self.open_session(self.day1, clock_in=time(22, 0))
        before = self.snapshot(night, night_activity)

        with override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=self.day1.isoformat()
        ):
            # Inside its window: eligible by date, and still current, so
            # untouched.
            finalized, _blocked = finalize_forgotten_sessions(
                self.employee,
                now=timezone.make_aware(
                    datetime.combine(self.today, time(3, 0))
                ),
            )

        self.assertEqual(finalized, [])
        self.assertEqual(self.snapshot(night, night_activity), before)


class AutoCheckOutIsUnaffectedByCutoffTests(FinalizationBase):
    """The cutoff belongs to finalization, not to Auto Check Out."""

    def test_auto_check_out_still_closes_a_pre_cutoff_session(self):
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_auto_punch_out_enabled=True, auto_punch_out_time=time(17, 30)
        )
        attendance, _activity = self.open_session(self.day0)

        # No cutoff at all: forgotten-session finalization is switched
        # off, and the administrator's own feature is untouched by that.
        with override_settings(ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=""):
            auto_punch_out()

        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, time(17, 30))

    def test_the_two_features_write_different_times(self):
        # Auto Check Out enabled for day0's weekday only, so each feature
        # gets exactly one session to act on. Enabling it for the whole
        # shift would let `auto_punch_out` close day1 first and the
        # comparison below would be between two runs of one feature.
        EmployeeShiftSchedule.objects.filter(
            shift_id=self.shift, day=self.day_of(self.day0)
        ).update(is_auto_punch_out_enabled=True, auto_punch_out_time=time(17, 30))
        auto_row, _a = self.open_session(self.day0)
        finalized_row, _b = self.open_session(self.day1)

        with override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=self.day1.isoformat()
        ):
            auto_punch_out()
            finalize_forgotten_sessions(self.employee)

        auto_row.refresh_from_db()
        finalized_row.refresh_from_db()
        # The administrator's nominated time, and the shift's own end.
        self.assertEqual(auto_row.attendance_clock_out, time(17, 30))
        self.assertEqual(finalized_row.attendance_clock_out, END)
