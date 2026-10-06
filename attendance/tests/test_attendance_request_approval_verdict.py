"""Approving a correction request must correct the verdict too.

Phase ATTENDANCE-STATUS-AND-ADMIN-DATA-HARDENING, section 2, through the real
HTTP view rather than the helper underneath it.

An employee whose check-in was recorded as 08:30 files a correction to 08:00.
The approval applied the new time with a queryset `.update()` and then called
`late_come`, which only ever creates — so the `late_come` row already on the row
survived, and the day went on telling the employee "Đi muộn" with an on-time
check-in printed beside it. Both approval paths now call `sync_late_early`,
which creates what the times imply and removes what they no longer do.
"""

import json
import uuid
from datetime import time, timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from attendance.models import Attendance, AttendanceLateComeEarlyOut
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
from joydigi_auth.models import JoydigiUser

PASSWORD = "approve-pass-123"


@override_settings(ATTENDANCE_LATE_EARLY_RULE_EFFECTIVE_DATE="2000-01-01")
class ApprovalReconcilesVerdictTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        tag = uuid.uuid4().hex[:6]
        cls.company = Company.objects.create(
            company="Approve Co %s" % tag,
            hq=True,
            address="x",
            country="VN",
            state="HN",
            city="HN",
            zip="10000",
        )
        CheckInPolicy.objects.create(company_id=cls.company, late_threshold_minutes=10)
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca %s" % tag)
        cls.shift.company_id.add(cls.company)
        cls.work_type = WorkType.objects.create(work_type="Office %s" % tag)
        cls.work_type.company_id.add(cls.company)
        Department.objects.create(department="Eng %s" % tag).company_id.add(cls.company)
        for name in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            EmployeeShiftSchedule.objects.create(
                day=EmployeeShiftDay.objects.filter(day=name).first(),
                shift_id=cls.shift,
                minimum_working_hour="08:00",
                start_time=time(8, 0),
                end_time=time(17, 0),
            ).company_id.add(cls.company)

        cls.monday = timezone.localdate()
        while cls.monday.weekday() != 0:
            cls.monday -= timedelta(days=1)

    def setUp(self):
        tag = uuid.uuid4().hex[:8]
        self.admin_user = JoydigiUser.objects.create_superuser(
            username="approver_%s" % tag,
            email="approver_%s@test.local" % tag,
            password=PASSWORD,
        )
        self.admin = self.make_employee("Approver", tag, user=self.admin_user)
        self.employee = self.make_employee("Worker", tag)
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            reporting_manager_id=self.admin
        )
        self.client.login(username=self.admin_user.username, password=PASSWORD)

    def make_employee(self, first, last, user=None):
        employee = Employee(
            employee_first_name=first,
            employee_last_name=last,
            email="%s.%s@test.local" % (first.lower(), last.lower()),
            phone="9999999999",
        )
        if user is not None:
            employee.employee_user_id = user
        employee.save()
        info = EmployeeWorkInformation.objects.get(employee_id=employee)
        info.company_id = self.company
        info.shift_id = self.shift
        info.work_type_id = self.work_type
        info.save()
        return Employee.objects.get(pk=employee.pk)

    def pending_correction(self, recorded, requested):
        """A day recorded at `recorded`, with a request to make it `requested`."""
        row = Attendance(
            employee_id=self.employee,
            attendance_date=self.monday,
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_day=EmployeeShiftDay.objects.filter(day="monday").first(),
            attendance_clock_in=recorded,
            attendance_clock_in_date=self.monday,
            attendance_clock_out=time(17, 0),
            attendance_clock_out_date=self.monday,
            minimum_hour="08:00",
            is_validate_request=True,
            request_type="update_request",
            requested_data=json.dumps(
                {"attendance_clock_in": requested.strftime("%H:%M:%S")}
            ),
        )
        row.save()
        flag = AttendanceLateComeEarlyOut(attendance_id=row, type="late_come")
        flag.employee_id = self.employee
        flag.save()
        return row

    def flags(self, row):
        return set(
            AttendanceLateComeEarlyOut.objects.entire()
            .filter(attendance_id=row)
            .values_list("type", flat=True)
        )

    def test_approving_an_on_time_correction_clears_the_late_flag(self):
        row = self.pending_correction(recorded=time(8, 30), requested=time(8, 0))
        self.assertEqual(self.flags(row), {"late_come"})

        response = self.client.post(
            reverse("approve-validate-attendance-request", args=[row.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertIn(response.status_code, (200, 302))

        row.refresh_from_db()
        self.assertEqual(
            row.attendance_clock_in,
            time(8, 0),
            "the approval should have applied the requested time",
        )
        self.assertEqual(
            self.flags(row),
            set(),
            "08:00 is on time, so the approved day must stop reporting a late "
            "arrival",
        )

    def test_approving_a_still_late_correction_keeps_one_flag(self):
        row = self.pending_correction(recorded=time(9, 30), requested=time(8, 45))

        response = self.client.post(
            reverse("approve-validate-attendance-request", args=[row.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertIn(response.status_code, (200, 302))

        row.refresh_from_db()
        self.assertEqual(row.attendance_clock_in, time(8, 45))
        self.assertEqual(self.flags(row), {"late_come"})
        self.assertEqual(
            AttendanceLateComeEarlyOut.objects.entire()
            .filter(attendance_id=row, type="late_come")
            .count(),
            1,
        )

    def test_approving_a_correction_that_makes_the_day_late_records_it(self):
        row = self.pending_correction(recorded=time(8, 0), requested=time(8, 40))
        AttendanceLateComeEarlyOut.objects.entire().filter(
            attendance_id=row
        ).delete()
        self.assertEqual(self.flags(row), set())

        response = self.client.post(
            reverse("approve-validate-attendance-request", args=[row.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertIn(response.status_code, (200, 302))

        row.refresh_from_db()
        self.assertEqual(self.flags(row), {"late_come"})

    def test_the_bulk_approval_reconciles_the_same_way(self):
        row = self.pending_correction(recorded=time(8, 30), requested=time(8, 0))

        response = self.client.post(
            reverse("bulk-approve-attendance-request"),
            {"ids": json.dumps([row.pk])},
            HTTP_HX_REQUEST="true",
        )
        self.assertIn(response.status_code, (200, 302))

        row.refresh_from_db()
        self.assertEqual(row.attendance_clock_in, time(8, 0))
        self.assertEqual(self.flags(row), set())
