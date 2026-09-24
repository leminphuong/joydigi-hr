"""Phase FIX A.1 — a forgotten day never stays open into a later one.

FIX A made a forgotten Tuesday harmless to Wednesday: it no longer
reported the employee as working, no longer blocked the new day, and
could no longer be closed by Wednesday's check-out. What it deliberately
did *not* do was tidy Tuesday up. Tuesday stayed open forever, waiting
for a human.

This closes it — at the shift's own configured end time, not at midnight,
not at "now", and not at the time an administrator nominated for the
separate Auto Check Out feature. Two things make that safe rather than
convenient, and both are tested here:

*It only ever closes what it can identify exactly.* A row whose two
check-out columns disagree, a session with no usable shift end, an
activity that cannot be paired one-to-one — each is left untouched and
logged. Guessing would mean writing a check-out over somebody's real
working day.

*It does not manufacture overtime.* A day closed at its own configured
end works exactly its minimum hours and earns nothing. Overtime cannot
be suppressed at the check-out layer — `attendance_overtime` is derived
inside `Attendance.save()` — so what holds the invariant is the time
written, and `NoUnapprovedOvertimeTests` says so at length.

And the night-shift tests are the counterweight, as they were in FIX A: a
session legitimately running past midnight must be left alone until its
own configured end, and even then this job does not touch it.
"""

from datetime import datetime, time, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.methods.session import (
    CURRENT_NIGHT,
    CURRENT_NORMAL,
    EXPIRED_NIGHT,
    EXPIRED_NORMAL,
    HISTORICAL_PROTECTED,
    MALFORMED_OR_AMBIGUOUS,
    finalization_cutoff,
    classify_open_session,
    expired_sessions_for,
    session_end_datetime,
)
from attendance.models import Attendance, AttendanceActivity
from attendance.scheduler import forgotten_session_finalization
from attendance.views.clock_in_out import finalize_forgotten_sessions
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

CLOCK_IN = "/api/attendance/clock-in/"

START = time(8, 0)
END = time(17, 0)


class FinalizationBase(TestCase):
    NIGHT = False
    START = START
    END = END

    def setUp(self):
        self.company = make_company("Finalize Co")
        self.user = make_user("finalizeuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="finalize@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )

        self.today = timezone.localdate()
        self.day1 = self.today - timedelta(days=1)
        self.day0 = self.today - timedelta(days=2)
        for on in (self.day0, self.day1, self.today):
            EmployeeShiftSchedule.objects.update_or_create(
                shift_id=self.shift,
                day=EmployeeShiftDay.objects.get(day=on.strftime("%A").lower()),
                defaults={
                    "is_night_shift": self.NIGHT,
                    "minimum_working_hour": "08:00",
                    "start_time": self.START,
                    "end_time": self.END,
                },
            )

        self.client = APIClient()
        self.client.force_authenticate(user=self.fresh_user())

        # Phase FIX A.1B: finalization only acts inside the policy
        # period, so every test below declares one. The cutoff is set two
        # days before the oldest fixture date, which keeps these tests
        # about finalization behaviour; the cutoff *boundary* itself has
        # its own class, `PolicyCutoffTests`.
        self._cutoff = override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=(
                self.day0 - timedelta(days=1)
            ).isoformat()
        )
        self._cutoff.enable()
        self.addCleanup(self._cutoff.disable)

    def fresh_user(self):
        return type(self.user).objects.get(pk=self.user.pk)

    def day_of(self, on):
        return EmployeeShiftDay.objects.get(day=on.strftime("%A").lower())

    def open_session(self, on, clock_in=None, overtime="00:00"):
        clock_in = clock_in or self.START
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=on,
            attendance_day=self.day_of(on),
            shift_id=self.shift,
            attendance_clock_in=clock_in,
            attendance_clock_in_date=on,
            minimum_hour="08:00",
            attendance_overtime=overtime,
        )
        activity = AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=on,
            clock_in_date=on,
            shift_day=self.day_of(on),
            clock_in=clock_in,
            in_datetime=timezone.make_aware(datetime.combine(on, clock_in)),
        )
        return attendance, activity

    def snapshot(self, attendance, activity):
        attendance.refresh_from_db()
        activity.refresh_from_db()
        return (
            attendance.attendance_clock_out,
            attendance.attendance_clock_out_date,
            attendance.attendance_worked_hour,
            attendance.attendance_overtime,
            attendance.attendance_validated,
            activity.clock_out,
            activity.clock_out_date,
            activity.out_datetime,
        )


class SessionEndTests(FinalizationBase):
    """When a session was configured to end."""

    def test_a_day_shift_ends_on_its_own_date(self):
        attendance, _activity = self.open_session(self.day1)
        ends_at = session_end_datetime(attendance)
        self.assertEqual(timezone.localtime(ends_at).date(), self.day1)
        self.assertEqual(timezone.localtime(ends_at).time(), END)

    def test_no_schedule_means_no_answer(self):
        attendance, _activity = self.open_session(self.day1)
        Attendance.objects.filter(pk=attendance.pk).update(attendance_day=None)
        attendance.refresh_from_db()
        self.assertIsNone(session_end_datetime(attendance))

    def test_no_end_time_means_no_answer(self):
        attendance, _activity = self.open_session(self.day1)
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(end_time=None)
        self.assertIsNone(session_end_datetime(attendance))


class ClassificationTests(FinalizationBase):
    """Expiry is decided by the shift's end, never by the calendar."""

    def test_a_day_shift_before_its_end_is_current(self):
        attendance, _activity = self.open_session(self.today)
        before_end = timezone.make_aware(
            datetime.combine(self.today, time(12, 0))
        )
        self.assertEqual(
            classify_open_session(attendance, before_end), CURRENT_NORMAL
        )

    def test_a_day_shift_after_its_end_is_expired(self):
        attendance, _activity = self.open_session(self.today)
        after_end = timezone.make_aware(
            datetime.combine(self.today, time(17, 1))
        )
        self.assertEqual(
            classify_open_session(attendance, after_end), EXPIRED_NORMAL
        )

    def test_a_half_written_row_is_never_classified_as_expired(self):
        attendance, _activity = self.open_session(self.day1)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        attendance.refresh_from_db()
        self.assertEqual(
            classify_open_session(attendance), MALFORMED_OR_AMBIGUOUS
        )

    def test_a_session_with_no_usable_end_is_never_expired(self):
        attendance, _activity = self.open_session(self.day1)
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(end_time=None)
        self.assertEqual(
            classify_open_session(attendance), MALFORMED_OR_AMBIGUOUS
        )

    def test_a_closed_row_is_not_a_session(self):
        attendance, _activity = self.open_session(self.day1)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0), attendance_clock_out_date=self.day1
        )
        attendance.refresh_from_db()
        self.assertIsNone(classify_open_session(attendance))


class NormalDayFinalizationTests(FinalizationBase):
    """§13 — the exact case, closed at the configured end."""

    def test_it_closes_the_pair_at_the_shift_end_time(self):
        attendance, activity = self.open_session(self.day1)

        finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(len(finalized), 1)
        self.assertEqual(blocked, [])
        attendance.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, END)
        self.assertEqual(attendance.attendance_clock_out_date, self.day1)
        self.assertIsNotNone(activity.clock_out)
        self.assertEqual(activity.clock_out_date, self.day1)

    def test_the_pair_belongs_to_the_same_session_date(self):
        attendance, activity = self.open_session(self.day1)
        finalize_forgotten_sessions(self.employee)
        attendance.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(attendance.attendance_date, activity.attendance_date)

    def test_it_creates_no_row_for_any_other_day(self):
        self.open_session(self.day1)
        before = set(Attendance.objects.values_list("pk", flat=True))
        finalize_forgotten_sessions(self.employee)
        self.assertEqual(set(Attendance.objects.values_list("pk", flat=True)), before)

    def test_it_uses_the_shift_end_not_midnight_or_now(self):
        attendance, _activity = self.open_session(self.day1)
        finalize_forgotten_sessions(self.employee)
        attendance.refresh_from_db()
        for wrong in (time(23, 59), time(23, 59, 59), time(0, 0)):
            self.assertNotEqual(attendance.attendance_clock_out, wrong)
        self.assertEqual(attendance.attendance_clock_out, END)

    def test_it_ignores_the_auto_check_out_setting_entirely(self):
        # Disabled, and with a different nominated time: neither is read.
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_auto_punch_out_enabled=False, auto_punch_out_time=time(19, 0)
        )
        attendance, _activity = self.open_session(self.day1)

        finalize_forgotten_sessions(self.employee)

        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, END)
        self.assertNotEqual(attendance.attendance_clock_out, time(19, 0))

    def test_todays_own_session_is_left_open(self):
        attendance, activity = self.open_session(self.today)
        before = self.snapshot(attendance, activity)
        finalize_forgotten_sessions(self.employee)
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_worked_hours_and_validation_still_run(self):
        attendance, _activity = self.open_session(self.day1)
        finalize_forgotten_sessions(self.employee)
        attendance.refresh_from_db()
        # 08:00-17:00 minus the unpaid 12:00-13:00 lunch hour.
        self.assertEqual(attendance.attendance_worked_hour, "08:00")


class NoUnapprovedOvertimeTests(FinalizationBase):
    """§6 — finalization must not manufacture overtime.

    `attendance_overtime` is derived, not stored: `Attendance.save()`
    calls `update_attendance_overtime()` on every save and recomputes it,
    together with `overtime_second` and `at_work_second`, from
    `attendance_worked_hour` and `minimum_hour`. There is therefore no
    honest way to "suppress overtime for a finalized day" at the
    check-out layer — anything written there is overwritten, and forcing
    it afterwards would leave those three fields contradicting one
    another.

    What actually holds the invariant is the *time* finalization writes:
    the shift's configured end. A day that ran its configured length
    works exactly its minimum hours and earns nothing. Overtime can only
    appear when the employee's own check-in was earlier than their shift
    start — their record, not the system's invention — and even then
    finalization adds nothing past the shift end. Both halves are
    asserted below.
    """

    def test_a_normal_forgotten_day_earns_no_overtime(self):
        # The case that matters: 08:00 start, 17:00 configured end, an
        # 08:00 minimum. Finalization writes 17:00 and nothing is owed.
        attendance, _activity = self.open_session(self.day1)
        finalize_forgotten_sessions(self.employee)
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_worked_hour, "08:00")
        self.assertEqual(attendance.attendance_overtime, "00:00")

    def test_finalization_never_writes_past_the_shift_end(self):
        # The only lever finalization has over overtime. An early arrival
        # produces overtime from the arrival, and the closing time is
        # still the configured end rather than "now" or midnight.
        attendance, _activity = self.open_session(self.day1, clock_in=time(6, 0))
        finalize_forgotten_sessions(self.employee)
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, END)

    def test_overtime_from_an_early_arrival_is_the_employees_own_record(self):
        # 06:00 to the 17:00 shift end is ten hours after lunch, against
        # an eight-hour minimum. The two extra hours come from when the
        # employee clocked in, and a manual check-out at the same instant
        # produces the identical number — finalization is not special.
        attendance, _activity = self.open_session(self.day1, clock_in=time(6, 0))
        finalize_forgotten_sessions(self.employee)
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_worked_hour, "10:00")
        self.assertEqual(attendance.attendance_overtime, "02:00")

    def test_finalization_and_manual_checkout_agree_given_the_same_times(self):
        # Proof that finalization applies no different overtime rule: the
        # same clock-in and the same clock-out produce the same overtime
        # whichever path wrote it.
        finalized, _blocked = None, None
        auto_row, _auto_activity = self.open_session(self.day1, clock_in=time(6, 0))
        finalized, _blocked = finalize_forgotten_sessions(self.employee)
        self.assertEqual(len(finalized), 1)
        auto_row.refresh_from_db()

        manual_row, manual_activity = self.open_session(
            self.today, clock_in=time(6, 0)
        )
        started = timezone.make_aware(
            datetime.combine(self.today, time(6, 0))
        )
        AttendanceActivity.objects.filter(pk=manual_activity.pk).update(
            clock_in=time(6, 0), in_datetime=started
        )
        # Check out at the same wall-clock moment the shift ends.
        from attendance.methods.utils import Request

        from attendance.views.clock_in_out import perform_clock_out

        ends_at = timezone.make_aware(datetime.combine(self.today, END))
        perform_clock_out(
            Request(
                user=self.fresh_user(),
                date=self.today,
                time=END,
                datetime=ends_at,
                trusted_device=True,
            )
        )
        manual_row.refresh_from_db()

        self.assertEqual(
            manual_row.attendance_overtime, auto_row.attendance_overtime
        )
        self.assertEqual(
            manual_row.attendance_worked_hour, auto_row.attendance_worked_hour
        )


class MalformedAndAmbiguousTests(FinalizationBase):
    """§10, §11 — never guess, never repair."""

    def test_a_half_written_row_is_left_exactly_as_it_is(self):
        attendance, activity = self.open_session(self.day1)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        before = self.snapshot(attendance, activity)

        finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(finalized, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_two_open_activities_on_one_day_block_that_session(self):
        attendance, activity = self.open_session(self.day1)
        second = AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=self.day1,
            clock_in_date=self.day1,
            shift_day=self.day_of(self.day1),
            clock_in=time(13, 0),
        )
        before = self.snapshot(attendance, activity)

        finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(finalized, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(self.snapshot(attendance, activity), before)
        second.refresh_from_db()
        self.assertIsNone(second.clock_out)

    def test_a_session_with_no_open_activity_is_blocked(self):
        attendance, activity = self.open_session(self.day1)
        AttendanceActivity.objects.filter(pk=activity.pk).update(
            clock_out=time(17, 0), clock_out_date=self.day1
        )
        before = self.snapshot(attendance, activity)

        finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(finalized, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_a_missing_shift_schedule_blocks_the_session(self):
        attendance, activity = self.open_session(self.day1)
        Attendance.objects.filter(pk=attendance.pk).update(attendance_day=None)
        attendance.refresh_from_db()
        before = self.snapshot(attendance, activity)

        finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(finalized, [])
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_one_ambiguous_day_does_not_stop_a_clean_one(self):
        # Two forgotten days: one unpairable, one clean. The clean one is
        # closed; the other is reported.
        broken, broken_activity = self.open_session(self.day0)
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=self.day0,
            clock_in_date=self.day0,
            shift_day=self.day_of(self.day0),
            clock_in=time(13, 0),
        )
        clean, _clean_activity = self.open_session(self.day1)
        broken_before = self.snapshot(broken, broken_activity)

        finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual([s.session_date for s in finalized], [self.day1])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(self.snapshot(broken, broken_activity), broken_before)
        clean.refresh_from_db()
        self.assertEqual(clean.attendance_clock_out_date, self.day1)

    def test_several_clean_forgotten_days_are_each_closed_on_their_own_date(self):
        first, _a = self.open_session(self.day0)
        second, _b = self.open_session(self.day1)

        finalized, blocked = finalize_forgotten_sessions(self.employee)

        self.assertEqual(len(finalized), 2)
        self.assertEqual(blocked, [])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.attendance_clock_out_date, self.day0)
        self.assertEqual(second.attendance_clock_out_date, self.day1)


class NightShiftTests(FinalizationBase):
    """§15 — a legitimate night is never cut short, and never closed here."""

    NIGHT = True
    START = time(22, 0)
    END = time(6, 0)

    def ends_next_morning(self, on):
        return timezone.make_aware(
            datetime.combine(on + timedelta(days=1), self.END)
        )

    def test_the_session_end_falls_on_the_following_morning(self):
        attendance, _activity = self.open_session(self.day1, clock_in=time(22, 0))
        ends_at = session_end_datetime(attendance)
        self.assertEqual(timezone.localtime(ends_at).date(), self.today)
        self.assertEqual(timezone.localtime(ends_at).time(), self.END)

    def test_it_is_current_at_every_moment_before_its_end(self):
        attendance, _activity = self.open_session(self.day1, clock_in=time(22, 0))
        for moment in (time(0, 1), time(3, 0), time(5, 59)):
            now = timezone.make_aware(datetime.combine(self.today, moment))
            self.assertEqual(
                classify_open_session(attendance, now),
                CURRENT_NIGHT,
                moment,
            )

    def test_it_becomes_expired_only_after_its_configured_end(self):
        attendance, _activity = self.open_session(self.day1, clock_in=time(22, 0))
        after = timezone.make_aware(datetime.combine(self.today, time(6, 1)))
        self.assertEqual(classify_open_session(attendance, after), EXPIRED_NIGHT)

    def test_a_current_night_session_is_never_finalized(self):
        attendance, activity = self.open_session(self.day1, clock_in=time(22, 0))
        before = self.snapshot(attendance, activity)
        now = timezone.make_aware(datetime.combine(self.today, time(3, 0)))

        finalized, _blocked = finalize_forgotten_sessions(self.employee, now=now)

        self.assertEqual(finalized, [])
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_even_an_expired_night_session_is_reported_not_closed(self):
        attendance, activity = self.open_session(self.day1, clock_in=time(22, 0))
        before = self.snapshot(attendance, activity)
        now = timezone.make_aware(datetime.combine(self.today, time(9, 0)))

        finalized, blocked = finalize_forgotten_sessions(self.employee, now=now)

        self.assertEqual(finalized, [])
        self.assertEqual([reason for _row, reason in blocked], [EXPIRED_NIGHT])
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_expired_sessions_for_never_offers_a_night_shift_as_ready(self):
        self.open_session(self.day1, clock_in=time(22, 0))
        now = timezone.make_aware(datetime.combine(self.today, time(23, 0)))
        ready, _blocked = expired_sessions_for(self.employee, now=now)
        self.assertEqual(ready, [])


class NextDayFallbackTests(FinalizationBase):
    """§14 — the scheduler did not run; check-in cleans up first."""

    def test_checking_in_finalizes_yesterday_then_opens_today(self):
        stale, stale_activity = self.open_session(self.day1)

        response = self.client.post(CLOCK_IN)
        self.assertEqual(response.status_code, 200, response.data)

        stale.refresh_from_db()
        stale_activity.refresh_from_db()
        self.assertEqual(stale.attendance_clock_out, END)
        self.assertEqual(stale.attendance_clock_out_date, self.day1)
        self.assertIsNotNone(stale_activity.clock_out)

        todays = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNone(todays.attendance_clock_out)
        self.assertTrue(
            AttendanceActivity.objects.filter(
                employee_id=self.employee,
                attendance_date=self.today,
                clock_out__isnull=True,
            ).exists()
        )

    def test_a_normal_forgotten_day_gains_no_overtime_from_the_fallback(self):
        # 08:00 start, 17:00 configured end, 08:00 minimum: the fallback
        # closes at the shift end and nothing is owed.
        stale, _activity = self.open_session(self.day1)
        self.client.post(CLOCK_IN)
        stale.refresh_from_db()
        self.assertEqual(stale.attendance_clock_out, END)
        self.assertEqual(stale.attendance_overtime, "00:00")

    def test_the_fallback_closes_at_the_shift_end_not_later(self):
        # An early arrival earns overtime from the arrival itself; what
        # matters is that the fallback writes the configured end and
        # nothing beyond it. See `NoUnapprovedOvertimeTests` for why
        # `attendance_overtime` cannot be suppressed at this layer.
        stale, _activity = self.open_session(self.day1, clock_in=time(6, 0))
        self.client.post(CLOCK_IN)
        stale.refresh_from_db()
        self.assertEqual(stale.attendance_clock_out, END)
        self.assertEqual(stale.attendance_clock_out_date, self.day1)

    def test_an_unfinalizable_yesterday_does_not_trap_today(self):
        # A half-written row cannot be finalized. Refusing today's
        # check-in over it would trap the employee — the exact bug FIX A
        # exists to prevent — so today proceeds and yesterday is logged.
        stale, stale_activity = self.open_session(self.day1)
        Attendance.objects.filter(pk=stale.pk).update(
            attendance_clock_out=time(17, 0)
        )
        before = self.snapshot(stale, stale_activity)

        response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.snapshot(stale, stale_activity), before)
        self.assertTrue(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists()
        )

    def test_a_failing_finalization_still_lets_today_begin(self):
        # Phase FUTURE-SAFE reverses this deliberately. FIX A.1 tied the
        # two together: if closing yesterday raised, today was not
        # created either. That is the safer half of an atomic pair, but
        # it also means a row nobody can finalize stops an employee
        # working — which is precisely the trap FIX A exists to prevent,
        # and a strictly worse outcome than one untidy historical row.
        #
        # What is kept is the half that matters: yesterday is either
        # closed completely or left exactly as it was. Each session now
        # runs in its own savepoint, so a failure rolls back that session
        # alone and cannot reach the check-in that follows it.
        from unittest import mock

        stale, stale_activity = self.open_session(self.day1)
        before = self.snapshot(stale, stale_activity)

        with mock.patch(
            "attendance.views.clock_in_out.finalize_forgotten_sessions",
            side_effect=RuntimeError("boom"),
        ):
            response = self.client.post(CLOCK_IN)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(
            Attendance.objects.filter(
                employee_id=self.employee, attendance_date=self.today
            ).exists(),
            "today must open even when yesterday cannot be tidied",
        )
        self.assertEqual(
            self.snapshot(stale, stale_activity),
            before,
            "yesterday is left exactly as it was — never half-closed",
        )

    def test_a_night_shift_in_progress_is_not_disturbed_by_a_check_in(self):
        # FIX A already refuses the check-in itself; this asserts the
        # finalization added here does not reach the night session.
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_night_shift=True, start_time=time(22, 0), end_time=time(6, 0)
        )
        night, night_activity = self.open_session(self.day1, clock_in=time(22, 0))
        before = self.snapshot(night, night_activity)

        self.client.post(CLOCK_IN)

        self.assertEqual(self.snapshot(night, night_activity), before)


class SchedulerJobTests(FinalizationBase):
    """§8 — the periodic job, separate from Auto Check Out."""

    def test_it_closes_a_forgotten_day_without_the_setting_enabled(self):
        attendance, activity = self.open_session(self.day1)

        forgotten_session_finalization()

        attendance.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(attendance.attendance_clock_out, END)
        self.assertEqual(attendance.attendance_clock_out_date, self.day1)

    def test_it_leaves_todays_session_alone(self):
        attendance, activity = self.open_session(self.today)
        before = self.snapshot(attendance, activity)
        forgotten_session_finalization()
        self.assertEqual(self.snapshot(attendance, activity), before)

    def test_it_does_nothing_when_there_is_nothing_to_do(self):
        before = set(Attendance.objects.values_list("pk", flat=True))
        forgotten_session_finalization()
        self.assertEqual(set(Attendance.objects.values_list("pk", flat=True)), before)

    def test_one_employees_broken_data_does_not_stop_the_job(self):
        from unittest import mock

        self.open_session(self.day1)
        with mock.patch(
            "attendance.views.clock_in_out.finalize_forgotten_sessions",
            side_effect=RuntimeError("boom"),
        ):
            forgotten_session_finalization()  # must not raise

    def test_it_issues_no_row_lock(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.open_session(self.day1)
        with CaptureQueriesContext(connection) as captured:
            forgotten_session_finalization()
        for query in captured.captured_queries:
            self.assertNotIn("FOR UPDATE", query["sql"].upper())


class CheckOnlineEquivalenceTests(FinalizationBase):
    """§18 — the two implementations of "online" must agree.

    `Employee.check_online()` uses the bulk `employees_online()` when a
    request context exists and `resolve_session()` when one does not.
    Two implementations of one rule drift; this is the test the comment
    in `employee/models.py` refers to.
    """

    def scenarios(self):
        from attendance.methods.session import employees_online, resolve_session

        return employees_online, resolve_session

    def assert_agree(self, label):
        employees_online, resolve_session = self.scenarios()
        bulk = self.employee.pk in employees_online([self.employee.pk])
        single = resolve_session(self.employee).is_online
        self.assertEqual(bulk, single, label)

    def test_they_agree_with_nothing_at_all(self):
        self.assert_agree("no rows")

    def test_they_agree_with_an_open_session_today(self):
        self.open_session(self.today)
        self.assert_agree("today open")

    def test_they_agree_with_a_closed_session_today(self):
        attendance, _activity = self.open_session(self.today)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=END, attendance_clock_out_date=self.today
        )
        self.assert_agree("today closed")

    def test_they_agree_with_a_forgotten_day_shift_yesterday(self):
        self.open_session(self.day1)
        self.assert_agree("stale previous day shift")

    def test_they_agree_with_a_half_written_row_today(self):
        attendance, _activity = self.open_session(self.today)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=END
        )
        self.assert_agree("malformed today")

    def test_they_agree_with_an_open_night_shift_from_yesterday(self):
        EmployeeShiftSchedule.objects.filter(shift_id=self.shift).update(
            is_night_shift=True, start_time=time(22, 0), end_time=time(6, 0)
        )
        self.open_session(self.day1, clock_in=time(22, 0))
        self.assert_agree("night shift crossing midnight")

    def test_they_agree_with_an_older_forgotten_day(self):
        self.open_session(self.day0)
        self.assert_agree("two days ago")
