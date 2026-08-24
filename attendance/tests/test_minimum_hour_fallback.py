"""Tests for the Phase 6.3A.1 fix: `Attendance.minimum_hour` reads
inside `Attendance.save()`/`update_attendance_overtime()` must never
crash on a missing value. `Attendance.minimum_hour` has a NOT NULL
constraint at the DB level (see `test_clock_out_api.py`'s companion
test for confirmation an ORM-level `None` insert raises
`IntegrityError` here) — so these tests exercise the defensive
fallback directly against in-memory/unsaved instances and the shared
helper, rather than trying to persist an impossible-locally NULL row.
"""

from django.test import TestCase

from attendance.models import Attendance, _normalized_minimum_hour


class NormalizedMinimumHourTests(TestCase):
    def test_returns_the_real_value_when_present(self):
        self.assertEqual(_normalized_minimum_hour("08:00", attendance_pk=1), "08:00")

    def test_falls_back_to_the_shift_schedule_default_for_none(self):
        self.assertEqual(_normalized_minimum_hour(None, attendance_pk=1), "08:15")

    def test_falls_back_to_the_shift_schedule_default_for_empty_string(self):
        self.assertEqual(_normalized_minimum_hour("", attendance_pk=1), "08:15")


class AttendanceUpdateOvertimeTests(TestCase):
    """`update_attendance_overtime()` only assigns attributes — no DB
    access — so it can be exercised directly on an unsaved instance."""

    def _unsaved_attendance(self, minimum_hour, attendance_worked_hour):
        attendance = Attendance()
        attendance.minimum_hour = minimum_hour
        attendance.attendance_worked_hour = attendance_worked_hour
        return attendance

    def test_normal_minimum_hour_unchanged(self):
        attendance = self._unsaved_attendance("08:00", "09:30")
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "01:30")

    def test_none_minimum_hour_never_raises_and_uses_the_fallback(self):
        attendance = self._unsaved_attendance(None, "08:15")
        attendance.update_attendance_overtime()  # must not raise
        # 08:15 worked == the 08:15 fallback -> no overtime, not the
        # entire day misreported as overtime (which "00:00" would do).
        self.assertEqual(attendance.attendance_overtime, "00:00")

    def test_empty_string_minimum_hour_uses_the_same_fallback(self):
        attendance = self._unsaved_attendance("", "08:15")
        attendance.update_attendance_overtime()
        self.assertEqual(attendance.attendance_overtime, "00:00")
