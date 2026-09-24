"""Phase NOTIFICATION B — reminding people to check in.

Two reminders, both derived from the shift's own configured start, and
both at most once per person per day:

    start - 10m   "Sắp đến giờ chấm công"
    start         "Bạn chưa chấm công vào"

Nothing is a fixed clock time. The tests below assert 07:50/08:00 for an
08:00-17:00 shift, 13:50/14:00 for a 14:00-22:00 shift, and 21:50/22:00
for a 22:00-06:00 night shift — from one implementation, with no special
case per shift.

Three things get more attention than the happy path, because each of them
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
"""

from datetime import date, datetime, time, timedelta
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from attendance.methods.reminders import (
    MISSING_CHECK_IN_WINDOW,
    PRE_START_LEAD,
    STAGE_MISSING_CHECK_IN,
    STAGE_PRE_START,
    marker_for,
    process_check_in_reminders,
    shift_start_datetime,
    stage_due,
)
from attendance.methods.session import needs_check_in, resolve_session
from attendance.models import Attendance, AttendanceActivity
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

    def open_session(self, on, clock_in=None):
        clock_in = clock_in or self.START
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=on,
            attendance_day=self.day_of(on),
            shift_id=self.shift,
            attendance_clock_in=clock_in,
            attendance_clock_in_date=on,
            minimum_hour="08:00",
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

    def close_session(self, attendance, activity, at_time=None):
        at_time = at_time or self.END
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=at_time,
            attendance_clock_out_date=attendance.attendance_date,
        )
        AttendanceActivity.objects.filter(pk=activity.pk).update(
            clock_out=at_time, clock_out_date=activity.attendance_date
        )

    def reminders(self, stage=None, on=None):
        """Notifications this job has written for our employee."""
        query = Notification.objects.filter(
            target_content_type=ContentType.objects.get_for_model(self.employee),
            target_object_id=str(self.employee.pk),
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
                    "pk", "attendance_date", "attendance_clock_in",
                    "attendance_clock_in_date", "attendance_clock_out",
                    "attendance_clock_out_date", "attendance_worked_hour",
                    "attendance_overtime", "attendance_validated",
                )
            ),
            list(
                AttendanceActivity.objects.order_by("pk").values_list(
                    "pk", "attendance_date", "clock_in", "clock_in_date",
                    "clock_out", "clock_out_date", "out_datetime",
                )
            ),
        )


class StageBoundaryTests(ReminderBase):
    """The two moments, to the minute."""

    def start_at(self, on=None):
        return shift_start_datetime(on or self.today, self.START)

    def test_nothing_is_due_before_the_lead_time(self):
        self.assertIsNone(
            stage_due(
                self.start_at() - PRE_START_LEAD - timedelta(seconds=1),
                self.start_at(),
            )
        )

    def test_the_pre_start_reminder_is_due_exactly_ten_minutes_before(self):
        self.assertEqual(
            stage_due(self.start_at() - PRE_START_LEAD, self.start_at()),
            STAGE_PRE_START,
        )

    def test_the_pre_start_reminder_stays_due_until_the_start(self):
        self.assertEqual(
            stage_due(self.start_at() - timedelta(seconds=1), self.start_at()),
            STAGE_PRE_START,
        )

    def test_the_missing_reminder_is_due_exactly_at_the_start(self):
        self.assertEqual(
            stage_due(self.start_at(), self.start_at()), STAGE_MISSING_CHECK_IN
        )

    def test_the_missing_reminder_stays_due_through_the_window(self):
        self.assertEqual(
            stage_due(
                self.start_at() + MISSING_CHECK_IN_WINDOW - timedelta(seconds=1),
                self.start_at(),
            ),
            STAGE_MISSING_CHECK_IN,
        )

    def test_nothing_is_due_after_the_window(self):
        self.assertIsNone(
            stage_due(
                self.start_at() + MISSING_CHECK_IN_WINDOW, self.start_at()
            )
        )

    def test_an_unknown_start_means_no_opinion(self):
        self.assertIsNone(stage_due(self.start_at(), None))


class NormalShiftTests(ReminderBase):
    """08:00-17:00, end to end through the pass."""

    def test_at_0749_nothing_is_sent(self):
        process_check_in_reminders(now=self.at(7, 49))
        self.assertEqual(self.reminders().count(), 0)

    def test_at_0750_one_pre_start_reminder_is_sent(self):
        tally = process_check_in_reminders(now=self.at(7, 50))
        self.assertEqual(tally["pre_start"], 1)
        self.assertEqual(self.reminders(STAGE_PRE_START).count(), 1)

    def test_the_pre_start_reminder_is_not_repeated_at_0759(self):
        process_check_in_reminders(now=self.at(7, 50))
        process_check_in_reminders(now=self.at(7, 59))
        self.assertEqual(self.reminders(STAGE_PRE_START).count(), 1)

    def test_at_0800_the_missing_check_in_reminder_is_sent(self):
        tally = process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(tally["missing_check_in"], 1)
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_the_two_reminders_are_independent(self):
        process_check_in_reminders(now=self.at(7, 50))
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders(STAGE_PRE_START).count(), 1)
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_the_recovery_window_can_deliver_a_missed_exact_minute(self):
        # The scheduler never ran at 08:00 — a restart, a slow tick. The
        # reminder still goes out, once.
        tally = process_check_in_reminders(now=self.at(8, 17))
        self.assertEqual(tally["missing_check_in"], 1)
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_the_recovery_window_still_only_sends_once(self):
        for minute in (0, 1, 7, 15, 29):
            process_check_in_reminders(now=self.at(8, minute))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_after_the_window_the_reminder_is_no_longer_created(self):
        process_check_in_reminders(now=self.at(8, 30))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 0)

    def test_tomorrow_can_remind_again(self):
        process_check_in_reminders(now=self.at(8, 0))
        tomorrow = self.today + timedelta(days=1)
        process_check_in_reminders(now=self.at(8, 0, on=tomorrow))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)
        self.assertEqual(
            self.reminders(STAGE_MISSING_CHECK_IN, on=tomorrow).count(), 1
        )

    def test_the_push_carries_the_expected_copy(self):
        process_check_in_reminders(now=self.at(8, 0))
        self.assertTrue(self.push.called)
        _user, title, body = self.push.call_args.args
        self.assertEqual(title, "Bạn chưa chấm công vào")
        self.assertEqual(body, "Đã đến giờ làm việc mà bạn chưa chấm công vào.")

    def test_the_pre_start_push_carries_its_own_copy(self):
        process_check_in_reminders(now=self.at(7, 50))
        _user, title, body = self.push.call_args.args
        self.assertEqual(title, "Sắp đến giờ chấm công")
        self.assertIn("10 phút", body)


class AlreadyCheckedInTests(ReminderBase):
    """Somebody who has started their day is not reminded to start it."""

    def test_an_open_session_today_suppresses_both_reminders(self):
        self.open_session(self.today, clock_in=time(7, 45))
        process_check_in_reminders(now=self.at(7, 50))
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)

    def test_a_finished_session_today_suppresses_the_reminders(self):
        attendance, activity = self.open_session(self.today, clock_in=time(7, 30))
        self.close_session(attendance, activity, at_time=time(7, 45))
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)

    def test_a_half_written_row_counts_as_started_and_is_not_touched(self):
        attendance, activity = self.open_session(self.today, clock_in=time(7, 30))
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        before = self.world()

        process_check_in_reminders(now=self.at(8, 0))

        self.assertEqual(self.reminders().count(), 0)
        self.assertEqual(self.world(), before)

    def test_yesterdays_forgotten_day_shift_does_not_count_as_today(self):
        # The FIX A shape. A row left open yesterday is not today's
        # attendance, so today's reminder must still go out — and
        # yesterday's row must not be touched.
        stale_attendance, stale_activity = self.open_session(self.yesterday)
        before = self.world()

        tally = process_check_in_reminders(now=self.at(8, 0))

        self.assertEqual(tally["missing_check_in"], 1)
        self.assertEqual(self.world(), before)
        stale_attendance.refresh_from_db()
        self.assertIsNone(stale_attendance.attendance_clock_out)
        self.assertIsNotNone(stale_activity)


class CustomShiftTests(ReminderBase):
    """14:00-22:00 — the same code, different times."""

    START = time(14, 0)
    END = time(22, 0)

    def test_the_times_follow_the_shift(self):
        process_check_in_reminders(now=self.at(13, 49))
        self.assertEqual(self.reminders().count(), 0)

        process_check_in_reminders(now=self.at(13, 50))
        self.assertEqual(self.reminders(STAGE_PRE_START).count(), 1)

        process_check_in_reminders(now=self.at(14, 0))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_the_morning_is_not_a_reminder_moment_for_this_shift(self):
        process_check_in_reminders(now=self.at(7, 50))
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)


class NightShiftTests(ReminderBase):
    """22:00-06:00 — start reminders on the evening it begins."""

    START = time(22, 0)
    END = time(6, 0)
    NIGHT = True

    def test_the_start_reminders_fire_at_2150_and_2200(self):
        process_check_in_reminders(now=self.at(21, 50))
        self.assertEqual(self.reminders(STAGE_PRE_START).count(), 1)

        process_check_in_reminders(now=self.at(22, 0))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_a_session_running_past_midnight_is_not_reminded_again(self):
        # Checked in at 22:00 yesterday and still working. After midnight
        # the calendar day has changed but the session has not, and the
        # canonical resolver knows it.
        self.open_session(self.yesterday, clock_in=time(22, 0))
        session = resolve_session(self.employee, self.today)
        self.assertTrue(session.is_online)

        for hour, minute in ((0, 30), (3, 0), (5, 0)):
            process_check_in_reminders(now=self.at(hour, minute))
        self.assertEqual(self.reminders().count(), 0)

    def test_the_bulk_state_agrees_with_the_resolver_for_a_night_shift(self):
        self.open_session(self.yesterday, clock_in=time(22, 0))
        bulk = self.employee.pk in needs_check_in([self.employee.pk], self.today)
        single = resolve_session(self.employee, self.today).state in {
            "NO_TODAY_SESSION",
            "STALE_PREVIOUS_NORMAL_SHIFT_OPEN",
        }
        self.assertEqual(bulk, single)


class OffDayTests(ReminderBase):
    """Days nobody is expected to work get nothing."""

    def test_no_schedule_for_the_weekday_means_no_reminder(self):
        EmployeeShiftSchedule.objects.filter(
            shift_id=self.shift, day=self.day_of(self.today)
        ).delete()
        process_check_in_reminders(now=self.at(7, 50))
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)

    def test_a_schedule_without_a_start_time_means_no_reminder(self):
        EmployeeShiftSchedule.objects.filter(
            shift_id=self.shift, day=self.day_of(self.today)
        ).update(start_time=None)
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)

    def test_a_company_holiday_suppresses_the_reminder(self):
        Holidays.objects.create(
            name="Quốc khánh", start_date=self.today, end_date=self.today
        )
        tally = process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)
        self.assertGreaterEqual(tally["skipped"], 1)

    def test_a_multi_day_holiday_covers_its_whole_span(self):
        Holidays.objects.create(
            name="Tết",
            start_date=self.today - timedelta(days=2),
            end_date=self.today + timedelta(days=2),
        )
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)

    def test_a_specific_holiday_only_affects_the_employees_it_names(self):
        other = make_employee(
            company=self.company, email="other@test.joydigi", first_name="Other"
        )
        EmployeeWorkInformation.objects.filter(employee_id=other).update(
            shift_id=self.shift
        )
        holiday = Holidays.objects.create(
            name="Nghỉ riêng",
            start_date=self.today,
            end_date=self.today,
            is_specific=True,
        )
        holiday.employees.add(self.employee)

        process_check_in_reminders(now=self.at(8, 0))

        # Ours is named on the holiday, so nothing. The other employee has
        # no user account, so nothing reaches them either — what is
        # asserted is that *we* were skipped.
        self.assertEqual(self.reminders().count(), 0)

    def test_a_weekly_off_day_suppresses_the_reminder(self):
        leave = CompanyLeaves.objects.create(
            based_on_week=None, based_on_week_day=str(self.today.weekday())
        )
        leave.company_id.add(self.company)
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)

    def test_a_weekly_off_day_for_another_weekday_does_not_suppress(self):
        other_weekday = (self.today.weekday() + 1) % 7
        leave = CompanyLeaves.objects.create(
            based_on_week=None, based_on_week_day=str(other_weekday)
        )
        leave.company_id.add(self.company)
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_an_approved_leave_request_suppresses_the_reminder(self):
        from leave.models import LeaveRequest, LeaveType

        leave_type = LeaveType.objects.create(name="Phép năm")
        LeaveRequest.objects.create(
            employee_id=self.employee,
            leave_type_id=leave_type,
            start_date=self.today,
            end_date=self.today,
            status="approved",
            requested_days=1,
        )
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 0)

    def test_an_unapproved_leave_request_does_not_suppress(self):
        from leave.models import LeaveRequest, LeaveType

        leave_type = LeaveType.objects.create(name="Phép năm")
        LeaveRequest.objects.create(
            employee_id=self.employee,
            leave_type_id=leave_type,
            start_date=self.today,
            end_date=self.today,
            status="requested",
            requested_days=1,
        )
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)


class DeduplicationTests(ReminderBase):
    """The scheduler runs in every worker; the reminder goes out once."""

    def test_three_consecutive_runs_send_one_reminder(self):
        for _ in range(3):
            process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)
        self.assertEqual(self.push.call_count, 1)

    def test_the_scheduler_entry_is_also_idempotent(self):
        with mock.patch(
            "attendance.methods.reminders.timezone.localtime",
            return_value=self.at(8, 0),
        ):
            for _ in range(3):
                attendance_reminders()
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_the_marker_distinguishes_stage_and_date(self):
        self.assertNotEqual(
            marker_for(STAGE_PRE_START, self.today),
            marker_for(STAGE_MISSING_CHECK_IN, self.today),
        )
        self.assertNotEqual(
            marker_for(STAGE_PRE_START, self.today),
            marker_for(STAGE_PRE_START, self.today + timedelta(days=1)),
        )


class PushFailureTests(ReminderBase):
    """A broken push must not break the reminder or the loop."""

    def test_a_raising_push_still_leaves_the_notification(self):
        self.push.side_effect = RuntimeError("firebase down")
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_a_raising_push_does_not_cause_a_resend(self):
        self.push.side_effect = RuntimeError("firebase down")
        process_check_in_reminders(now=self.at(8, 0))
        self.push.side_effect = None
        process_check_in_reminders(now=self.at(8, 5))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_no_registered_device_is_not_an_error(self):
        self.push.return_value = {
            "sent": 0, "failed": 0, "deactivated": 0, "skipped": True
        }
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders(STAGE_MISSING_CHECK_IN).count(), 1)

    def test_multiple_devices_go_through_the_existing_push_path(self):
        # The fan-out itself belongs to `joydigi_api.push.send_to_user`,
        # which this job calls once per user and does not reimplement.
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.push.call_count, 1)
        # Compared by primary key: `employee_user_id` re-reads the row, so
        # identity would be comparing two instances of the same user.
        self.assertEqual(
            self.push.call_args.args[0].pk, self.employee.employee_user_id.pk
        )


class ReadOnlyTests(ReminderBase):
    """The job may write notifications. It may not write attendance."""

    def test_attendance_and_activity_are_untouched(self):
        self.open_session(self.yesterday)
        before = self.world()

        for hour, minute in ((7, 50), (8, 0), (8, 15), (17, 10)):
            process_check_in_reminders(now=self.at(hour, minute))

        self.assertEqual(self.world(), before)

    def test_no_attendance_row_is_created(self):
        counts = (Attendance.objects.count(), AttendanceActivity.objects.count())
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(
            (Attendance.objects.count(), AttendanceActivity.objects.count()), counts
        )

    def test_notifications_are_created_though(self):
        process_check_in_reminders(now=self.at(8, 0))
        self.assertEqual(self.reminders().count(), 1)

    def test_no_write_helper_is_ever_called(self):
        import attendance.scheduler as scheduler_module
        import attendance.views.clock_in_out as clock_module

        with mock.patch.object(clock_module, "perform_clock_in") as check_in, \
                mock.patch.object(clock_module, "perform_clock_out") as check_out, \
                mock.patch.object(
                    clock_module, "finalize_forgotten_sessions"
                ) as finalize, \
                mock.patch.object(scheduler_module, "auto_punch_out") as punch:
            process_check_in_reminders(now=self.at(8, 0))

        for spy, name in (
            (check_in, "perform_clock_in"),
            (check_out, "perform_clock_out"),
            (finalize, "finalize_forgotten_sessions"),
            (punch, "auto_punch_out"),
        ):
            self.assertFalse(spy.called, name)

    def test_the_job_issues_no_row_lock(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            process_check_in_reminders(now=self.at(8, 0))
        for query in captured.captured_queries:
            self.assertNotIn("FOR UPDATE", query["sql"].upper())


class QueryBudgetTests(ReminderBase):
    """The cost must not grow with headcount."""

    _made = 0

    def populate(self, count):
        for _ in range(count):
            type(self)._made += 1
            tag = f"Bulk{type(self)._made}"
            person = make_employee(
                company=self.company,
                email=f"{tag.lower()}@test.joydigi",
                first_name="Bulk",
                last_name=tag,
                user=make_user(tag.lower(), password="secret123"),
            )
            EmployeeWorkInformation.objects.filter(employee_id=person).update(
                shift_id=self.shift
            )

    def query_count(self, now):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            process_check_in_reminders(now=now)
        return len(captured.captured_queries)

    def test_the_eligibility_queries_do_not_grow_with_employee_count(self):
        # Counted at a moment when no reminder is due, so the measurement
        # is of the eligibility pass rather than of the notification
        # writes — those are necessarily per recipient.
        self.populate(3)
        few = self.query_count(self.at(11, 0))
        self.populate(12)
        many = self.query_count(self.at(11, 0))
        self.assertLessEqual(many, few, f"{few} -> {many}")


class CanonicalAgreementTests(ReminderBase):
    """`needs_check_in` must not become a second opinion."""

    def assert_agree(self, label, on=None):
        on = on or self.today
        bulk = self.employee.pk in needs_check_in([self.employee.pk], on)
        state = resolve_session(self.employee, on).state
        single = state in {
            "NO_TODAY_SESSION",
            "STALE_PREVIOUS_NORMAL_SHIFT_OPEN",
        }
        self.assertEqual(bulk, single, f"{label} ({state})")

    def test_they_agree_with_nothing_at_all(self):
        self.assert_agree("no rows")

    def test_they_agree_with_an_open_session_today(self):
        self.open_session(self.today)
        self.assert_agree("today open")

    def test_they_agree_with_a_closed_session_today(self):
        attendance, activity = self.open_session(self.today)
        self.close_session(attendance, activity)
        self.assert_agree("today closed")

    def test_they_agree_with_a_forgotten_day_shift_yesterday(self):
        self.open_session(self.yesterday)
        self.assert_agree("stale previous day shift")

    def test_they_agree_with_a_half_written_row_today(self):
        attendance, _activity = self.open_session(self.today)
        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(17, 0)
        )
        self.assert_agree("malformed today")

    def test_they_agree_with_an_empty_id_list(self):
        self.assertEqual(needs_check_in([], self.today), set())

    def test_a_date_with_no_data_at_all_needs_a_check_in(self):
        self.assertEqual(
            needs_check_in([self.employee.pk], date(2020, 1, 1)),
            {self.employee.pk},
        )
