"""
Phase 6.3A.4 — regression test for the real production `DataError`
(`StringDataRightTruncation: value too long for type character
varying(30)`) captured via the temporary CLOCK_OUT_DATA_ERROR
diagnostic at stage `attendance_post_save_signal:work_record_save`.

Root cause: `WorkRecords.message` was `CharField(max_length=30)` since
the very first migration, but `attendance_post_save`
(attendance/signals.py) assigns it a gettext-translated Vietnamese
status string. With `LANGUAGE_CODE = "vi"` (the project's actual
default — see joydigi/settings/base.py) and no `Accept-Language`
header (exactly what a plain API client sends), Django's
`LocaleMiddleware` activates Vietnamese for the request, and
`_("Incomplete half minimum hour")` resolves to "Chưa hoàn thành nửa
giờ tối thiểu" — 33 characters, 3 over the old 30-character limit.
That branch fires whenever an employee clocks out having worked less
than half of their required minimum hour — an ordinary, easily
reproduced case (e.g. a same-minute test checkout, or a genuinely
short shift), not an edge case.

This test exercises the real path end to end — the checkout API view,
`perform_clock_out`, `Attendance.save()`, and the `post_save` signal —
rather than calling `WorkRecords.save()` in isolation, and does not
force an artificial oversized string: the 33-character value is the
actual translated production string.
"""

from datetime import date, datetime

from django.test import TestCase
from django.utils.translation import gettext, override
from rest_framework.test import APIClient

from attendance.models import Attendance, WorkRecords
from attendance.views.clock_in_out import clock_in_attendance_and_activity
from base.models import EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user


class WorkRecordsMessageFieldSizeTests(TestCase):
    """
    Direct, DB-independent proof of the historical bug. SQLite (this
    project's local test DB) does not enforce `CharField.max_length` at
    the storage layer — Django only checks it via `full_clean()`, which
    `attendance_post_save`'s plain `work_record.save()` never calls —
    so the real `StringDataRightTruncation` this bug caused cannot be
    reproduced against SQLite; only real PostgreSQL enforces the
    column's `varchar(N)` limit at write time. That mismatch is exactly
    why this got past every previous local test run and only surfaced
    in production. These tests instead prove the underlying fact
    directly: the real translated value the signal assigns did not fit
    in the old 30-char column, and does fit in the corrected one.
    """

    def test_the_real_translated_value_exceeded_the_old_30_char_limit(self):
        with override("vi"):
            value = gettext("Incomplete half minimum hour")
        self.assertEqual(value, "Chưa hoàn thành nửa giờ tối thiểu")
        self.assertGreater(
            len(value),
            30,
            "this is the exact value attendance_post_save assigns to "
            "WorkRecords.message on the 'worked less than half the "
            "minimum hour' branch — it must exceed the OLD max_length "
            "for this regression test to mean anything",
        )

    def test_the_current_field_comfortably_fits_the_real_value(self):
        with override("vi"):
            value = gettext("Incomplete half minimum hour")
        max_length = WorkRecords._meta.get_field("message").max_length
        self.assertGreaterEqual(max_length, len(value))
        # The longest legitimate value found across every write site
        # (attendance/signals.py + leave/signals.py) — see the model
        # field's own comment.
        with override("vi"):
            longest = gettext("An approved leave exists")
        self.assertEqual(len(longest), 45)
        self.assertGreaterEqual(max_length, len(longest))


class ClockOutWorkRecordsMessageLengthTests(TestCase):
    def setUp(self):
        self.company = make_company("Message Length Co")
        self.user = make_user("msglenuser", password="secret123")
        self.employee = make_employee(
            company=self.company,
            email="msglen@test.joydigi",
            user=self.user,
        )
        self.shift = EmployeeShift.objects.create(employee_shift="Msg Len Shift")
        EmployeeWorkInformation.objects.filter(employee_id=self.employee).update(
            shift_id=self.shift
        )
        self.today = date.today()
        day_name = self.today.strftime("%A").lower()
        self.day = EmployeeShiftDay.objects.get(day=day_name)
        # A real 08:00 minimum, same as production's affected row —
        # half of it (04:00) is comfortably above the ~0-second worked
        # time this test produces, landing on the "ABS" / "Incomplete
        # half minimum hour" branch that overflowed.
        EmployeeShiftSchedule.objects.get_or_create(
            shift_id=self.shift,
            day=self.day,
            defaults={
                "is_night_shift": False,
                "minimum_working_hour": "08:00",
                "start_time": "08:00:00",
                "end_time": "17:00:00",
            },
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _clock_in(self):
        clock_in_attendance_and_activity(
            employee=self.employee,
            date_today=self.today,
            attendance_date=self.today,
            day=self.day,
            now="08:00",
            shift=self.shift,
            minimum_hour="08:00",
            start_time=0,
            end_time=1,
            in_datetime=datetime.now(),
        )

    def test_checkout_with_worked_time_under_half_minimum_succeeds(self):
        """
        The exact production scenario: clock-in then an almost-immediate
        clock-out (worked time far under half of the 08:00 minimum) must
        not raise a 500 — this used to raise
        `django.db.utils.DataError: value too long for type character
        varying(30)` from inside the post_save signal's WorkRecords
        write, without the API view's `transaction.atomic()` catching
        anything (there was no `except DataError` at the time this bug
        was live in production — this test proves the real fix: the
        column is now wide enough, not that the error got swallowed).
        """
        self._clock_in()

        response = self.client.post("/api/attendance/clock-out/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["message"], "Clocked-Out")

        attendance = Attendance.objects.get(
            employee_id=self.employee, attendance_date=self.today
        )
        self.assertIsNotNone(attendance.attendance_clock_out)

        work_record = WorkRecords.objects.get(
            employee_id=self.employee, date=self.today
        )
        # The real, untruncated Vietnamese status string — not a
        # generic placeholder. This is the exact value that used to
        # overflow `varchar(30)`.
        self.assertEqual(work_record.work_record_type, "ABS")
        self.assertEqual(len(work_record.message), 33)
        self.assertEqual(work_record.message, "Chưa hoàn thành nửa giờ tối thiểu")
