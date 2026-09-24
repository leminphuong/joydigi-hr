"""Phase NOTIFICATION B2 — the two *start* reminders.

    start - 5m   SHIFT_START_MINUS_5   "Nhắc chấm công vào"
    start + 5m   SHIFT_START_PLUS_5    "Bạn chưa chấm công vào"

Nothing is a fixed clock time. The tests below assert 07:55/08:05 for an
08:00-17:00 shift, 08:55/09:05 for a 09:00-18:00 shift and 21:55/22:05
for a 22:00-06:00 night shift — from one implementation, with no special
case per shift. The two *end* stages are tested in
`test_end_of_day_checkout.py`, which owns the effective-end calculation.

Four things get more attention than the happy path, because each of them
is a way this could go wrong quietly:

*It must not write to attendance.* Every class here snapshots the
attendance tables around the job and asserts equality, and one test mocks
every write helper and asserts none was called. A notification job that
can close somebody's day is a far worse bug than a missed reminder.

*It must not remind somebody who is already at work.* Which means asking
the canonical session layer rather than inventing a third opinion — and
in particular not reading yesterday's forgotten day shift as today's
attendance, which is the mistake the whole FIX A series exists to end.

*It must not send twice.* The scheduler runs in every gunicorn worker, so
the job is run three times in a row in one test and the count of
notifications asserted.

*A late run must deliver the right reminder, not a stale one.* The two
windows are bounded and disjoint, so a run at 08:06 owes the second
reminder and can never still owe the first.
"""

from datetime import datetime, time, timedelta
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from attendance.methods.reminders import (
    RECOVERY_WINDOW,
    REMINDER_GRACE,
    REMINDER_LEAD,
    STAGE_START_MINUS_5,
    STAGE_START_PLUS_5,
    marker_for,
    process_check_in_reminders,
    push_copy,
    shift_start_datetime,
    stage_due,
)
from attendance.methods.session import needs_check_in, resolve_session
from attendance.models import Attendance, AttendanceActivity, OvertimeRequest
from attendance.scheduler import attendance_reminders
from base.models import (
    CompanyLeaves,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    Holidays,
)
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from notifications.models import Notification


class ReminderBase(TestCase):
    """One employee on an ordinary 08:00-17:00 shift, every weekday."""

    START = time(8, 0)
    END = time(17, 0)
    NIGHT = False

    def setUp(self):
        self.company = make_company("Reminder Co")
        self.user = make_user("reminderuser", password="secret123")
        self.employee = make_employee(
            company=self.company, email="reminder@test.joydigi", user=self.user
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Ca kiểm thử")
        self.shift.company_id.add(self.company)
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )

        self.today = timezone.localdate()
        self.yesterday = self.today - timedelta(days=1)

        # A schedule for every weekday, so the tests can use real dates
        # without caring which day of the week the suite runs on. Off-day
        # behaviour has its own class, which removes them deliberately.
        for day in EmployeeShiftDay.objects.all():
            schedule, created = EmployeeShiftSchedule.objects.update_or_create(
                shift_id=self.shift,
                day=day,
                defaults={
                    "is_night_shift": self.NIGHT,
                    "minimum_working_hour": "08:00",
                    "start_time": self.START,
                    "end_time": self.END,
                },
            )
            if created:
                schedule.company_id.add(self.company)

        # Push never leaves the machine anywhere in this file.
        self._push = mock.patch(
            "joydigi_api.push.send_to_user",
            return_value={"sent": 1, "failed": 0, "deactivated": 0, "skipped": False},
        )
        self.push = self._push.start()
        self.addCleanup(self._push.stop)

    # -- helpers --------------------------------------------------------

    def at(self, hour, minute, on=None, second=0):
        return timezone.make_aware(
            datetime.combine(on or self.today, time(hour, minute, second))
        )

    def day_of(self, on):
        return EmployeeShiftDay.objects.get(day=on.strftime("%A").lower())

    def open_session(self, on, clock_in=None, employee=None):
        employee = employee or self.employee
        clock_in = clock_in or self.START
        attendance = Attendance.objects.create(
            employee_id=employee,
            attendance_date=on,
            attendance_day=self.day_of(on),
            shift_id=self.shift,
            attendance_clock_in=clock_in,
            attendance_clock_in_date=on,
            minimum_hour="08:00",
        )
        activity = AttendanceActivity.objects.create(
            employee_id=employee,
            attendance_date=on,
            clock_in_date=on,
            shift_day=self.day_of(on),
            clock_in=clock_in,
            in_datetime=timezone.make_aware(datetime.combine(on, clock_in)),
        )
        return attendance, activity

    def close_session(self, attendance, activity, at_time=None):
        at_time = at_time or self.END
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=at_time,
            attendance_clock_out_date=attendance.attendance_date,
        )
        AttendanceActivity.objects.filter(pk=activity.pk).update(
            clock_out=at_time, clock_out_date=activity.attendance_date
        )

    def reminders(self, stage=None, on=None, employee=None):
        """Notifications this job has written for our employee."""
        employee = employee or self.employee
        query = Notification.objects.filter(
            target_content_type=ContentType.objects.get_for_model(employee),
            target_object_id=str(employee.pk),
        )
        if stage is not None:
            query = query.filter(
                data__checkin_reminder=marker_for(stage, on or self.today)
            )
        else:
            query = query.exclude(data__checkin_reminder=None)
        return query

    def world(self):
        """Every attendance-shaped row, for read-only checks."""
        return (
            list(
                Attendance.objects.order_by("pk").values_list(
                    "pk",
                    "attendance_date",
                    "attendance_clock_in",
                    "attendance_clock_in_date",
                    "attendance_clock_out",
                    "attendance_clock_out_date",
                    "attendance_worked_hour",
                    "attendance_overtime",
                    "attendance_validated",
                )
            ),
            list(
                AttendanceActivity.objects.order_by("pk").values_list(
                    "pk",
                    "attendance_date",
                    "clock_in",
                    "clock_in_date",
                    "clock_out",
                    "clock_out_date",
                    "out_datetime",
                )
            ),
        )

    def start_at(self, on=None):
        return shift_start_datetime(on or self.today, self.START)


class StageBoundaryTests(ReminderBase):
    """The two windows, to the second, and the gap between them."""

    def test_nothing_is_due_before_the_lead(self):
        self.assertIsNone(
            stage_due(
                self.start_at() - REMINDER_LEAD - timedelta(seconds=1), self.start_at()
            )
        )

    def test_the_pre_start_window_opens_exactly_five_minutes_early(self):
        self.assertEqual(
            stage_due(self.start_at() - REMINDER_LEAD, self.start_at()),
            STAGE_START_MINUS_5,
        )

    def test_the_pre_start_window_is_still_open_one_second_before_the_shift(self):
        self.assertEqual(
            stage_due(self.start_at() - timedelta(seconds=1), self.start_at()),
            STAGE_START_MINUS_5,
        )

    def test_nothing_is_due_in_the_gap_between_the_two_windows(self):
        for offset in (timedelta(0), timedelta(minutes=2), REMINDER_GRACE - timedelta(seconds=1)):
            with self.subTest(offset=offset):
                self.assertIsNone(stage_due(self.start_at() + offset, self.start_at()))

    def test_the_missing_window_opens_exactly_five_minutes_late(self):
        self.assertEqual(
            stage_due(self.start_at() + REMINDER_GRACE, self.start_at()),
            STAGE_START_PLUS_5,
        )

    def test_the_missing_window_survives_a_late_run(self):
        self.assertEqual(
            stage_due(
                self.start_at() + REMINDER_GRACE + RECOVERY_WINDOW - timedelta(seconds=1),
                self.start_at(),
            ),
            STAGE_START_PLUS_5,
        )

    def test_the_missing_window_closes(self):
        self.assertIsNone(
            stage_due(
                self.start_at() + REMINDER_GRACE + RECOVERY_WINDOW, self.start_at()
            )
        )

    def test_a_much_later_run_owes_nothing_at_all(self):
        for later in (timedelta(minutes=30), timedelta(hours=3), timedelta(days=1)):
            with self.subTest(later=later):
                self.assertIsNone(stage_due(self.start_at() + later, self.start_at()))

    def test_the_two_windows_never_overlap(self):
        moment = self.start_at() - REMINDER_LEAD - timedelta(minutes=1)
        end = self.start_at() + REMINDER_GRACE + RECOVERY_WINDOW + timedelta(minutes=1)
        seen = []
        while moment < end:
            stage = stage_due(moment, self.start_at())
            if stage is not None:
                seen.append(stage)
            moment += timedelta(seconds=30)
        # Every pre-start sample comes before every missing sample: the
        # earlier reminder can never be owed after the later one is.
        first_missing = seen.index(STAGE_START_PLUS_5)
        self.assertNotIn(STAGE_START_MINUS_5, seen[first_missing:])


class NormalShiftTests(ReminderBase):
    """08:00-17:00 — the ordinary company day."""

    def test_five_minutes_early_reminds_somebody_who_has_not_checked_in(self):
        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)

    def test_the_reminder_carries_the_real_shift_time(self):
        process_check_in_reminders(now=self.at(7, 55))

        message = self.reminders(STAGE_START_MINUS_5).first()
        self.assertIn("08:00", message.verb)

    def test_the_push_carries_the_real_shift_time(self):
        process_check_in_reminders(now=self.at(7, 55))

        _user, title, body = self.push.call_args.args
        self.assertEqual(title, "Nhắc chấm công vào")
        self.assertIn("08:00", body)

    def test_somebody_already_checked_in_is_not_reminded(self):
        self.open_session(self.today, clock_in=time(7, 40))

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders().count(), 0)

    def test_five_minutes_late_reminds_somebody_still_missing(self):
        process_check_in_reminders(now=self.at(8, 5))

        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 1)
        self.assertIn("08:00", self.reminders(STAGE_START_PLUS_5).first().verb)

    def test_checking_in_between_the_two_moments_stops_the_second(self):
        process_check_in_reminders(now=self.at(7, 55))
        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)

        self.open_session(self.today, clock_in=time(7, 58))
        process_check_in_reminders(now=self.at(8, 5))

        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 0)

    def test_the_second_reminder_does_not_mark_anybody_late(self):
        before = self.world()

        process_check_in_reminders(now=self.at(8, 5))

        self.assertEqual(self.world(), before)


class CustomShiftTests(ReminderBase):
    """09:00-18:00 — nothing in the implementation knows about 08:00."""

    START = time(9, 0)
    END = time(18, 0)

    def test_the_pre_start_reminder_follows_the_schedule(self):
        process_check_in_reminders(now=self.at(7, 55))
        self.assertEqual(self.reminders().count(), 0)

        process_check_in_reminders(now=self.at(8, 55))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)
        self.assertIn("09:00", self.reminders(STAGE_START_MINUS_5).first().verb)

    def test_the_missing_reminder_follows_the_schedule(self):
        process_check_in_reminders(now=self.at(8, 5))
        self.assertEqual(self.reminders().count(), 0)

        process_check_in_reminders(now=self.at(9, 5))

        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 1)


class NightShiftTests(ReminderBase):
    """22:00-06:00 — the start reminders belong to the evening."""

    START = time(22, 0)
    END = time(6, 0)
    NIGHT = True

    def test_the_pre_start_reminder_is_at_twenty_one_fifty_five(self):
        process_check_in_reminders(now=self.at(21, 55))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)
        self.assertIn("22:00", self.reminders(STAGE_START_MINUS_5).first().verb)

    def test_the_missing_reminder_is_at_twenty_two_oh_five(self):
        process_check_in_reminders(now=self.at(22, 5))

        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 1)

    def test_checking_in_before_the_second_moment_stops_it(self):
        self.open_session(self.today, clock_in=time(22, 0))

        process_check_in_reminders(now=self.at(22, 5))

        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 0)

    def test_a_night_shift_still_running_is_not_asked_to_check_in_again(self):
        # Checked in at 22:00 last night and still open at 21:55 tonight:
        # the canonical resolver says they are at work, so nothing is due.
        self.open_session(self.yesterday, clock_in=time(22, 0))

        process_check_in_reminders(now=self.at(21, 55))

        self.assertEqual(self.reminders().count(), 0)


class OffDayTests(ReminderBase):
    """Days nobody is expected to work get no start reminder."""

    def test_no_schedule_for_this_weekday_means_no_reminder(self):
        EmployeeShiftSchedule.objects.filter(
            shift_id=self.shift, day=self.day_of(self.today)
        ).delete()

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders().count(), 0)

    def test_a_company_holiday_means_no_reminder(self):
        Holidays.objects.create(
            name="Tết", start_date=self.today, end_date=self.today
        )

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders().count(), 0)

    def test_a_weekly_off_day_means_no_reminder(self):
        leave = CompanyLeaves.objects.create(
            based_on_week_day=str(self.today.weekday()), based_on_week=None
        )
        leave.company_id.add(self.company)

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders().count(), 0)

    def test_approved_leave_means_no_reminder(self):
        self._approve_leave(self.today)

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders().count(), 0)

    def test_approved_leave_does_not_silence_the_next_day(self):
        self._approve_leave(self.today)

        process_check_in_reminders(now=self.at(7, 55))
        process_check_in_reminders(
            now=self.at(7, 55, on=self.today + timedelta(days=1))
        )

        self.assertEqual(
            self.reminders(STAGE_START_MINUS_5, on=self.today + timedelta(days=1))
            .count(),
            1,
        )

    def test_approved_weekend_overtime_does_not_create_a_start_reminder(self):
        """An approved weekend OT day is still a week-off day.

        The company has no normal attendance requirement on it — the
        summary credits the approved request rather than expecting a
        shift — so a reminder telling somebody to check in at 08:00 would
        be inventing an obligation this application does not have.
        """
        leave = CompanyLeaves.objects.create(
            based_on_week_day=str(self.today.weekday()), based_on_week=None
        )
        leave.company_id.add(self.company)
        OvertimeRequest.objects.create(
            employee_id=self.employee,
            request_date=self.today,
            start_time=time(8, 0),
            end_time=time(12, 0),
            approved=True,
            canceled=False,
        )

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders().count(), 0)

    def _approve_leave(self, on):
        from leave.models import LeaveRequest, LeaveType

        leave_type = LeaveType.objects.create(name="Nghỉ phép")
        LeaveRequest.objects.create(
            employee_id=self.employee,
            leave_type_id=leave_type,
            start_date=on,
            end_date=on,
            requested_days=1,
            status="approved",
        )


class DeduplicationTests(ReminderBase):
    """Each employee, work date and stage: at most one notification."""

    def test_the_same_minute_twice_sends_once(self):
        process_check_in_reminders(now=self.at(7, 55))
        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)

    def test_every_worker_running_the_same_minute_sends_once(self):
        for _worker in range(3):
            process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)

    def test_the_next_minute_does_not_send_again(self):
        process_check_in_reminders(now=self.at(7, 55))
        process_check_in_reminders(now=self.at(7, 56))
        process_check_in_reminders(now=self.at(7, 59))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)

    def test_the_two_stages_are_deduplicated_independently(self):
        process_check_in_reminders(now=self.at(7, 55))
        process_check_in_reminders(now=self.at(8, 5))
        process_check_in_reminders(now=self.at(8, 6))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)
        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 1)

    def test_tomorrow_can_remind_again(self):
        tomorrow = self.today + timedelta(days=1)

        process_check_in_reminders(now=self.at(8, 5))
        process_check_in_reminders(now=self.at(8, 5, on=tomorrow))

        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 1)
        self.assertEqual(
            self.reminders(STAGE_START_PLUS_5, on=tomorrow).count(), 1
        )

    def test_a_restart_mid_window_does_not_re_send(self):
        """Process memory is not what stops the second copy."""
        process_check_in_reminders(now=self.at(8, 5))
        # A fresh process would know nothing; the marker is in the
        # database, so the next run finds it anyway.
        process_check_in_reminders(now=self.at(8, 12))

        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 1)


class RecoveryWindowTests(ReminderBase):
    """A late scheduler still delivers — but only what is current."""

    def test_one_minute_late_still_delivers_the_pre_start_reminder(self):
        process_check_in_reminders(now=self.at(7, 56))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)

    def test_nine_minutes_late_still_delivers_the_missing_reminder(self):
        process_check_in_reminders(now=self.at(8, 14))

        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 1)

    def test_an_expired_pre_start_window_is_not_delivered_late(self):
        process_check_in_reminders(now=self.at(8, 5))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 0)
        self.assertEqual(self.reminders(STAGE_START_PLUS_5).count(), 1)

    def test_a_run_after_both_windows_delivers_nothing(self):
        process_check_in_reminders(now=self.at(8, 30))

        self.assertEqual(self.reminders().count(), 0)


class StateSafetyTests(ReminderBase):
    """The canonical session layer decides, and nothing is written."""

    def test_a_stale_previous_day_shift_does_not_suppress_today(self):
        # Forgot to check out yesterday; today has not been started.
        self.open_session(self.yesterday, clock_in=time(8, 0))

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)

    def test_a_stale_previous_day_shift_is_left_exactly_as_it_was(self):
        self.open_session(self.yesterday, clock_in=time(8, 0))
        before = self.world()

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.world(), before)

    def test_a_malformed_row_is_neither_repaired_nor_reminded(self):
        attendance, _activity = self.open_session(self.today, clock_in=time(8, 0))
        # One of the two check-out columns, which is neither open nor
        # closed. The day has been started, so no check-in reminder — and
        # the row must come out untouched.
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        before = self.world()

        process_check_in_reminders(now=self.at(8, 5))

        self.assertEqual(self.world(), before)
        self.assertEqual(self.reminders().count(), 0)

    def test_the_pass_creates_no_attendance(self):
        before = Attendance.objects.count()

        process_check_in_reminders(now=self.at(7, 55))
        process_check_in_reminders(now=self.at(8, 5))

        self.assertEqual(Attendance.objects.count(), before)

    def test_the_pass_creates_no_activity(self):
        before = AttendanceActivity.objects.count()

        process_check_in_reminders(now=self.at(7, 55))
        process_check_in_reminders(now=self.at(8, 5))

        self.assertEqual(AttendanceActivity.objects.count(), before)

    def test_no_check_in_or_check_out_helper_is_ever_called(self):
        with mock.patch(
            "attendance.views.clock_in_out.perform_clock_in"
        ) as clock_in, mock.patch(
            "attendance.views.clock_in_out.perform_clock_out"
        ) as clock_out, mock.patch(
            "attendance.views.clock_in_out.finalize_forgotten_sessions"
        ) as finalize:
            process_check_in_reminders(now=self.at(7, 55))
            process_check_in_reminders(now=self.at(8, 5))

        clock_in.assert_not_called()
        clock_out.assert_not_called()
        finalize.assert_not_called()


class CanonicalAgreementTests(ReminderBase):
    """`needs_check_in` must answer exactly what `resolve_session` does."""

    def _agree(self):
        bulk = needs_check_in([self.employee.pk], self.today)
        session = resolve_session(self.employee, self.today)
        expected = session.state in {"NO_TODAY_SESSION", "STALE_PREVIOUS_NORMAL_SHIFT_OPEN"}
        self.assertEqual(self.employee.pk in bulk, expected, session.state)

    def test_nothing_at_all(self):
        self._agree()

    def test_today_open(self):
        self.open_session(self.today)
        self._agree()

    def test_today_closed(self):
        attendance, activity = self.open_session(self.today)
        self.close_session(attendance, activity)
        self._agree()

    def test_today_malformed(self):
        attendance, _activity = self.open_session(self.today)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        self._agree()

    def test_stale_previous_day_shift(self):
        self.open_session(self.yesterday)
        self._agree()


class NightCanonicalAgreementTests(NightShiftTests):
    """The same agreement, for a shift that crosses midnight."""

    def test_a_legitimate_night_shift_is_not_needing_check_in(self):
        self.open_session(self.yesterday, clock_in=time(22, 0))

        self.assertNotIn(
            self.employee.pk, needs_check_in([self.employee.pk], self.today)
        )
        self.assertEqual(
            resolve_session(self.employee, self.today).state,
            "LEGITIMATE_PREVIOUS_NIGHT_SHIFT_OPEN",
        )


class PushFailureTests(ReminderBase):
    """Push is an extra channel and never the reminder itself."""

    def test_a_push_that_raises_still_leaves_the_notification(self):
        self.push.side_effect = RuntimeError("firebase down")

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)

    def test_a_push_that_raises_changes_no_attendance(self):
        self.open_session(self.yesterday)
        self.push.side_effect = RuntimeError("firebase down")
        before = self.world()

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.world(), before)

    def test_one_employee_failing_does_not_stop_the_others(self):
        other_user = make_user("reminderuser2", password="secret123")
        other = make_employee(
            company=self.company, email="reminder2@test.joydigi", user=other_user
        )
        EmployeeWorkInformation.objects.filter(employee_id=other).update(
            shift_id=self.shift
        )

        def explode(user, *args, **kwargs):
            if user.pk == self.user.pk:
                raise RuntimeError("firebase down for this one")
            return {"sent": 1, "failed": 0, "deactivated": 0, "skipped": False}

        self.push.side_effect = explode

        process_check_in_reminders(now=self.at(7, 55))

        self.assertEqual(self.reminders(STAGE_START_MINUS_5).count(), 1)
        self.assertEqual(
            self.reminders(STAGE_START_MINUS_5, employee=other).count(), 1
        )


class QueryBudgetTests(ReminderBase):
    """The scan must not cost a query per employee."""

    def _extra_employees(self, count):
        for index in range(count):
            user = make_user(f"budgetuser{index}", password="secret123")
            employee = make_employee(
                company=self.company,
                email=f"budget{index}@test.joydigi",
                user=user,
            )
            EmployeeWorkInformation.objects.filter(employee_id=employee).update(
                shift_id=self.shift
            )

    def _queries_for_a_settled_pass(self):
        # Send first, then measure the run that finds everything already
        # sent: what is left is the scan and the deduplication lookup,
        # which are the parts that must not grow.
        process_check_in_reminders(now=self.at(7, 55))
        with CaptureQueriesContext(connection) as captured:
            process_check_in_reminders(now=self.at(7, 55))
        return len(captured)

    def test_the_scan_does_not_grow_with_headcount(self):
        one = self._queries_for_a_settled_pass()

        self._extra_employees(4)
        many = self._queries_for_a_settled_pass()

        self.assertEqual(
            many,
            one,
            "five employees cost the same scan as one — a per-employee "
            "query here is what makes this job expensive at 07:55",
        )

    def test_an_empty_minute_is_cheap(self):
        self._extra_employees(4)
        with CaptureQueriesContext(connection) as captured:
            process_check_in_reminders(now=self.at(3, 17))
        self.assertLess(len(captured), 12)


class SchedulerEntryTests(ReminderBase):
    """The scheduler wrapper, which is what production actually calls."""

    def test_the_job_delegates_to_the_pass(self):
        with mock.patch(
            "attendance.methods.reminders.process_check_in_reminders"
        ) as process:
            attendance_reminders()
        process.assert_called_once_with()

    def test_the_job_swallows_a_failure_rather_than_killing_the_scheduler(self):
        with mock.patch(
            "attendance.methods.reminders.process_check_in_reminders",
            side_effect=RuntimeError("boom"),
        ):
            attendance_reminders()  # must not raise

    def test_the_reminder_module_is_imported_lazily(self):
        """Startup must not pay for this module, or query anything.

        The scheduler registers its jobs at import time, in every gunicorn
        worker. Importing the reminder implementation there — or touching
        the database — is what turns a job registration into a slow boot.
        """
        import inspect

        import attendance.scheduler as scheduler_module

        source = inspect.getsource(scheduler_module)
        module_level = source.split("def attendance_reminders")[0]
        self.assertNotIn("from attendance.methods.reminders", module_level)
        self.assertIn(
            "from attendance.methods.reminders import process_check_in_reminders",
            inspect.getsource(scheduler_module.attendance_reminders),
        )


class CopyTests(ReminderBase):
    """The wording, which is the only part the employee ever sees."""

    def test_the_pre_start_copy_names_the_shift_start(self):
        title, body = push_copy(STAGE_START_MINUS_5, self.start_at())

        self.assertEqual(title, "Nhắc chấm công vào")
        self.assertEqual(
            body, "Ca làm việc của bạn bắt đầu lúc 08:00. Đừng quên chấm công vào."
        )

    def test_the_missing_copy_names_the_shift_start(self):
        title, body = push_copy(STAGE_START_PLUS_5, self.start_at())

        self.assertEqual(title, "Bạn chưa chấm công vào")
        self.assertIn("bắt đầu lúc 08:00", body)

    def test_a_different_shift_gets_a_different_time(self):
        _title, body = push_copy(
            STAGE_START_MINUS_5, shift_start_datetime(self.today, time(9, 0))
        )

        self.assertIn("09:00", body)
        self.assertNotIn("08:00", body)
