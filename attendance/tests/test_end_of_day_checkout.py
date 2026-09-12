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
from django.test import TestCase
from django.utils import timezone

from attendance.methods.end_of_day import (
    REMINDER_BEFORE_END,
    SECOND_REMINDER_AFTER_END,
    STAGE_FIRST_REMINDER,
    STAGE_SECOND_REMINDER,
    effective_end_datetime,
    effective_end_seconds,
    process_end_of_day,
    stage_due,
)
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

    def test_the_first_reminder_stays_due_through_the_end_of_the_day(self):
        self.assertEqual(stage_due(self.end, self.end), STAGE_FIRST_REMINDER)
        self.assertEqual(
            stage_due(
                self.at(SECOND_REMINDER_AFTER_END - timedelta(microseconds=1)),
                self.end,
            ),
            STAGE_FIRST_REMINDER,
        )

    def test_the_second_reminder_is_due_exactly_ten_minutes_after(self):
        self.assertEqual(
            stage_due(self.at(SECOND_REMINDER_AFTER_END), self.end),
            STAGE_SECOND_REMINDER,
        )

    def test_the_second_reminder_is_the_last_word(self):
        # There is no third stage. Quarter past, an hour later, three hours
        # later — all still just "the second reminder is due", which the
        # dedupe then declines to send again.
        for later in (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=3)):
            self.assertEqual(
                stage_due(self.at(later), self.end),
                STAGE_SECOND_REMINDER,
                later,
            )

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

    def notifications(self, stage=None):
        qs = Notification.objects.filter(
            recipient=self.employee.employee_user_id,
            target_content_type=ContentType.objects.get_for_model(Attendance),
        )
        if stage:
            qs = qs.filter(data__checkout_reminder=stage)
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
        self.assertIn("chấm công ra về", self.notifications().first().verb)

    def test_rerunning_does_not_send_the_first_reminder_twice(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(16, 55, 0))
        for minute in (56, 57, 58, 59):
            process_end_of_day(now=self.at(16, minute))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)
        self.assertEqual(self.push.call_count, 1)

    def test_no_second_reminder_a_microsecond_early(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(17, 9, 59, 999999))
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 0)

    def test_the_second_reminder_goes_out_at_ten_past(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(16, 55))
        tally = process_end_of_day(now=self.at(17, 10, 0))
        self.assertEqual(tally["second_reminder"], 1)
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 1)

    def test_rerunning_does_not_send_the_second_reminder_twice(self):
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(17, 10))
        for minute in (11, 12, 13, 14, 30):
            process_end_of_day(now=self.at(17, minute))
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 1)

    def test_checking_out_before_the_first_reminder_means_no_reminder_at_all(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 40))
        process_end_of_day(now=self.at(16, 55))
        process_end_of_day(now=self.at(17, 10))
        self.assertEqual(self.notifications().count(), 0)
        self.assertFalse(self.push.called)

    def test_checking_out_between_the_reminders_stops_the_second(self):
        # The stated case: reminded at 16:55, checked out at 17:05, so the
        # 17:10 reminder must not arrive.
        self.check_in(self.at(8, 0))
        process_end_of_day(now=self.at(16, 55))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

        self.check_out(self.at(17, 5))
        process_end_of_day(now=self.at(17, 10))
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
        yesterday_row.refresh_from_db()
        self.assertEqual(yesterday_row.attendance_date, yesterday)
        self.assertIsNone(yesterday_row.attendance_clock_out)

    def test_todays_check_out_closes_todays_session_only(self):
        yesterday_row, _yesterday = self.leave_yesterday_open()
        self.check_in(self.at(8, 0, date=self.real_today))

        _a, allowed, reason = self.check_out(self.at(17, 0, date=self.real_today))
        self.assertTrue(allowed, reason)

        self.assertEqual(
            self.row(date=self.real_today).attendance_clock_out, time(17, 0)
        )
        yesterday_row.refresh_from_db()
        self.assertIsNone(yesterday_row.attendance_clock_out)

    def test_the_thirty_minute_rule_uses_todays_check_in_not_yesterdays(self):
        # Yesterday's session is many hours old. If the rule measured from
        # it, a check-out seconds after arriving today would be allowed.
        self.leave_yesterday_open()
        self.check_in(self.at(9, 0, date=self.real_today))

        _a, allowed, reason = self.check_out(self.at(9, 10, date=self.real_today))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_TOO_SOON")

    def test_yesterdays_session_is_never_closed_or_altered(self):
        yesterday_row, _yesterday = self.leave_yesterday_open()
        before = (
            yesterday_row.attendance_clock_out,
            yesterday_row.attendance_clock_out_date,
            yesterday_row.attendance_worked_hour,
            yesterday_row.attendance_overtime,
        )
        self.check_in(self.at(8, 0, date=self.real_today))
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

        process_end_of_day(now=self.at(19, 10))
        self.assertEqual(self.notifications(STAGE_SECOND_REMINDER).count(), 1)

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

    def test_an_unapproved_request_does_not_move_the_reminder(self):
        self.check_in(self.at(8, 0))
        OvertimeRequest.objects.create(
            employee_id=self.employee, request_date=self.today,
            start_time=time(17, 0), end_time=time(19, 0),
            approved=False, canceled=False,
        )
        process_end_of_day(now=self.at(16, 55))
        self.assertEqual(self.notifications(STAGE_FIRST_REMINDER).count(), 1)

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
