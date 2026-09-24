"""
Phases ATTENDANCE-END-OF-DAY-AUTO-CHECKOUT-NOTIFY-SAFE-IMPLEMENT-1 and
ATTENDANCE-PUSH-NOTIFICATION-FCM-SAFE-IMPLEMENT-1.

Reminding people to check out — twice, and then leaving them alone.

Nothing is closed automatically. A session left open stays open for the
rest of that day, and the next day is a fresh session that yesterday
cannot touch: the tests below assert both halves of that, because the
dangerous failure is not the open row itself but tomorrow's check-in
being refused because of it.

The boundaries are asserted to the microsecond, because "at 16:55" can
mean either side of that instant.

No push ever leaves the machine: Firebase is mocked at the send boundary,
and the in-app notification is exercised through its real model so the
deduplication being tested is the real one.
"""

import datetime
import uuid
from datetime import time, timedelta
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone

from attendance.methods.end_of_day import (
    REMINDER_BEFORE_END,
    SECOND_REMINDER_AFTER_END,
    STAGE_FIRST_REMINDER,
    STAGE_SECOND_REMINDER,
    effective_end_datetime,
    effective_end_instant,
    effective_end_seconds,
    process_end_of_day,
    stage_due,
)
from attendance.methods.reminders import RECOVERY_WINDOW, marker_for
from attendance.methods.utils import Request
from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceLateComeEarlyOut,
    OvertimeRequest,
)
from attendance.views.clock_in_out import perform_clock_in, perform_clock_out
from base.models import (
    CheckInPolicy,
    Company,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation
from notifications.models import Notification

HOUR = 3600


class EffectiveEndTests(TestCase):
    """The day's effective end, as arithmetic."""

    def test_an_ordinary_day_ends_when_the_shift_does(self):
        self.assertEqual(effective_end_seconds(time(17, 0), []), 17 * HOUR)

    def test_approved_overtime_pushes_the_end_later(self):
        self.assertEqual(
            effective_end_seconds(time(17, 0), [(time(17, 0), time(19, 0))]),
            19 * HOUR,
        )

    def test_the_latest_of_several_approved_windows_wins(self):
        windows = [(time(17, 0), time(18, 0)), (time(18, 30), time(19, 30))]
        self.assertEqual(
            effective_end_seconds(time(17, 0), windows), 19 * HOUR + 30 * 60
        )

    def test_a_gap_between_windows_is_not_bridged(self):
        # The end moves to 19:30, but 18:00-18:30 is still not covered —
        # this helper answers "when does the day finish", not "how much
        # overtime is approved", and it must not change the latter.
        from attendance.methods.worktime import approved_overtime_seconds

        windows = [(time(17, 0), time(18, 0)), (time(18, 30), time(19, 30))]
        self.assertEqual(approved_overtime_seconds(windows), 2 * HOUR)

    def test_overlapping_windows_are_merged_not_summed(self):
        windows = [(time(17, 0), time(19, 0)), (time(18, 0), time(19, 0))]
        self.assertEqual(effective_end_seconds(time(17, 0), windows), 19 * HOUR)

    def test_overtime_ending_before_the_shift_does_not_shorten_the_day(self):
        self.assertEqual(
            effective_end_seconds(time(17, 0), [(time(15, 0), time(16, 0))]),
            17 * HOUR,
        )

    def test_no_shift_and_no_overtime_has_no_answer(self):
        self.assertIsNone(effective_end_seconds(None, []))

    def test_overtime_alone_can_define_the_end(self):
        self.assertEqual(
            effective_end_seconds(None, [(time(17, 0), time(19, 0))]), 19 * HOUR
        )


class StageBoundaryTests(TestCase):
    """The two moments, to the microsecond — and nothing after them."""

    def setUp(self):
        self.end = timezone.make_aware(datetime.datetime(2026, 9, 10, 17, 0, 0))

    def at(self, delta):
        return self.end + delta

    def test_nothing_is_due_before_the_first_reminder(self):
        self.assertIsNone(
            stage_due(
                self.at(-REMINDER_BEFORE_END - timedelta(microseconds=1)), self.end
            )
        )

    def test_the_first_reminder_is_due_exactly_five_minutes_before(self):
        self.assertEqual(
            stage_due(self.at(-REMINDER_BEFORE_END), self.end), STAGE_FIRST_REMINDER
        )

    def test_the_first_reminder_window_closes_at_the_shift_end(self):
        # Phase NOTIFICATION B2 made the windows bounded and disjoint.
        # Before, the first reminder stayed due right up to the second
        # one; now there is a deliberate gap between them, so a late run
        # can never still owe the earlier message.
        self.assertEqual(
            stage_due(self.at(-timedelta(microseconds=1)), self.end),
            STAGE_FIRST_REMINDER,
        )
        self.assertIsNone(stage_due(self.end, self.end))
        self.assertIsNone(
            stage_due(
                self.at(SECOND_REMINDER_AFTER_END - timedelta(microseconds=1)),
                self.end,
            )
        )

    def test_the_second_reminder_is_due_exactly_five_minutes_after(self):
        self.assertEqual(
            stage_due(self.at(SECOND_REMINDER_AFTER_END), self.end),
            STAGE_SECOND_REMINDER,
        )

    def test_the_second_reminder_survives_a_late_run_but_expires(self):
        # Phase NOTIFICATION B2: a bounded recovery window rather than
        # "due for ever". Ten minutes is enough to survive a restart; a
        # reminder delivered hours later is about a moment that has gone.
        self.assertEqual(
            stage_due(
                self.at(SECOND_REMINDER_AFTER_END + RECOVERY_WINDOW)
                - timedelta(microseconds=1),
                self.end,
            ),
            STAGE_SECOND_REMINDER,
        )
        for later in (
            SECOND_REMINDER_AFTER_END + RECOVERY_WINDOW,
            timedelta(hours=1),
            timedelta(hours=3),
        ):
            self.assertIsNone(stage_due(self.at(later), self.end), later)

    def test_an_unknown_end_means_no_opinion(self):
        self.assertIsNone(stage_due(self.end, None))


class EndOfDayBaseTests(TestCase):
    """Shared fixtures: one employee on an ordinary 08:00-17:00 weekday."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            company="End Of Day Corp", hq=True, address="x", country="VN",
            state="HN", city="HN", zip="10000",
        )
        CheckInPolicy.objects.create(company_id=cls.company, late_threshold_minutes=10)
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        cls.shift.company_id.add(cls.company)
        cls.work_type = WorkType.objects.create(work_type="Office")
        cls.work_type.company_id.add(cls.company)
        Department.objects.create(department="Eng").company_id.add(cls.company)

        cls.today = timezone.localtime().date()
        while cls.today.weekday() > 4:  # keep it a weekday
            cls.today -= timedelta(days=1)
        cls.shift_day = EmployeeShiftDay.objects.filter(
            day=cls.today.strftime("%A").lower()
        ).first()
        schedule = EmployeeShiftSchedule.objects.create(
            day=cls.shift_day, shift_id=cls.shift, minimum_working_hour="08:00",
            start_time=time(8, 0), end_time=time(17, 0),
        )
        schedule.company_id.add(cls.company)

    def setUp(self):
        tag = uuid.uuid4().hex[:10]
        self.employee = Employee.objects.create(
            employee_first_name="Eod", employee_last_name=tag,
            email="eod%s@test.local" % tag, phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        info.company_id = self.company
        info.shift_id = self.shift
        info.work_type_id = self.work_type
        info.save()
        # Push is never attempted for real anywhere in this file.
        patcher = mock.patch("joydigi_api.push.send_to_user", return_value={"sent": 0})
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

    # ---------- helpers ----------

    def at(self, hour, minute=0, second=0, microsecond=0, date=None):
        return timezone.make_aware(
            datetime.datetime.combine(
                date or self.today, time(hour, minute, second, microsecond)
            )
        )

    def request_at(self, moment):
        user = type(self.employee.employee_user_id).objects.get(
            pk=self.employee.employee_user_id.pk
        )
        return Request(
            user=user, date=moment.date(), time=moment.time(), datetime=moment,
            trusted_device=True,
        )

    def check_in(self, moment):
        attendance, allowed, reason = perform_clock_in(self.request_at(moment))
        self.assertTrue(allowed, reason)
        return attendance

    def check_out(self, moment):
        return perform_clock_out(self.request_at(moment))

    def row(self, date=None):
        return Attendance.objects.get(
            employee_id=self.employee, attendance_date=date or self.today
        )

    def notifications(self, stage=None, date=None):
        qs = Notification.objects.filter(
            recipient=self.employee.employee_user_id,
            target_content_type=ContentType.objects.get_for_model(Attendance),
        )
        if stage:
            # Phase NOTIFICATION B2: the stored marker carries the work
            # date as well as the stage, so the four stages of a day all
            # share one marker format.
            qs = qs.filter(
                data__checkout_reminder=marker_for(stage, date or self.today)
            )
        return qs

    def approve_overtime(self, start, end):
        return OvertimeRequest.objects.create(
            employee_id=self.employee,
            request_date=self.today,
            start_time=start,
            end_time=end,
            approved=True,
            canceled=False,
        )


class ReminderScheduleTests(EndOfDayBaseTests):
    """The two reminders on an ordinary day."""

    def test_no_reminder_a_microsecond_before_the_first_moment(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(16, 54, 59, 999999))
        self.assertEqual(self.notifications().count(), 0)

    def test_the_first_reminder_goes_out_at_five_to_five(self):
        self.check_in(self.at(8, 0))
        tally = process_end_of_day(now=self.at(16, 55, 0))
        self.assertEqual(tally["first_reminder"], 1)
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)
        self.assertIn("chấm công ra", self.notifications().first().verb)

    def test_rerunning_does_not_send_the_first_reminder_twice(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(16, 55, 0))
        for minute in (56, 57, 58, 59):
            process_end_of_day(now=self.at(16, minute))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)
        self.assertEqual(self.push.call_count, 1)

    def test_no_second_reminder_a_microsecond_early(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(17, 4, 59, 999999))
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 0)

    def test_the_second_reminder_goes_out_at_five_past(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(16, 55))
        tally = process_end_of_day(now=self.at(17, 5, 0))
        self.assertEqual(tally["second_reminder"], 1)
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 1)

    def test_rerunning_does_not_send_the_second_reminder_twice(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(17, 5))
        for minute in (6, 7, 8, 9, 14):
            process_end_of_day(now=self.at(17, minute))
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 1)

    def test_checking_out_before_the_first_reminder_means_no_reminder_at_all(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 40))
        process_end_of_day(now=self.at(16, 55))
        process_end_of_day(now=self.at(17, 5))
        self.assertEqual(self.notifications().count(), 0)
        self.assertFalse(self.push.called)

    def test_checking_out_between_the_reminders_stops_the_second(self):
        # The stated case: reminded at 16:55, checked out at 17:02, so the
        # 17:05 reminder must not arrive.
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(16, 55))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

        self.check_out(self.at(17, 2))
        process_end_of_day(now=self.at(17, 5))
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 0)

    def test_no_reminder_for_someone_who_never_checked_in(self):
        process_end_of_day(now=self.at(16, 55))
        self.assertEqual(self.notifications().count(), 0)
        self.assertFalse(
            Attendance.objects.filter(employee_id=self.employee).exists()
        )

    def test_a_disabled_notification_preference_silences_both_channels(self):
        from joydigi_api.models import NotificationPreference

        NotificationPreference.objects.update_or_create(
            user=self.employee.employee_user_id,
            defaults={"all_notifications_enabled": False},
        )
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(16, 55))
        self.assertEqual(self.notifications().count(), 0)
        self.assertFalse(self.push.called)


class NoAutoCloseTests(EndOfDayBaseTests):
    """The session is never closed by the system."""

    def test_the_session_is_still_open_at_quarter_past(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(17, 15))
        self.assertIsNone(self.row().attendance_clock_out)

    def test_the_session_is_still_open_an_hour_later(self):
        self.check_in(self.at(8, 0))
        for moment in (self.at(17, 15), self.at(17, 30), self.at(18, 0)):
            process_end_of_day(now=moment)
        row = self.row()
        self.assertIsNone(row.attendance_clock_out)
        self.assertIsNone(row.attendance_clock_out_date)
        self.assertTrue(
            AttendanceActivity.objects.filter(
                employee_id=self.employee, clock_out__isnull=True
            ).exists()
        )

    def test_the_job_reports_no_closures(self):
        self.check_in(self.at(8, 0))
        tally = process_end_of_day(now=self.at(18, 0))
        self.assertNotIn("closed", tally)

    def test_no_worked_hours_or_overtime_are_invented(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(18, 0))
        row = self.row()
        self.assertIsNone(row.attendance_clock_out)
        self.assertEqual(row.attendance_overtime, "00:00")

    def test_no_early_out_is_recorded_by_the_job(self):
        row = self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(18, 0))
        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                attendance_id=row, type="early_out"
            ).exists()
        )

    def test_a_manual_check_out_after_the_reminders_still_works(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(17, 10))
        _a, allowed, reason = self.check_out(self.at(17, 40))
        self.assertTrue(allowed, reason)
        self.assertEqual(self.row().attendance_clock_out, time(17, 40))


class NextDayIsolationTests(EndOfDayBaseTests):
    """Yesterday's open session must not reach into today."""

    def fake_request(self):
        """
        A bare request object for `check_online`.

        It reads the current request out of thread locals and caches the
        answer on it; in a test there is no request, so one is supplied
        with no cached attribute, forcing a real query.
        """
        return mock.patch(
            "joydigi.joydigi_middlewares._thread_locals",
            mock.Mock(request=mock.Mock(spec=[])),
        )

    def setUp(self):
        super().setUp()
        # `check_online` asks about the real calendar, not the weekday the
        # rest of this file pins its fixtures to. On a Saturday those are
        # two different days, and an isolation test measured against the
        # wrong window would pass without proving anything.
        self.real_today = timezone.localdate()
        self.real_yesterday = self.real_today - timedelta(days=1)
        self.ensure_schedule(self.real_today)
        self.ensure_schedule(self.real_yesterday)

        # Phase FIX A.1B: automatic finalization only acts inside the
        # configured policy period, and does nothing at all without one.
        # These tests assert what happens to yesterday when the policy is
        # in force, so they declare one covering their fixture dates. The
        # boundary itself — and what happens to sessions *before* it — is
        # tested in `attendance.tests.test_finalization_policy_cutoff`.
        cutoff = override_settings(
            ATTENDANCE_FORGOTTEN_FINALIZATION_CUTOFF=(
                self.real_yesterday - timedelta(days=1)
            ).isoformat()
        )
        cutoff.enable()
        self.addCleanup(cutoff.disable)

    def ensure_schedule(self, date, shift=None, night=False):
        """A shift schedule for whatever weekday `date` falls on."""
        shift = shift or self.shift
        day = EmployeeShiftDay.objects.filter(
            day=date.strftime("%A").lower()
        ).first()
        schedule, created = EmployeeShiftSchedule.objects.get_or_create(
            day=day,
            shift_id=shift,
            defaults={
                "minimum_working_hour": "08:00",
                "start_time": time(22, 0) if night else time(8, 0),
                "end_time": time(6, 0) if night else time(17, 0),
                "is_night_shift": night,
            },
        )
        if created:
            schedule.company_id.add(self.company)
        return day, schedule

    def leave_yesterday_open(self):
        """A session checked in yesterday and never checked out."""
        row = self.check_in(self.at(8, 0, date=self.real_yesterday))
        return row, self.real_yesterday

    def test_yesterdays_open_session_does_not_block_todays_check_in(self):
        _row, _yesterday = self.leave_yesterday_open()

        # The mobile gate: `check_online` must not report this employee as
        # still working, or the API answers "Already clocked-in".
        employee = Employee.objects.get(pk=self.employee.pk)
        with self.fake_request():
            self.assertFalse(employee.check_online())

    def test_todays_check_in_creates_its_own_attendance(self):
        yesterday_row, yesterday = self.leave_yesterday_open()

        today_row = self.check_in(self.at(8, 0, date=self.real_today))

        self.assertNotEqual(today_row.pk, yesterday_row.pk)
        self.assertEqual(today_row.attendance_date, self.real_today)
        # Yesterday keeps its own date and its own record. Phase FIX A.1
        # additionally finalizes it at its configured shift end — the
        # earlier design left it open indefinitely, and the decision since
        # is that a forgotten day must not survive into a later workday.
        # What isolation means here is unchanged: today's row is a
        # separate row, and yesterday is closed against *yesterday*.
        yesterday_row.refresh_from_db()
        self.assertEqual(yesterday_row.attendance_date, yesterday)
        self.assertEqual(yesterday_row.attendance_clock_out, time(17, 0))
        self.assertEqual(yesterday_row.attendance_clock_out_date, yesterday)

    def test_todays_check_out_closes_todays_session_only(self):
        yesterday_row, _yesterday = self.leave_yesterday_open()
        self.check_in(self.at(8, 0, date=self.real_today))

        _a, allowed, reason = self.check_out(self.at(17, 0, date=self.real_today))
        self.assertTrue(allowed, reason)

        self.assertEqual(
            self.row(date=self.real_today).attendance_clock_out, time(17, 0)
        )
        # Yesterday was closed by the check-in above (Phase FIX A.1), at
        # its own shift end and against its own date — never by today's
        # check-out. That is what "today's session only" protects.
        yesterday_row.refresh_from_db()
        self.assertEqual(
            yesterday_row.attendance_clock_out_date, yesterday_row.attendance_date
        )
        self.assertNotEqual(
            yesterday_row.attendance_clock_out_date, self.real_today
        )

    def test_the_thirty_minute_rule_uses_todays_check_in_not_yesterdays(self):
        # Yesterday's session is many hours old. If the rule measured from
        # it, a check-out seconds after arriving today would be allowed.
        self.leave_yesterday_open()
        self.check_in(self.at(9, 0, date=self.real_today))

        _a, allowed, reason = self.check_out(self.at(9, 10, date=self.real_today))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_TOO_SOON")

    def test_yesterdays_session_is_never_altered_by_todays_activity(self):
        # Phase FIX A.1 renamed and narrowed this. Yesterday *is* now
        # closed — by the check-in below, at its own shift end. What must
        # still never happen is any of today's later activity reaching
        # back into it: the end-of-day pass and today's own check-out must
        # leave it exactly as finalization left it.
        yesterday_row, _yesterday = self.leave_yesterday_open()
        self.check_in(self.at(8, 0, date=self.real_today))

        yesterday_row.refresh_from_db()
        before = (
            yesterday_row.attendance_clock_out,
            yesterday_row.attendance_clock_out_date,
            yesterday_row.attendance_worked_hour,
            yesterday_row.attendance_overtime,
        )
        process_end_of_day(now=self.at(18, 0, date=self.real_today))
        self.check_out(self.at(18, 5, date=self.real_today))

        yesterday_row.refresh_from_db()
        self.assertEqual(
            (
                yesterday_row.attendance_clock_out,
                yesterday_row.attendance_clock_out_date,
                yesterday_row.attendance_worked_hour,
                yesterday_row.attendance_overtime,
            ),
            before,
        )

    def test_a_night_shift_still_counts_as_working_across_midnight(self):
        # The yesterday window exists for night shifts; narrowing it must
        # not take that away.
        night_shift = EmployeeShift.objects.create(employee_shift="Ca đêm")
        night_shift.company_id.add(self.company)
        yesterday = self.real_yesterday
        day, _schedule = self.ensure_schedule(yesterday, shift=night_shift, night=True)

        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        info.shift_id = night_shift
        info.save()

        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=yesterday,
            shift_id=night_shift,
            attendance_day=day,
            attendance_clock_in=time(22, 0),
            attendance_clock_in_date=yesterday,
            minimum_hour="08:00",
        )
        self.assertTrue(attendance.is_night_shift())

        employee = Employee.objects.get(pk=self.employee.pk)
        with self.fake_request():
            self.assertTrue(employee.check_online())

    def test_an_old_open_session_is_never_touched(self):
        old_date = self.real_today - timedelta(days=30)
        row = self.check_in(self.at(8, 0))
        Attendance.objects.filter(pk=row.pk).update(attendance_date=old_date)
        AttendanceActivity.objects.filter(employee_id=self.employee).update(
            attendance_date=old_date, clock_in_date=old_date
        )

        process_end_of_day(now=self.at(16, 55))

        row.refresh_from_db()
        self.assertIsNone(row.attendance_clock_out)
        self.assertEqual(self.notifications().count(), 0)


class ApprovedOvertimeReminderTests(EndOfDayBaseTests):
    """Approved overtime moves the reminders, and nothing else."""

    def test_the_reminders_follow_the_approved_overtime_end(self):
        self.check_in(self.at(8, 0))
        self.approve_overtime(time(17, 0), time(19, 0))

        # The ordinary 16:55 moment no longer applies.
        process_end_of_day(now=self.at(16, 55))
        self.assertEqual(self.notifications().count(), 0)

        process_end_of_day(now=self.at(18, 54, 59, 999999))
        self.assertEqual(self.notifications().count(), 0)

        process_end_of_day(now=self.at(18, 55))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

        process_end_of_day(now=self.at(19, 5))
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 1)

    def test_the_ordinary_end_reminders_are_not_sent_at_all_on_an_overtime_day(self):
        """Neither 16:55 nor 17:05 — the whole pair moves, not just one."""
        self.check_in(self.at(8, 0))
        self.approve_overtime(time(17, 0), time(19, 0))

        for moment in (self.at(16, 55), self.at(17, 5), self.at(17, 10)):
            process_end_of_day(now=moment)

        self.assertEqual(self.notifications().count(), 0)

    def test_checking_out_before_the_overtime_end_stops_the_post_end_reminder(self):
        self.check_in(self.at(8, 0))
        self.approve_overtime(time(17, 0), time(19, 0))
        process_end_of_day(now=self.at(18, 55))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

        self.check_out(self.at(19, 0))
        process_end_of_day(now=self.at(19, 5))

        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 0)

    def test_the_session_is_still_not_closed_on_an_overtime_day(self):
        self.check_in(self.at(8, 0))
        self.approve_overtime(time(17, 0), time(19, 0))
        process_end_of_day(now=self.at(19, 15))
        process_end_of_day(now=self.at(20, 0))
        self.assertIsNone(self.row().attendance_clock_out)

    def test_several_approved_windows_push_the_reminder_to_the_last_one(self):
        self.check_in(self.at(8, 0))
        self.approve_overtime(time(17, 0), time(18, 0))
        self.approve_overtime(time(18, 30), time(19, 30))

        process_end_of_day(now=self.at(19, 24))
        self.assertEqual(self.notifications().count(), 0)
        process_end_of_day(now=self.at(19, 25))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

    def test_a_cancelled_request_does_not_move_the_reminder(self):
        self.check_in(self.at(8, 0))
        request = self.approve_overtime(time(17, 0), time(19, 0))
        request.canceled = True
        request.save()
        process_end_of_day(now=self.at(16, 55))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

    def test_a_rejected_request_does_not_move_the_reminder(self):
        """`OvertimeRequest.request_status()` reads `canceled` as Rejected.

        This model has no separate rejected flag — the same column carries
        both — so one term excludes both states.
        """
        self.check_in(self.at(8, 0))
        request = self.approve_overtime(time(17, 0), time(19, 0))
        request.canceled = True
        request.save()
        # The same flag the model reports as a rejection, whatever
        # language the label is rendered in.
        self.assertTrue(request.canceled)

        process_end_of_day(now=self.at(16, 55))

        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

    def test_a_soft_deleted_request_does_not_move_the_reminder(self):
        """`is_active=False`, the filter the monthly summary already uses.

        A request that contributes nothing to anybody's hours must not
        move a reminder either.
        """
        self.check_in(self.at(8, 0))
        request = self.approve_overtime(time(17, 0), time(19, 0))
        request.is_active = False
        request.save()

        process_end_of_day(now=self.at(16, 55))

        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

    def test_an_unapproved_request_does_not_move_the_reminder(self):
        self.check_in(self.at(8, 0))
        OvertimeRequest.objects.create(
            employee_id=self.employee, request_date=self.today,
            start_time=time(17, 0), end_time=time(19, 0),
            approved=False, canceled=False,
        )
        process_end_of_day(now=self.at(16, 55))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

    def test_a_pending_request_is_not_credited_even_at_its_own_end(self):
        self.check_in(self.at(8, 0))
        OvertimeRequest.objects.create(
            employee_id=self.employee, request_date=self.today,
            start_time=time(17, 0), end_time=time(19, 0),
            approved=False, canceled=False,
        )

        process_end_of_day(now=self.at(18, 55))

        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 0)

    def test_the_overtime_request_is_never_modified(self):
        self.check_in(self.at(8, 0))
        request = self.approve_overtime(time(17, 0), time(19, 0))
        before = (request.approved, request.canceled, request.start_time,
                  request.end_time, request.request_date)
        process_end_of_day(now=self.at(19, 10))
        request.refresh_from_db()
        self.assertEqual(
            (request.approved, request.canceled, request.start_time,
             request.end_time, request.request_date),
            before,
        )

    def test_overtime_calculation_is_untouched_by_the_reminders(self):
        self.check_in(self.at(8, 0))
        self.approve_overtime(time(17, 0), time(19, 0))
        process_end_of_day(now=self.at(18, 55))
        process_end_of_day(now=self.at(19, 10))
        self.check_out(self.at(19, 0))
        row = self.row()
        # Exactly what a manual check-out at 19:00 produced before this
        # phase existed — the reminders changed nothing about the numbers.
        self.assertEqual(row.attendance_worked_hour, "10:00")
        self.assertEqual(row.attendance_overtime, "02:00")


class ExistingRulesUnchangedTests(EndOfDayBaseTests):
    """The rules this phase must not disturb."""

    def flags(self, row):
        return set(
            AttendanceLateComeEarlyOut.objects.filter(attendance_id=row)
            .values_list("type", flat=True)
        )

    def test_the_thirty_minute_manual_rule_still_applies(self):
        self.check_in(self.at(8, 0))
        _a, allowed, reason = self.check_out(self.at(8, 29, 59))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_TOO_SOON")
        _a, allowed, _r = self.check_out(self.at(8, 30, 0))
        self.assertTrue(allowed)

    def test_lateness_boundaries_are_unchanged(self):
        row = self.check_in(self.at(8, 30, 59))
        self.assertNotIn("late_come", self.flags(row))

    def test_the_next_minute_is_still_late(self):
        row = self.check_in(self.at(8, 31, 0))
        self.assertIn("late_come", self.flags(row))

    def test_early_out_boundaries_are_unchanged(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 29, 59))
        self.assertIn("early_out", self.flags(row))

    def test_leaving_at_half_past_four_is_still_not_early(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 30))
        self.assertNotIn("early_out", self.flags(row))

    def test_the_lunch_hour_is_still_excluded(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(17, 0))
        self.assertEqual(self.row().attendance_worked_hour, "08:00")

    def test_a_short_afternoon_still_excludes_lunch(self):
        self.check_in(self.at(11, 0))
        self.check_out(self.at(17, 0))
        self.assertEqual(self.row().attendance_worked_hour, "05:00")


class PostgresGuardTests(EndOfDayBaseTests):
    """The outage guard, extended to the scheduled path."""

    def test_the_job_takes_no_row_lock(self):
        """
        A job that walks many rows is exactly where somebody would reach
        for a lock, and PostgreSQL refuses FOR UPDATE with the manager's
        DISTINCT and with `Meta.ordering`'s outer join. SQLite ignores the
        call, so the call itself is what is watched.
        """
        self.check_in(self.at(8, 0))

        calls = []
        original = QuerySet.select_for_update

        def spy(self, *args, **kwargs):
            calls.append(self.model.__name__)
            return original(self, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            process_end_of_day(now=self.at(16, 55))
            process_end_of_day(now=self.at(17, 10))
            process_end_of_day(now=self.at(18, 0))

        self.assertEqual(calls, [], "the reminder job must take no row lock")


class EndOfDayTimezoneTests(EndOfDayBaseTests):
    """Deadlines are aware datetimes, not wall-clock arithmetic."""

    def test_the_effective_end_is_timezone_aware(self):
        end = effective_end_datetime(self.today, 17 * HOUR)
        self.assertTrue(timezone.is_aware(end))
        self.assertEqual(timezone.localtime(end).time(), time(17, 0))

    def test_a_run_expressed_in_utc_behaves_identically(self):
        self.check_in(self.at(8, 0))
        run_at = self.at(16, 55).astimezone(datetime.timezone.utc)
        process_end_of_day(now=run_at)
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)


class NightShiftEndReminderTests(EndOfDayBaseTests):
    """22:00-06:00 — the end reminders belong to the following morning.

    Phase NOTIFICATION B2. The end of a night shift is not on the date the
    session is filed under, so every assertion here is about the *next*
    calendar day, and the reminders must land at 05:55 and 06:05 rather
    than twelve hours early or not at all.
    """

    def setUp(self):
        super().setUp()
        self.session_date = timezone.localdate() - timedelta(days=1)
        self.night_shift = EmployeeShift.objects.create(employee_shift="Ca đêm B2")
        self.night_shift.company_id.add(self.company)
        self.night_day, schedule = self._night_schedule(self.session_date)
        self.schedule = schedule

        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        info.shift_id = self.night_shift
        info.save()

    def _night_schedule(self, date):
        day = EmployeeShiftDay.objects.filter(day=date.strftime("%A").lower()).first()
        schedule, created = EmployeeShiftSchedule.objects.get_or_create(
            day=day,
            shift_id=self.night_shift,
            defaults={
                "minimum_working_hour": "08:00",
                "start_time": time(22, 0),
                "end_time": time(6, 0),
                "is_night_shift": True,
            },
        )
        if created:
            schedule.company_id.add(self.company)
        return day, schedule

    def start_night(self):
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=self.session_date,
            shift_id=self.night_shift,
            attendance_day=self.night_day,
            attendance_clock_in=time(22, 0),
            attendance_clock_in_date=self.session_date,
            minimum_hour="08:00",
        )
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=self.session_date,
            clock_in_date=self.session_date,
            shift_day=self.night_day,
            clock_in=time(22, 0),
            in_datetime=timezone.make_aware(
                datetime.datetime.combine(self.session_date, time(22, 0))
            ),
        )
        return attendance

    def notifications_for_session(self, stage):
        return self.notifications(stage, date=self.session_date)

    def test_the_end_is_the_following_morning_not_the_session_date(self):
        attendance = self.start_night()

        end = effective_end_instant(attendance, self.schedule)

        self.assertEqual(timezone.localtime(end).time(), time(6, 0))
        self.assertEqual(
            timezone.localtime(end).date(), self.session_date + timedelta(days=1)
        )

    def test_the_first_reminder_is_at_five_to_six_the_next_morning(self):
        self.start_night()

        process_end_of_day(now=self.at(5, 54, 59, 999999))
        self.assertEqual(self.notifications().count(), 0)

        process_end_of_day(now=self.at(5, 55))

        self.assertEqual(
            self.notifications_for_session(STAGE_FIRST_REMINDER).count(), 1
        )

    def test_the_second_reminder_is_at_five_past_six(self):
        self.start_night()

        process_end_of_day(now=self.at(5, 55))
        process_end_of_day(now=self.at(6, 5))

        self.assertEqual(
            self.notifications_for_session(STAGE_SECOND_REMINDER).count(), 1
        )

    def test_checking_out_before_six_oh_five_stops_the_second_reminder(self):
        attendance = self.start_night()
        process_end_of_day(now=self.at(5, 55))

        Attendance.objects.filter(pk=attendance.pk).update(
            attendance_clock_out=time(6, 0),
            attendance_clock_out_date=self.session_date + timedelta(days=1),
        )
        AttendanceActivity.objects.filter(employee_id=self.employee).update(
            clock_out=time(6, 0), clock_out_date=self.session_date + timedelta(days=1)
        )

        process_end_of_day(now=self.at(6, 5))

        self.assertEqual(
            self.notifications_for_session(STAGE_SECOND_REMINDER).count(), 0
        )

    def test_the_night_session_is_never_closed_automatically(self):
        attendance = self.start_night()

        process_end_of_day(now=self.at(6, 5))
        process_end_of_day(now=self.at(7, 0))

        attendance.refresh_from_db()
        self.assertIsNone(attendance.attendance_clock_out)
        self.assertIsNone(attendance.attendance_clock_out_date)

    def test_overtime_on_the_following_date_does_not_extend_a_night_shift(self):
        """Documented limitation, not an oversight.

        A night session ends on the calendar day after the one it is
        filed under, and `OvertimeRequest` is a same-day window — so
        overtime continuing this shift could only be recorded on that
        following date. Nothing in this codebase establishes which
        requests on that date belong to the night that just ended rather
        than to the day that has just begun, and inventing an association
        rule here would be inventing business behaviour.

        So the reminders stay on the shift's own end. If that is the
        wrong answer for the business, the fix is a product decision
        about attribution, followed by a change here — not a guess.
        """
        attendance = self.start_night()
        OvertimeRequest.objects.create(
            employee_id=self.employee,
            request_date=self.session_date + timedelta(days=1),
            start_time=time(6, 0),
            end_time=time(8, 0),
            approved=True,
            canceled=False,
        )

        end = effective_end_instant(attendance, self.schedule)

        self.assertEqual(timezone.localtime(end).time(), time(6, 0))

        # And the ordinary night-shift reminders still arrive on time.
        process_end_of_day(now=self.at(5, 55))
        self.assertEqual(
            self.notifications_for_session(STAGE_FIRST_REMINDER).count(), 1
        )

    def test_unrelated_overtime_later_that_day_does_not_move_them(self):
        """An evening request on the following date changes nothing."""
        attendance = self.start_night()
        OvertimeRequest.objects.create(
            employee_id=self.employee,
            request_date=self.session_date + timedelta(days=1),
            start_time=time(18, 0),
            end_time=time(21, 0),
            approved=True,
            canceled=False,
        )

        end = effective_end_instant(attendance, self.schedule)

        self.assertEqual(timezone.localtime(end).time(), time(6, 0))

    def test_overtime_on_the_session_date_is_still_read(self):
        """The session's own date is consulted, for a night shift too.

        18:00-20:00 on the evening the shift began is earlier than the
        06:00 end, so it cannot extend anything — but it proves the
        lookup happens rather than being skipped for night shifts.
        """
        attendance = self.start_night()
        OvertimeRequest.objects.create(
            employee_id=self.employee,
            request_date=self.session_date,
            start_time=time(18, 0),
            end_time=time(20, 0),
            approved=True,
            canceled=False,
        )

        end = effective_end_instant(attendance, self.schedule)

        self.assertEqual(timezone.localtime(end).time(), time(6, 0))
        self.assertEqual(
            timezone.localtime(end).date(), self.session_date + timedelta(days=1)
        )
