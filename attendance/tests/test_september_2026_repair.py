from datetime import date, datetime, time
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceConflictResolution,
    AttendanceDailyHours,
    AttendanceExplanationRequest,
    OvertimeRequest,
    WorkRecords,
)
from attendance.views.summary import build_monthly_summary
from base.models import (
    Company,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    Holidays,
    Roster,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation
from leave.models import LeaveRequest, LeaveType


class September2026RepairTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            company="Repair Test Corp",
            hq=True,
            address="1 Test Street",
            country="VN",
            state="HCM",
            city="HCM",
            zip="70000",
        )
        cls.shift = EmployeeShift.objects.create(employee_shift="Office 08:00-17:00")
        cls.shift.company_id.add(cls.company)
        cls.work_type = WorkType.objects.create(work_type="Office")
        cls.work_type.company_id.add(cls.company)
        for day_name in ("monday", "tuesday", "wednesday", "thursday", "saturday"):
            shift_day = EmployeeShiftDay.objects.filter(day=day_name).first()
            EmployeeShiftSchedule.objects.create(
                day=shift_day,
                shift_id=cls.shift,
                minimum_working_hour="08:00",
                start_time=time(8, 0),
                end_time=time(17, 0),
            ).company_id.add(cls.company)

        cls.employee = Employee.objects.create(
            badge_id="NV005",
            employee_first_name="Test",
            employee_last_name="Employee",
            email="nv005@repair.test",
            phone="0900000005",
        )
        work_info = EmployeeWorkInformation.objects.get(employee_id=cls.employee)
        work_info.company_id = cls.company
        work_info.shift_id = cls.shift
        work_info.work_type_id = cls.work_type
        work_info.save()

    def make_employee(self, badge_id, email):
        employee = Employee.objects.create(
            badge_id=badge_id,
            employee_first_name="Leave",
            employee_last_name="Employee",
            email=email,
            phone="0900000006",
        )
        work_info = EmployeeWorkInformation.objects.get(employee_id=employee)
        work_info.company_id = self.company
        work_info.shift_id = self.shift
        work_info.work_type_id = self.work_type
        work_info.save()
        return employee

    def test_repairs_the_2027_national_day_holiday(self):
        """A wrong National Day row must not leave 01/09 as a working day."""
        holiday = Holidays.objects.create(
            name="Quoc khanh",
            start_date=date(2027, 9, 2),
            end_date=date(2027, 9, 2),
            is_specific=True,
            assigning_type="employee",
            company_id=self.company,
        )

        try:
            from attendance.methods.september_2026_repair import (
                repair_september_2026,
            )
        except ImportError:
            self.fail("The September 2026 attendance repair service is missing")

        repair_september_2026(apply=True)

        holiday.refresh_from_db()
        self.assertEqual(holiday.name, "Quốc khánh")
        self.assertEqual(holiday.start_date, date(2027, 9, 1))
        self.assertEqual(holiday.end_date, date(2027, 9, 2))
        self.assertFalse(holiday.is_specific)

    def test_does_not_overwrite_an_unrelated_holiday_on_the_same_dates(self):
        """Date overlap alone must never destroy another holiday's identity."""
        retreat = Holidays.objects.create(
            name="Company retreat",
            start_date=date(2027, 9, 1),
            end_date=date(2027, 9, 2),
            is_specific=True,
            assigning_type="employee",
            company_id=self.company,
        )
        retreat.employees.add(self.employee)

        from attendance.methods.september_2026_repair import repair_september_2026

        repair_september_2026(apply=True)

        retreat.refresh_from_db()
        self.assertEqual(retreat.name, "Company retreat")
        self.assertTrue(retreat.is_specific)
        self.assertEqual(list(retreat.employees.all()), [self.employee])
        self.assertTrue(
            Holidays.objects.entire().filter(
                name="Quốc khánh",
                start_date=date(2027, 9, 1),
                end_date=date(2027, 9, 2),
                is_specific=False,
                company_id=self.company,
            ).exists()
        )

    def test_does_not_move_a_same_alias_holiday_from_an_unrelated_date(self):
        """Another country's Independence Day must remain on its own date."""
        independence_day = Holidays.objects.create(
            name="Independence Day",
            start_date=date(2027, 7, 4),
            end_date=date(2027, 7, 4),
            is_specific=False,
            company_id=self.company,
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        repair_september_2026(apply=True)

        independence_day.refresh_from_db()
        self.assertEqual(independence_day.name, "Independence Day")
        self.assertEqual(independence_day.start_date, date(2027, 7, 4))
        self.assertTrue(
            Holidays.objects.entire().filter(
                name="Quốc khánh",
                start_date=date(2027, 9, 1),
                end_date=date(2027, 9, 2),
                company_id=self.company,
            ).exists()
        )

    def test_credits_a_full_day_without_manufacturing_clock_events(self):
        """System downtime must grant hours without pretending a punch occurred."""
        from attendance.methods.september_2026_repair import repair_september_2026

        repair_september_2026(apply=True)

        attendance_qs = Attendance.objects.entire().filter(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 3),
        )
        self.assertTrue(attendance_qs.exists(), "03/09 was not credited")
        attendance = attendance_qs.get()
        self.assertEqual(attendance.attendance_worked_hour, "08:00")
        self.assertEqual(attendance.minimum_hour, "08:00")
        self.assertIsNone(attendance.attendance_clock_in)
        self.assertIsNone(attendance.attendance_clock_out)
        self.assertTrue(attendance.attendance_validated)
        self.assertEqual(
            attendance.request_description,
            "Chưa có hệ thống chấm công",
        )

        override = AttendanceDailyHours.objects.entire().get(
            employee_id=self.employee,
            date=date(2026, 9, 3),
        )
        self.assertEqual(override.hours_second, 8 * 60 * 60)
        self.assertTrue(override.is_manually_edited)

        resolution = AttendanceConflictResolution.objects.entire().get(
            employee_id=self.employee,
            date=date(2026, 9, 3),
        )
        self.assertEqual(resolution.resolution, "full_present")
        self.assertEqual(resolution.conflict_type, "system_unavailable")

        work_record = WorkRecords.objects.entire().get(
            employee_id=self.employee,
            date=date(2026, 9, 3),
        )
        self.assertEqual(work_record.note, "Chưa có hệ thống chấm công")
        self.assertEqual(work_record.work_record_type, "FDP")

    def test_credits_online_days_but_not_employees_on_approved_leave(self):
        """15–16/09 count as remote work only for employees who were not off."""
        employee_on_leave = self.make_employee(
            "NV006",
            "nv006@repair.test",
        )
        leave_type = LeaveType.objects.create(
            name="Paid leave",
            payment="paid",
            company_id=self.company,
        )
        LeaveRequest.objects.create(
            employee_id=employee_on_leave,
            leave_type_id=leave_type,
            start_date=date(2026, 9, 15),
            end_date=date(2026, 9, 16),
            status="approved",
            description="Approved time off",
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        repair_september_2026(apply=True)

        online_rows = Attendance.objects.entire().filter(
            employee_id=self.employee,
            attendance_date__in=[date(2026, 9, 15), date(2026, 9, 16)],
            attendance_worked_hour="08:00",
            request_description="Làm online – không chấm công",
        )
        self.assertEqual(online_rows.count(), 2)
        self.assertFalse(
            Attendance.objects.entire()
            .filter(
                employee_id=employee_on_leave,
                attendance_date__in=[date(2026, 9, 15), date(2026, 9, 16)],
            )
            .exists()
        )

    def test_approves_requested_ot_on_05_and_12_without_attendance(self):
        """A filed Saturday OT request is sufficient when the clock was unavailable."""
        requested = OvertimeRequest.objects.create(
            employee_id=self.employee,
            request_date=date(2026, 9, 5),
            start_time=time(9, 0),
            end_time=time(17, 0),
            approved=False,
            canceled=False,
            description="Saturday OT",
        )
        outside_scope = OvertimeRequest.objects.create(
            employee_id=self.employee,
            request_date=date(2026, 9, 6),
            start_time=time(9, 0),
            end_time=time(12, 0),
            approved=False,
            canceled=False,
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        repair_september_2026(apply=True)

        requested.refresh_from_db()
        outside_scope.refresh_from_db()
        self.assertTrue(requested.approved)
        self.assertFalse(outside_scope.approved)
        self.assertFalse(
            Attendance.objects.entire()
            .filter(employee_id=self.employee, attendance_date=date(2026, 9, 5))
            .exists()
        )

        rows, _working_days, _totals = build_monthly_summary(
            date(2026, 9, 5),
            date(2026, 9, 5),
            Employee.objects.entire().filter(pk=self.employee.pk),
        )
        self.assertEqual(rows[0]["ot_week_off_seconds"], 7 * 60 * 60)

    def test_moves_the_complete_14_september_attendance_to_19_september(self):
        """Moving the incident day must carry its activities and HR overrides."""
        source = date(2026, 9, 14)
        destination = date(2026, 9, 19)
        monday = EmployeeShiftDay.objects.get(day="monday")
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=source,
            attendance_day=monday,
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_clock_in_date=source,
            attendance_clock_in=time(8, 7),
            attendance_worked_hour="00:00",
            minimum_hour="08:00",
            attendance_validated=False,
        )
        activity = AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=source,
            shift_day=monday,
            clock_in_date=source,
            clock_in=time(8, 7),
            in_datetime=timezone.make_aware(datetime(2026, 9, 14, 8, 7)),
        )
        AttendanceDailyHours.objects.create(
            employee_id=self.employee,
            date=source,
            hours_second=0,
            is_manually_edited=True,
        )
        AttendanceConflictResolution.objects.create(
            employee_id=self.employee,
            date=source,
            resolution="partial_hours",
            conflict_type="network_error",
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        repair_september_2026(apply=True)

        attendance.refresh_from_db()
        activity.refresh_from_db()
        self.assertEqual(attendance.attendance_date, destination)
        self.assertEqual(attendance.attendance_clock_in_date, destination)
        self.assertEqual(attendance.attendance_clock_in, time(8, 7))
        self.assertEqual(attendance.attendance_day.day, "saturday")
        self.assertEqual(activity.attendance_date, destination)
        self.assertEqual(activity.clock_in_date, destination)
        self.assertEqual(
            activity.in_datetime,
            timezone.make_aware(datetime(2026, 9, 19, 8, 7)),
        )
        self.assertFalse(
            WorkRecords.objects.entire()
            .filter(employee_id=self.employee, date=source)
            .exists()
        )
        self.assertTrue(
            WorkRecords.objects.entire()
            .filter(
                employee_id=self.employee,
                date=destination,
                attendance_id=attendance,
            )
            .exists()
        )
        self.assertTrue(
            AttendanceDailyHours.objects.entire()
            .filter(employee_id=self.employee, date=destination)
            .exists()
        )
        self.assertTrue(
            AttendanceConflictResolution.objects.entire()
            .filter(employee_id=self.employee, date=destination)
            .exists()
        )

    def test_lists_only_sub_five_hour_rows_without_a_permission_request(self):
        """The audit list must exclude short days that the employee explained."""
        monday = EmployeeShiftDay.objects.get(day="monday")
        tuesday = EmployeeShiftDay.objects.get(day="tuesday")
        unexcused = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 7),
            attendance_day=monday,
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_clock_in_date=date(2026, 9, 7),
            attendance_clock_in=time(8, 0),
            attendance_clock_out_date=date(2026, 9, 7),
            attendance_clock_out=time(12, 0),
            attendance_worked_hour="04:00",
            minimum_hour="08:00",
            attendance_validated=True,
        )
        explained = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 8),
            attendance_day=tuesday,
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_clock_in_date=date(2026, 9, 8),
            attendance_clock_in=time(8, 0),
            attendance_clock_out_date=date(2026, 9, 8),
            attendance_clock_out=time(12, 0),
            attendance_worked_hour="04:00",
            minimum_hour="08:00",
            attendance_validated=True,
        )
        AttendanceExplanationRequest.objects.create(
            employee_id=self.employee,
            request_type="other",
            request_date=date(2026, 9, 8),
            description="Reported a network outage",
            approved=False,
            canceled=False,
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        report = repair_september_2026(apply=True)

        listed_ids = {row["attendance_id"] for row in report.short_attendance}
        self.assertIn(unexcused.pk, listed_ids)
        self.assertNotIn(explained.pk, listed_ids)

    def test_management_command_is_a_dry_run_unless_apply_is_explicit(self):
        """Running the command for review must never change attendance data."""
        output = StringIO()
        try:
            call_command("repair_september_2026_attendance", stdout=output)
        except CommandError:
            self.fail("The September 2026 repair management command is missing")

        self.assertIn("DRY RUN", output.getvalue())
        self.assertFalse(
            Attendance.objects.entire()
            .filter(employee_id=self.employee, attendance_date=date(2026, 9, 3))
            .exists()
        )

    def test_dry_run_short_hours_omits_days_already_scheduled_for_repair(self):
        """The review list must not flag 14/09 when that row will move to 19/09."""
        source = date(2026, 9, 14)
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=source,
            attendance_day=EmployeeShiftDay.objects.get(day="monday"),
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_clock_in_date=source,
            attendance_clock_in=time(8, 7),
            attendance_worked_hour="00:00",
            minimum_hour="08:00",
            attendance_validated=False,
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        report = repair_september_2026(apply=False)

        self.assertNotIn(
            attendance.pk,
            {row["attendance_id"] for row in report.short_attendance},
        )
        attendance.refresh_from_db()
        self.assertEqual(attendance.attendance_date, source)

    def test_does_not_invent_eight_hours_when_an_employee_has_no_schedule(self):
        """Missing shift data must be reported, not guessed into payroll."""
        unscheduled = self.make_employee("NV007", "nv007@repair.test")
        work_info = EmployeeWorkInformation.objects.get(employee_id=unscheduled)
        work_info.shift_id = None
        work_info.save()

        from attendance.methods.september_2026_repair import repair_september_2026

        report = repair_september_2026(apply=True)

        self.assertFalse(
            Attendance.objects.entire()
            .filter(
                employee_id=unscheduled,
                attendance_date__in=[
                    date(2026, 9, 3),
                    date(2026, 9, 15),
                    date(2026, 9, 16),
                ],
            )
            .exists()
        )
        self.assertEqual(
            {row["badge_id"] for row in getattr(report, "schedule_conflicts", [])},
            {"NV007"},
            report,
        )

    def test_preserves_historical_metadata_and_larger_manual_hours(self):
        """Repairing a day must not replace its historical shift or reduce HR hours."""
        historical_shift = EmployeeShift.objects.create(employee_shift="Historic shift")
        historical_shift.company_id.add(self.company)
        historical_work_type = WorkType.objects.create(work_type="Historic remote")
        historical_work_type.company_id.add(self.company)
        thursday = EmployeeShiftDay.objects.get(day="thursday")
        EmployeeShiftSchedule.objects.create(
            day=thursday,
            shift_id=historical_shift,
            minimum_working_hour="06:00",
            start_time=time(9, 0),
            end_time=time(16, 0),
        ).company_id.add(self.company)
        attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 3),
            attendance_day=thursday,
            shift_id=historical_shift,
            work_type_id=historical_work_type,
            attendance_worked_hour="04:00",
            minimum_hour="06:00",
            attendance_validated=True,
        )
        daily = AttendanceDailyHours.objects.create(
            employee_id=self.employee,
            date=date(2026, 9, 3),
            hours_second=10 * 60 * 60,
            is_manually_edited=True,
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        repair_september_2026(apply=True)

        attendance.refresh_from_db()
        daily.refresh_from_db()
        self.assertEqual(attendance.shift_id, historical_shift)
        self.assertEqual(attendance.work_type_id, historical_work_type)
        self.assertEqual(attendance.attendance_day, thursday)
        self.assertEqual(attendance.minimum_hour, "06:00")
        self.assertEqual(attendance.attendance_worked_hour, "10:00")
        self.assertEqual(daily.hours_second, 10 * 60 * 60)

    def test_existing_destination_activity_blocks_move_and_source_is_audited(self):
        """A Sep19 activity collision must preserve Sep14 and keep it reviewable."""
        source = date(2026, 9, 14)
        destination = date(2026, 9, 19)
        monday = EmployeeShiftDay.objects.get(day="monday")
        saturday = EmployeeShiftDay.objects.get(day="saturday")
        source_attendance = Attendance.objects.create(
            employee_id=self.employee,
            attendance_date=source,
            attendance_day=monday,
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_clock_in_date=source,
            attendance_clock_in=time(8, 0),
            attendance_clock_out_date=source,
            attendance_clock_out=time(12, 0),
            attendance_worked_hour="04:00",
            minimum_hour="08:00",
            attendance_validated=True,
        )
        AttendanceActivity.objects.create(
            employee_id=self.employee,
            attendance_date=destination,
            shift_day=saturday,
            clock_in_date=destination,
            clock_in=time(9, 0),
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        report = repair_september_2026(apply=True)

        source_attendance.refresh_from_db()
        self.assertEqual(source_attendance.attendance_date, source)
        self.assertEqual(len(report.move_conflicts), 1)
        self.assertIn(
            source_attendance.pk,
            {row["attendance_id"] for row in report.short_attendance},
        )

    def test_historical_inactive_employee_remains_in_incident_scope(self):
        """Deactivation after September must not erase historical corrections."""
        employee = self.make_employee("NV008", "nv008@repair.test")
        employee.is_active = False
        employee.save(update_fields=["is_active"])
        overtime = OvertimeRequest.objects.create(
            employee_id=employee,
            request_date=date(2026, 9, 5),
            start_time=time(9, 0),
            end_time=time(12, 0),
            approved=False,
            canceled=False,
        )
        short = Attendance.objects.create(
            employee_id=employee,
            attendance_date=date(2026, 9, 7),
            attendance_day=EmployeeShiftDay.objects.get(day="monday"),
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_worked_hour="04:00",
            minimum_hour="08:00",
            attendance_validated=True,
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        report = repair_september_2026(apply=True)

        overtime.refresh_from_db()
        self.assertTrue(overtime.approved)
        self.assertEqual(
            Attendance.objects.entire().filter(
                employee_id=employee,
                attendance_date__in=[
                    date(2026, 9, 3),
                    date(2026, 9, 15),
                    date(2026, 9, 16),
                ],
            ).count(),
            3,
        )
        self.assertIn(
            short.pk,
            {row["attendance_id"] for row in report.short_attendance},
        )

    def test_rostered_day_off_is_not_credited(self):
        """An explicit roster OFF wins over a company-wide correction."""
        Roster.objects.create(
            employee=self.employee,
            date=date(2026, 9, 3),
            is_off=True,
            is_published=True,
        )

        from attendance.methods.september_2026_repair import repair_september_2026

        repair_september_2026(apply=True)

        self.assertFalse(
            Attendance.objects.entire().filter(
                employee_id=self.employee,
                attendance_date=date(2026, 9, 3),
            ).exists()
        )

    def test_second_apply_is_a_no_op(self):
        """A rerun must not rewrite payroll audit timestamps or history."""
        from attendance.methods.september_2026_repair import repair_september_2026

        first_report = repair_september_2026(apply=True)
        daily = AttendanceDailyHours.objects.entire().get(
            employee_id=self.employee,
            date=date(2026, 9, 3),
        )
        work_record = WorkRecords.objects.entire().get(
            employee_id=self.employee,
            date=date(2026, 9, 3),
        )
        first_daily_modified = daily.modified_at
        first_work_record_modified = work_record.last_update
        first_history_count = Attendance.objects.entire().get(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 3),
        ).history.count()

        second_report = repair_september_2026(apply=True)

        daily.refresh_from_db()
        work_record.refresh_from_db()
        second_history_count = Attendance.objects.entire().get(
            employee_id=self.employee,
            attendance_date=date(2026, 9, 3),
        ).history.count()
        self.assertEqual(first_report.full_days_credited, 3)
        self.assertEqual(second_report.full_days_credited, 0)
        self.assertEqual(second_report.full_days_unchanged, 3)
        self.assertEqual(daily.modified_at, first_daily_modified)
        self.assertEqual(work_record.last_update, first_work_record_modified)
        self.assertEqual(second_history_count, first_history_count)
