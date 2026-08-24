"""Tests for `overtime_calculation()`'s handling of a missing
`minimum_hour` (Phase 6.3A.1) — a real production `500` on checkout,
traced to `Attendance.minimum_hour` being `None` for a row written
outside the normal check-in path. `EmployeeShiftSchedule.minimum_working_hour`
is a non-nullable `CharField(default="08:15")`, so a `None`/empty value
on the copied `Attendance.minimum_hour` is a data-integrity gap, not a
legitimate "no minimum" configuration — the fix must fall back to that
model's own default, never to `"00:00"` (which would incorrectly count
an employee's entire worked day as overtime, as if it were a holiday)."""

from django.test import TestCase

from attendance.methods.utils import overtime_calculation


class _FakeAttendance:
    """Minimal stand-in — `overtime_calculation` only reads
    `.minimum_hour`, `.attendance_worked_hour`, and `.pk` (for the
    diagnostic log line)."""

    def __init__(self, minimum_hour, attendance_worked_hour, pk=1):
        self.minimum_hour = minimum_hour
        self.attendance_worked_hour = attendance_worked_hour
        self.pk = pk


class OvertimeCalculationTests(TestCase):
    # Case A — normal minimum_hour: unchanged behavior.
    def test_normal_minimum_hour_computes_overtime_as_before(self):
        attendance = _FakeAttendance(
            minimum_hour="08:00", attendance_worked_hour="09:30"
        )
        self.assertEqual(overtime_calculation(attendance), "01:30")

    def test_normal_minimum_hour_no_overtime_when_under(self):
        attendance = _FakeAttendance(
            minimum_hour="08:00", attendance_worked_hour="07:00"
        )
        self.assertEqual(overtime_calculation(attendance), "00:00")

    # Case B/D — a None/empty minimum_hour must never crash, and must
    # not silently treat the whole worked day as overtime.
    def test_none_minimum_hour_never_raises(self):
        attendance = _FakeAttendance(minimum_hour=None, attendance_worked_hour="00:30")
        overtime_calculation(attendance)  # must not raise AttributeError

    def test_none_minimum_hour_falls_back_to_shift_default_not_zero(self):
        # 08:15 worked == the EmployeeShiftSchedule model's own default
        # minimum ("08:15") -> no overtime, exactly what a normal,
        # correctly-configured day would compute. If the fallback were
        # "00:00" instead, this would incorrectly report 08:15 of
        # overtime.
        attendance = _FakeAttendance(
            minimum_hour=None, attendance_worked_hour="08:15"
        )
        self.assertEqual(overtime_calculation(attendance), "00:00")

    def test_empty_string_minimum_hour_falls_back_the_same_way(self):
        attendance = _FakeAttendance(minimum_hour="", attendance_worked_hour="08:15")
        self.assertEqual(overtime_calculation(attendance), "00:00")

    def test_none_minimum_hour_still_computes_real_overtime_past_the_fallback(self):
        attendance = _FakeAttendance(
            minimum_hour=None, attendance_worked_hour="10:15"
        )
        self.assertEqual(overtime_calculation(attendance), "02:00")
