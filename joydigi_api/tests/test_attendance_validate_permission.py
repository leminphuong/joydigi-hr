"""Who may declare an attendance final.

Phase ATTENDANCE-STATUS-AND-ADMIN-DATA-HARDENING, sections 4 and 10.

`PUT /api/attendance/attendance-validate/<pk>` carried `IsAuthenticated` and
nothing else, over a bare `Attendance.objects.filter(id=pk).update(...)`. Any
logged-in employee could therefore validate any attendance row by guessing its
id — their own unfinished day, a colleague's, or one belonging to another
company. That matters twice over: validation is an administrative decision that
notifies the employee, and the timesheet treats a validated day as a complete
one, so a self-validated day painted itself "Đủ công".

The rule applied is the same one the web approval paths use
(`_can_review_attendance_request`): a check-in admin, or that employee's own
reporting manager, and never one's own record. These tests hold both the
refusals and the permission that must keep working.
"""

from datetime import time, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.models import Attendance
from base.models import EmployeeShift, EmployeeShiftDay, WorkType
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user


class ValidateAttendancePermissionTests(TestCase):
    def setUp(self):
        self.company = make_company("Validate Co")
        self.other_company = make_company("Other Co")
        self.shift = EmployeeShift.objects.create(employee_shift="Ca v")
        self.shift.company_id.add(self.company)
        self.work_type = WorkType.objects.create(work_type="Office v")
        self.work_type.company_id.add(self.company)

        self.owner_user = make_user("owner_user", password="x")
        self.owner = make_employee(
            company=self.company,
            email="owner_user@test.joydigi",
            user=self.owner_user,
            shift=self.shift,
            work_type=self.work_type,
        )
        self.manager_user = make_user("manager_user", password="x")
        self.manager = make_employee(
            company=self.company,
            email="manager_user@test.joydigi",
            user=self.manager_user,
            shift=self.shift,
            work_type=self.work_type,
        )
        self.stranger_user = make_user("stranger_user", password="x")
        self.stranger = make_employee(
            company=self.company,
            email="stranger_user@test.joydigi",
            user=self.stranger_user,
            shift=self.shift,
            work_type=self.work_type,
        )
        self.outsider_user = make_user("outsider_user", password="x")
        self.outsider = make_employee(
            company=self.other_company,
            email="outsider_user@test.joydigi",
            user=self.outsider_user,
        )

        EmployeeWorkInformation.objects.filter(employee_id=self.owner).update(
            reporting_manager_id=self.manager
        )

        day = timezone.localdate() - timedelta(days=1)
        self.attendance = Attendance(
            employee_id=self.owner,
            attendance_date=day,
            shift_id=self.shift,
            work_type_id=self.work_type,
            attendance_day=EmployeeShiftDay.objects.filter(
                day=day.strftime("%A").lower()
            ).first(),
            attendance_clock_in=time(8, 0),
            attendance_clock_in_date=day,
            minimum_hour="08:00",
        )
        self.attendance.save()

    def url(self, pk=None):
        return "/api/attendance/attendance-validate/%s" % (pk or self.attendance.pk)

    def put_as(self, user, pk=None):
        client = APIClient()
        client.force_authenticate(user=type(user).objects.get(pk=user.pk))
        return client.put(self.url(pk))

    def is_validated(self):
        return Attendance.objects.entire().get(pk=self.attendance.pk).attendance_validated

    # ------------------------------------------------------------- refusals
    def test_an_unauthenticated_caller_is_rejected(self):
        self.assertEqual(APIClient().put(self.url()).status_code, 401)
        self.assertFalse(self.is_validated())

    def test_an_unrelated_colleague_cannot_validate_someone_elses_day(self):
        response = self.put_as(self.stranger_user)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.is_validated())

    def test_the_employee_cannot_validate_their_own_day(self):
        response = self.put_as(self.owner_user)
        self.assertEqual(
            response.status_code,
            403,
            "self-validation would let anyone mark their own day complete",
        )
        self.assertFalse(self.is_validated())

    def test_an_employee_of_another_company_cannot_validate_it(self):
        response = self.put_as(self.outsider_user)
        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(self.is_validated())

    def test_an_unknown_id_is_not_found(self):
        response = self.put_as(self.manager_user, pk=99999999)
        self.assertEqual(response.status_code, 404)

    # ----------------------------------------------------------- permission
    def test_the_reporting_manager_can_still_validate(self):
        response = self.put_as(self.manager_user)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.is_validated())

    def test_a_superuser_can_still_validate(self):
        admin = make_user("admin_user", password="x", is_superuser=True)
        make_employee(
            company=self.company,
            email="admin_user@test.joydigi",
            user=admin,
        )
        response = self.put_as(admin)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(self.is_validated())

    def test_validating_changes_only_the_validated_flag(self):
        """Approving a day must not quietly edit the day.

        The endpoint now goes through `Attendance.save()` instead of a queryset
        `.update()`, so that the model's own bookkeeping runs rather than being
        skipped. What must not change is the recorded attendance itself, and
        that is what this pins — the times, the shift and the date come back
        exactly as they went in.
        """
        self.attendance.attendance_clock_out = time(17, 0)
        self.attendance.attendance_clock_out_date = self.attendance.attendance_date
        self.attendance.save()
        before = Attendance.objects.entire().values(
            "attendance_date",
            "attendance_clock_in",
            "attendance_clock_out",
            "shift_id",
            "minimum_hour",
        ).get(pk=self.attendance.pk)

        self.assertEqual(self.put_as(self.manager_user).status_code, 200)

        after = Attendance.objects.entire().values(
            "attendance_date",
            "attendance_clock_in",
            "attendance_clock_out",
            "shift_id",
            "minimum_hour",
        ).get(pk=self.attendance.pk)
        self.assertEqual(before, after)
        self.assertTrue(self.is_validated())
