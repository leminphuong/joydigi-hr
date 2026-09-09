"""
Phase ATTENDANCE-CHECKOUT-FINAL-WORKTIME-2.

Covers the three rules introduced together, because they interact:

* the 12:00-13:00 lunch hour is unpaid and excluded from worked time;
* an employee must stay 30 minutes before the day can be closed;
* a day may be checked out twice, and the *last* check-out wins — the
  correction moves the existing mark instead of opening a second session.

The lunch arithmetic is exercised directly against the pure functions in
`attendance.methods.worktime` (no DB, no ambiguity about which row was
read), while everything about permission, ordering and the counter goes
through `perform_clock_out` — the single shared entry point web, mobile,
face and the scheduler all use, so proving it here proves it for all four.
"""

from datetime import date, datetime, time, timedelta
from unittest import mock

from django.db.models.query import QuerySet
from django.test import TestCase
from django.utils import timezone

from attendance.methods.utils import Request
from attendance.methods.worktime import (
    activities_worked_seconds,
    lunch_overlap_seconds,
    worked_seconds,
)
from attendance.models import Attendance, AttendanceActivity
from attendance.views.clock_in_out import perform_clock_out
from base.models import (
    Company,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation
from joydigi.joydigi_middlewares import set_selected_company

HOUR = 3600


def _dt(day, hour, minute=0, second=0):
    """Naive datetime, for the pure worked-time helpers."""
    return datetime.combine(day, time(hour, minute, second))


class LunchExclusionTests(TestCase):
    """
    The exact table the business specified. These are the numbers a payroll
    dispute would be settled with, so each is asserted on its own rather
    than looped — a failure names the case.
    """

    day = date(2026, 3, 2)

    def _worked(self, start, end):
        return worked_seconds(_dt(self.day, *start), _dt(self.day, *end))

    def test_full_day_spanning_lunch_is_eight_hours(self):
        self.assertEqual(self._worked((8, 0), (17, 0)), 8 * HOUR)

    def test_late_start_still_loses_the_whole_lunch_hour(self):
        self.assertEqual(self._worked((9, 0), (17, 0)), 7 * HOUR)

    def test_early_finish_still_loses_the_whole_lunch_hour(self):
        self.assertEqual(self._worked((8, 0), (16, 0)), 7 * HOUR)

    def test_morning_only_never_reaches_lunch(self):
        self.assertEqual(self._worked((8, 0), (11, 0)), 3 * HOUR)

    def test_partial_overlap_loses_only_the_overlapping_half_hour(self):
        # The case a flat "subtract one hour" would get wrong twice over:
        # it would wipe out the entire span and clamp at zero.
        self.assertEqual(self._worked((11, 30), (12, 30)), HOUR // 2)

    def test_a_span_entirely_inside_lunch_is_worth_nothing(self):
        self.assertEqual(self._worked((12, 0), (13, 0)), 0)

    def test_starting_mid_lunch_only_counts_from_thirteen(self):
        self.assertEqual(self._worked((12, 30), (17, 0)), 4 * HOUR)

    def test_starting_after_lunch_is_untouched(self):
        self.assertEqual(self._worked((13, 0), (17, 0)), 4 * HOUR)

    def test_the_shift_actually_configured_locally_comes_to_eight_hours(self):
        # Local `EmployeeShiftSchedule` is 08:30-17:30 against an 08:00
        # minimum. Before lunch exclusion that read as 9h and manufactured
        # an hour of overtime every single day.
        self.assertEqual(self._worked((8, 30), (17, 30)), 8 * HOUR)

    def test_a_reversed_span_contributes_nothing_rather_than_going_negative(self):
        self.assertEqual(self._worked((17, 0), (8, 0)), 0)

    def test_overlap_is_measured_not_assumed(self):
        self.assertEqual(
            lunch_overlap_seconds(_dt(self.day, 11, 30), _dt(self.day, 12, 30)),
            HOUR // 2,
        )
        self.assertEqual(
            lunch_overlap_seconds(_dt(self.day, 8, 0), _dt(self.day, 11, 0)), 0
        )

    def test_a_shift_crossing_midnight_loses_each_day_s_lunch(self):
        # Two calendar days spanned, so two lunch windows — a single
        # flat subtraction would under-deduct by an hour.
        start = _dt(self.day, 8, 0)
        end = _dt(self.day + timedelta(days=1), 17, 0)
        self.assertEqual(lunch_overlap_seconds(start, end), 2 * HOUR)


class CheckOutFlowTests(TestCase):
    """
    Everything routed through `perform_clock_out`, the shared entry point.

    Times are pinned explicitly on the request (the `Request` shim's
    `date`/`time`/`datetime`) instead of depending on when the suite runs,
    so 08:29:59 means 08:29:59 on every machine.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            company="Worktime Corp",
            hq=True,
            address="1 Test St",
            country="VN",
            state="HN",
            city="HN",
            zip="10000",
        )
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        cls.shift.company_id.add(cls.company)

        cls.work_type = WorkType.objects.create(work_type="Office")
        cls.work_type.company_id.add(cls.company)

        cls.dept = Department.objects.create(department="Engineering")
        cls.dept.company_id.add(cls.company)

        # The row must be dated today: a same-day correction is the only
        # kind allowed, so a fixed historical date could not exercise it.
        cls.today = timezone.localtime().date()
        cls.shift_day = EmployeeShiftDay.objects.filter(
            day=cls.today.strftime("%A").lower()
        ).first()

        for day in EmployeeShiftDay.objects.all():
            schedule = EmployeeShiftSchedule.objects.create(
                day=day,
                shift_id=cls.shift,
                minimum_working_hour="08:00",
                start_time=time(8, 0),
                end_time=time(17, 0),
            )
            schedule.company_id.add(cls.company)

        cls.employee = Employee.objects.create(
            employee_first_name="Worktime",
            employee_last_name="Tester",
            email="worktime@test.local",
            phone="9999999999",
        )
        EmployeeWorkInformation.objects.filter(employee_id=cls.employee).update(
            company_id_id=cls.company.pk,
            shift_id_id=cls.shift.pk,
            work_type_id_id=cls.work_type.pk,
        )
        cls.user = cls.employee.employee_user_id

    # ---------- helpers ----------

    def at(self, hour, minute=0, second=0, day=None):
        return timezone.make_aware(
            datetime.combine(day or self.today, time(hour, minute, second))
        )

    def check_in(self, moment, day=None):
        day = day or self.today
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=day,
            shift_day=self.shift_day,
            clock_in_date=day,
            clock_in=moment.time(),
            in_datetime=moment,
        )
        return Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=day,
            shift_id=self.shift,
            attendance_day=self.shift_day,
            attendance_clock_in_date=day,
            attendance_clock_in=moment.time(),
            minimum_hour="08:00",
        )

    def check_out(self, moment, system=False):
        return perform_clock_out(
            Request(
                user=self.user,
                date=moment.date(),
                time=moment.time(),
                datetime=moment,
                # Attendance-source verification (GPS/WiFi/QR) is a
                # different feature with its own tests; bypassing it keeps
                # these tests about check-out rules. Deliberately NOT
                # `system_checkout`, so the 30-minute lock still applies.
                trusted_device=True,
                system_checkout=system,
            )
        )

    def worked(self, attendance):
        attendance.refresh_from_db()
        return attendance.attendance_worked_hour

    # ---------- 30-minute lock ----------

    def test_checking_out_one_second_early_is_rejected(self):
        self.check_in(self.at(8, 0))
        attendance, allowed, reason = self.check_out(self.at(8, 29, 59))
        self.assertFalse(allowed)
        self.assertIsNone(attendance)
        self.assertEqual(reason["code"], "CHECKOUT_TOO_SOON")

    def test_checking_out_immediately_is_rejected(self):
        self.check_in(self.at(8, 0))
        _, allowed, reason = self.check_out(self.at(8, 0))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_TOO_SOON")

    def test_exactly_thirty_minutes_is_allowed(self):
        row = self.check_in(self.at(8, 0))
        attendance, allowed, reason = self.check_out(self.at(8, 30))
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        self.assertEqual(attendance.pk, row.pk)
        self.assertEqual(attendance.checkout_count, 1)

    def test_a_rejected_check_out_leaves_the_counter_untouched(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(8, 10))
        row.refresh_from_db()
        self.assertEqual(row.checkout_count, 0)
        # ...and leaves the day open, so a valid check-out still works.
        self.assertIsNone(row.attendance_clock_out_date)
        _, allowed, _reason = self.check_out(self.at(17, 0))
        self.assertTrue(allowed)

    def test_the_scheduler_is_exempt_from_the_thirty_minute_lock(self):
        # An employee who checks in at 17:15 against a 17:30 auto-punch-out
        # must still have their row closed, not left open forever.
        row = self.check_in(self.at(17, 15))
        attendance, allowed, _reason = self.check_out(self.at(17, 30), system=True)
        self.assertTrue(allowed)
        self.assertEqual(attendance.pk, row.pk)
        # It closes the day but does not consume the manual correction.
        self.assertEqual(attendance.checkout_count, 1)

    # ---------- worked time through the real flow ----------

    def test_first_check_out_records_worked_time_net_of_lunch(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        self.assertEqual(self.worked(row), "07:00")
        row.refresh_from_db()
        self.assertEqual(row.checkout_count, 1)

    def test_a_standard_day_is_eight_hours_and_produces_no_overtime(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(17, 0))
        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "08:00")
        # OT formula untouched: max(0, worked - minimum_hour), and worked
        # now equals the 08:00 minimum exactly.
        self.assertEqual(row.attendance_overtime, "00:00")
        self.assertEqual(row.overtime_second, 0)
        self.assertEqual(row.at_work_second, 8 * HOUR)

    def test_overtime_still_appears_when_the_day_genuinely_runs_long(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(18, 30))
        row.refresh_from_db()
        # 10h30 raw - 1h lunch = 9h30 worked; 1h30 over the 08:00 minimum.
        self.assertEqual(row.attendance_worked_hour, "09:30")
        self.assertEqual(row.attendance_overtime, "01:30")

    # ---------- second check-out: final wins ----------

    def test_the_second_check_out_replaces_the_first_and_does_not_stack(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        self.assertEqual(self.worked(row), "07:00")

        attendance, allowed, reason = self.check_out(self.at(17, 0))
        self.assertTrue(allowed)
        self.assertIsNone(reason)

        row.refresh_from_db()
        self.assertEqual(row.attendance_clock_out, time(17, 0))
        self.assertEqual(row.attendance_clock_out_date, self.today)
        self.assertEqual(row.checkout_count, 2)
        # 8h, i.e. 08:00-17:00 as one day. Not 15h (08-16 plus 08-17) and
        # not 9h (08-16 plus a phantom 16-17 session).
        self.assertEqual(row.attendance_worked_hour, "08:00")
        self.assertEqual(row.at_work_second, 8 * HOUR)

    def test_the_second_check_out_creates_no_new_attendance_row(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        before = Attendance.objects.filter(employee_id=self.employee).count()
        self.check_out(self.at(17, 0))
        self.assertEqual(
            Attendance.objects.filter(employee_id=self.employee).count(), before
        )

    def test_the_second_check_out_creates_no_new_activity_row(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        before = AttendanceActivity.objects.filter(employee_id=self.employee).count()
        self.check_out(self.at(17, 0))
        activities = AttendanceActivity.objects.filter(employee_id=self.employee)
        self.assertEqual(activities.count(), before)
        # The one existing session was moved, not duplicated.
        activity = activities.get()
        self.assertEqual(activity.clock_out, time(17, 0))
        self.assertEqual(activity.clock_in, time(8, 0))

    def test_the_monthly_worked_total_reflects_the_correction_not_the_sum(self):
        from attendance.models import AttendanceOverTime

        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        self.check_out(self.at(17, 0))
        row.refresh_from_db()
        account = AttendanceOverTime.objects.entire().get(
            employee_id=self.employee,
            month=self.today.strftime("%B").lower(),
            year=self.today.year,
        )
        # 8h, not 7h + 8h — `Attendance.save()` applies the delta.
        self.assertEqual(account.hour_account_second, 8 * HOUR)

    # ---------- second check-out: validation ----------

    def test_a_second_check_out_earlier_than_the_first_is_rejected(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        _, allowed, reason = self.check_out(self.at(15, 0))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_NOT_LATER")
        row.refresh_from_db()
        self.assertEqual(row.checkout_count, 1)
        self.assertEqual(row.attendance_clock_out, time(16, 0))

    def test_a_second_check_out_at_the_same_time_is_rejected(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        _, allowed, reason = self.check_out(self.at(16, 0))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_NOT_LATER")

    def test_the_second_check_out_need_not_wait_another_thirty_minutes(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        _, allowed, _reason = self.check_out(self.at(16, 1))
        self.assertTrue(allowed)

    def test_a_third_check_out_is_rejected(self):
        row = self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        self.check_out(self.at(17, 0))
        attendance, allowed, reason = self.check_out(self.at(18, 0))
        self.assertFalse(allowed)
        self.assertIsNone(attendance)
        self.assertEqual(reason["code"], "CHECKOUT_LIMIT_REACHED")
        row.refresh_from_db()
        self.assertEqual(row.checkout_count, 2)
        self.assertEqual(row.attendance_clock_out, time(17, 0))

    def test_the_scheduler_cannot_exceed_the_ceiling_either(self):
        self.check_in(self.at(8, 0))
        self.check_out(self.at(16, 0))
        self.check_out(self.at(17, 0))
        _, allowed, reason = self.check_out(self.at(18, 0), system=True)
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "CHECKOUT_LIMIT_REACHED")

    # ---------- historical safety ----------

    def test_a_finished_day_from_before_this_feature_cannot_be_reopened(self):
        # Exactly the shape the migration leaves behind: closed row,
        # checkout_count still at its 0 default. It must read as finished,
        # not as "one correction remaining".
        yesterday = self.today - timedelta(days=1)
        row = self.check_in(self.at(8, 0, day=yesterday), day=yesterday)
        row.attendance_clock_out = time(17, 0)
        row.attendance_clock_out_date = yesterday
        row.attendance_worked_hour = "09:00"
        row.save()
        self.assertEqual(row.checkout_count, 0)

        _, allowed, reason = self.check_out(self.at(18, 0))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "ALREADY_CLOCKED_OUT")

        row.refresh_from_db()
        # Untouched: the historical worked hours were not recalculated.
        self.assertEqual(row.attendance_worked_hour, "09:00")
        self.assertEqual(row.attendance_clock_out, time(17, 0))
        self.assertEqual(row.checkout_count, 0)

    def test_yesterday_s_correction_window_has_closed(self):
        yesterday = self.today - timedelta(days=1)
        row = self.check_in(self.at(8, 0, day=yesterday), day=yesterday)
        self.check_out(self.at(16, 0, day=yesterday))
        row.refresh_from_db()
        self.assertEqual(row.checkout_count, 1)

        # Same row, one correction still nominally available — but the day
        # is over. A correction is not a backfill mechanism.
        _, allowed, reason = self.check_out(self.at(17, 0))
        self.assertFalse(allowed)
        self.assertEqual(reason["code"], "ALREADY_CLOCKED_OUT")
        row.refresh_from_db()
        self.assertEqual(row.checkout_count, 1)

    def test_saving_an_old_row_does_not_recalculate_its_worked_hours(self):
        # Lunch exclusion lives on the check-out path, never in
        # `Attendance.save()` — so an HR user opening and re-saving an old
        # record must not silently rewrite it.
        old_day = self.today - timedelta(days=40)
        row = self.check_in(self.at(8, 0, day=old_day), day=old_day)
        row.attendance_clock_out = time(17, 0)
        row.attendance_clock_out_date = old_day
        row.attendance_worked_hour = "09:00"
        row.save()

        row.refresh_from_db()
        row.save()
        row.refresh_from_db()
        self.assertEqual(row.attendance_worked_hour, "09:00")

    # ---------- the success-with-nothing-written path ----------

    def test_a_check_out_with_no_attendance_at_all_reports_failure(self):
        attendance, allowed, reason = self.check_out(self.at(17, 0))
        self.assertFalse(allowed)
        self.assertIsNone(attendance)
        self.assertIsNotNone(reason)
        self.assertEqual(reason["code"], "NO_ACTIVE_ATTENDANCE")

    def test_an_open_row_whose_activity_is_missing_reports_failure(self):
        # `allowed=True` with `attendance=None` used to be reachable here,
        # and the mobile API turned it into HTTP 200 "Clocked-Out" with a
        # null id — success reported for a write that never happened.
        self.check_in(self.at(8, 0))
        AttendanceActivity.objects.filter(employee_id=self.employee).delete()
        attendance, allowed, reason = self.check_out(self.at(17, 0))
        self.assertFalse(allowed)
        self.assertIsNone(attendance)
        self.assertIsNotNone(reason)

    def test_no_outcome_ever_reports_success_without_an_attendance(self):
        # The invariant itself, over every reachable outcome above.
        cases = []

        cases.append(self.check_out(self.at(17, 0)))  # nothing checked in
        self.check_in(self.at(8, 0))
        cases.append(self.check_out(self.at(8, 5)))  # too soon
        cases.append(self.check_out(self.at(16, 0)))  # ok
        cases.append(self.check_out(self.at(15, 0)))  # not later
        cases.append(self.check_out(self.at(17, 0)))  # ok, correction
        cases.append(self.check_out(self.at(18, 0)))  # limit reached

        for attendance, allowed, _reason in cases:
            if allowed:
                self.assertIsNotNone(attendance)

    # ---------- concurrency ----------

    def test_a_replayed_check_out_cannot_push_the_count_past_two(self):
        # A double-tapped button or a retried mobile request replays the
        # same call. Each replay re-reads the locked row, so the third
        # sees count == 2 and is refused rather than writing a third mark.
        self.check_in(self.at(8, 0))
        outcomes = [
            self.check_out(self.at(16, 0)),
            self.check_out(self.at(16, 30)),
            self.check_out(self.at(17, 0)),
            self.check_out(self.at(17, 30)),
        ]
        row = Attendance.objects.get(employee_id=self.employee)
        self.assertEqual(row.checkout_count, 2)
        self.assertEqual([allowed for _a, allowed, _r in outcomes], [True, True, False, False])
        self.assertEqual(row.attendance_clock_out, time(16, 30))


class ActivityAggregationTests(TestCase):
    """`activities_worked_seconds` over the rows a real day produces."""

    class _Activity:
        def __init__(self, day, start, end):
            self.clock_in_date = day
            self.clock_in = time(*start)
            self.clock_out_date = day if end else None
            self.clock_out = time(*end) if end else None

    day = date(2026, 3, 2)

    def test_split_sessions_each_lose_only_their_own_lunch_overlap(self):
        activities = [
            self._Activity(self.day, (8, 0), (11, 30)),
            self._Activity(self.day, (12, 30), (17, 0)),
        ]
        # 3h30 (no overlap) + 4h00 (4h30 raw, less its own 12:30-13:00
        # overlap) = 7h30. Each session is charged only the lunch it
        # actually spans, and the 11:30-12:30 gap between them — never
        # worked — is not charged to anyone.
        self.assertEqual(activities_worked_seconds(activities), 7 * HOUR + 1800)

    def test_an_open_session_contributes_nothing(self):
        activities = [
            self._Activity(self.day, (8, 0), (11, 0)),
            self._Activity(self.day, (13, 0), None),
        ]
        self.assertEqual(activities_worked_seconds(activities), 3 * HOUR)


class CheckOutRowLockTests(TestCase):
    """
    The row lock taken during check-out must not carry a DISTINCT clause.

    `JoydigiCompanyManager` appends `.distinct()` whenever a company is
    selected — which `CompanyMiddleware` does on every request, API calls
    included. PostgreSQL rejects `SELECT DISTINCT ... FOR UPDATE` outright,
    so a lock taken through the scoped manager made every real check-out
    answer 500.

    SQLite drops `select_for_update` on the floor, so no amount of
    exercising the check-out flow locally can catch this. These tests
    inspect the query Django *builds* instead of what SQLite chooses to
    execute — the one thing that is identical on both backends.
    """

    def setUp(self):
        # A company must be selected for the manager to add DISTINCT at all,
        # which is the state every authenticated request actually runs in.
        set_selected_company("1")
        self.addCleanup(set_selected_company, None)

    def test_the_lock_queryset_still_asks_for_a_row_lock(self):
        # Dropping the lock would "fix" PostgreSQL by reintroducing the race
        # between two concurrent check-outs. It must stay.
        queryset = Attendance.objects.entire().select_for_update().filter(pk=1)
        self.assertTrue(queryset.query.select_for_update)

    def test_the_lock_queryset_is_not_distinct(self):
        queryset = Attendance.objects.entire().select_for_update().filter(pk=1)
        self.assertFalse(
            queryset.query.distinct,
            "the locking re-fetch must go through .entire(); a DISTINCT here "
            "makes PostgreSQL refuse the FOR UPDATE",
        )

    def test_the_scoped_manager_would_reintroduce_the_defect(self):
        # Guard, not a wish: this is exactly the query the fixed line used to
        # build. If someone drops `.entire()`, the assertion above starts
        # failing and this one explains why.
        queryset = Attendance.objects.select_for_update().filter(pk=1)
        self.assertTrue(
            queryset.query.distinct,
            "the company-scoped manager is expected to add DISTINCT — if it "
            "no longer does, .entire() may no longer be needed here",
        )

    def test_the_lock_taken_during_a_real_check_out_is_not_distinct(self):
        """
        The guard that actually catches a revert.

        The three assertions above describe the ORM contract; this one watches
        `perform_clock_out` itself. It records the state of every queryset the
        flow calls `select_for_update()` on — which is where the DISTINCT
        either is or isn't — so dropping `.entire()` from the production line
        fails here, on SQLite, without needing PostgreSQL to reject it.
        """
        company, shift, employee, attendance, moment = self._open_day_for_lock()

        distinct_flags = []
        original = QuerySet.select_for_update

        def spy(self, *args, **kwargs):
            distinct_flags.append(self.query.distinct)
            return original(self, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            _result, allowed, reason = perform_clock_out(
                self._request_for(employee, moment)
            )

        self.assertTrue(allowed, reason)
        self.assertEqual(
            len(distinct_flags), 1, "check-out should take exactly one row lock"
        )
        self.assertFalse(
            distinct_flags[0],
            "the queryset being locked carried DISTINCT — PostgreSQL refuses "
            "SELECT DISTINCT ... FOR UPDATE, so this is the production 500",
        )

    def _request_for(self, employee, moment):
        user = type(employee.employee_user_id).objects.get(
            pk=employee.employee_user_id.pk
        )
        return Request(
            user=user, date=moment.date(), time=moment.time(),
            datetime=moment, trusted_device=True,
        )

    def _open_day_for_lock(self):
        """A company-scoped employee with an open attendance day."""
        company = Company.objects.create(
            company="Lock Corp %s" % Company.objects.count(), hq=True,
            address="x", country="VN", state="HN", city="HN", zip="10000",
        )
        set_selected_company(str(company.pk))
        shift = EmployeeShift.objects.create(
            employee_shift="Lock Shift %s" % EmployeeShift.objects.count()
        )
        shift.company_id.add(company)
        work_type = WorkType.objects.create(
            work_type="Office %s" % WorkType.objects.count()
        )
        work_type.company_id.add(company)
        today = timezone.localtime().date()
        shift_day = EmployeeShiftDay.objects.filter(
            day=today.strftime("%A").lower()
        ).first()
        schedule = EmployeeShiftSchedule.objects.create(
            day=shift_day, shift_id=shift, minimum_working_hour="08:00",
            start_time=time(8, 0), end_time=time(17, 0),
        )
        schedule.company_id.add(company)

        import uuid

        tag = uuid.uuid4().hex[:10]
        employee = Employee.objects.create(
            employee_first_name="Lock", employee_last_name=tag,
            email="lock%s@test.local" % tag, phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=employee)
        info.company_id = company
        info.shift_id = shift
        info.work_type_id = work_type
        info.save()

        start = timezone.make_aware(datetime.combine(today, time(8, 0)))
        AttendanceActivity.objects.create(
            employee_id=employee, attendance_date=today, shift_day=shift_day,
            clock_in_date=today, clock_in=time(8, 0), in_datetime=start,
        )
        attendance = Attendance.objects.create(
            employee_id=employee, attendance_date=today, shift_id=shift,
            attendance_day=shift_day, attendance_clock_in_date=today,
            attendance_clock_in=time(8, 0), minimum_hour="08:00",
        )
        out = timezone.make_aware(datetime.combine(today, time(17, 0)))
        return company, shift, employee, attendance, out

    def test_the_flow_still_works_with_a_company_selected(self):
        # End-to-end under the company scoping a real request carries, which
        # no other check-out test exercises.
        company = Company.objects.create(
            company="Lock Corp", hq=True, address="x", country="VN",
            state="HN", city="HN", zip="10000",
        )
        set_selected_company(str(company.pk))
        shift = EmployeeShift.objects.create(employee_shift="Lock Shift")
        shift.company_id.add(company)
        work_type = WorkType.objects.create(work_type="Office")
        work_type.company_id.add(company)
        today = timezone.localtime().date()
        shift_day = EmployeeShiftDay.objects.filter(
            day=today.strftime("%A").lower()
        ).first()
        schedule = EmployeeShiftSchedule.objects.create(
            day=shift_day, shift_id=shift, minimum_working_hour="08:00",
            start_time=time(8, 0), end_time=time(17, 0),
        )
        schedule.company_id.add(company)

        employee = Employee.objects.create(
            employee_first_name="Lock", employee_last_name="Tester",
            email="lock@test.local", phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=employee)
        info.company_id = company
        info.shift_id = shift
        info.work_type_id = work_type
        info.save()

        def at(hour, minute=0):
            return timezone.make_aware(
                datetime.combine(today, time(hour, minute))
            )

        AttendanceActivity.objects.create(
            employee_id=employee, attendance_date=today, shift_day=shift_day,
            clock_in_date=today, clock_in=time(8, 0), in_datetime=at(8, 0),
        )
        attendance = Attendance.objects.create(
            employee_id=employee, attendance_date=today, shift_id=shift,
            attendance_day=shift_day, attendance_clock_in_date=today,
            attendance_clock_in=time(8, 0), minimum_hour="08:00",
        )

        user = type(employee.employee_user_id).objects.get(
            pk=employee.employee_user_id.pk
        )
        moment = at(17, 0)
        result, allowed, reason = perform_clock_out(
            Request(
                user=user, date=moment.date(), time=moment.time(),
                datetime=moment, trusted_device=True,
            )
        )
        self.assertTrue(allowed, reason)
        self.assertEqual(result.pk, attendance.pk)
        attendance.refresh_from_db()
        self.assertEqual(attendance.checkout_count, 1)
        self.assertEqual(attendance.attendance_worked_hour, "08:00")


class CheckOutCompanyIsolationTests(TestCase):
    """
    Using the unscoped manager for the lock must not become a way to reach
    another company's attendance.
    """

    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localtime().date()
        cls.shift_day = EmployeeShiftDay.objects.filter(
            day=cls.today.strftime("%A").lower()
        ).first()

    def _company(self, name, email):
        company = Company.objects.create(
            company=name, hq=True, address="x", country="VN",
            state="HN", city="HN", zip="10000",
        )
        shift = EmployeeShift.objects.create(employee_shift="Shift " + name)
        shift.company_id.add(company)
        work_type = WorkType.objects.create(work_type="WT " + name)
        work_type.company_id.add(company)
        schedule = EmployeeShiftSchedule.objects.create(
            day=self.shift_day, shift_id=shift, minimum_working_hour="08:00",
            start_time=time(8, 0), end_time=time(17, 0),
        )
        schedule.company_id.add(company)
        employee = Employee.objects.create(
            employee_first_name=name, employee_last_name="Emp",
            email=email, phone="9999999999",
        )
        info = EmployeeWorkInformation.objects.get(employee_id=employee)
        info.company_id = company
        info.shift_id = shift
        info.work_type_id = work_type
        info.save()
        return company, shift, employee

    def _open_day(self, employee, shift):
        moment = timezone.make_aware(
            datetime.combine(self.today, time(8, 0))
        )
        AttendanceActivity.objects.create(
            employee_id=employee, attendance_date=self.today,
            shift_day=self.shift_day, clock_in_date=self.today,
            clock_in=time(8, 0), in_datetime=moment,
        )
        return Attendance.objects.create(
            employee_id=employee, attendance_date=self.today, shift_id=shift,
            attendance_day=self.shift_day, attendance_clock_in_date=self.today,
            attendance_clock_in=time(8, 0), minimum_hour="08:00",
        )

    def test_one_company_cannot_check_out_anothers_attendance(self):
        company_a, shift_a, emp_a = self._company("Alpha", "alpha@test.local")
        _company_b, shift_b, emp_b = self._company("Beta", "beta@test.local")
        att_a = self._open_day(emp_a, shift_a)
        att_b = self._open_day(emp_b, shift_b)

        set_selected_company(str(company_a.pk))
        self.addCleanup(set_selected_company, None)

        user = type(emp_a.employee_user_id).objects.get(
            pk=emp_a.employee_user_id.pk
        )
        moment = timezone.make_aware(datetime.combine(self.today, time(17, 0)))
        result, allowed, _reason = perform_clock_out(
            Request(
                user=user, date=moment.date(), time=moment.time(),
                datetime=moment, trusted_device=True,
            )
        )

        # Alpha's own day closed...
        self.assertTrue(allowed)
        self.assertEqual(result.pk, att_a.pk)
        # ...and Beta's is untouched. The employee is server-derived, so the
        # unscoped lock can only ever reach the caller's own row.
        att_b.refresh_from_db()
        self.assertIsNone(att_b.attendance_clock_out)
        self.assertEqual(att_b.checkout_count, 0)
