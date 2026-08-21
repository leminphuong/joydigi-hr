from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0007_attendanceconflictresolution_attendancedailyhours_and_more"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="attendance",
            options={
                "ordering": [
                    "-attendance_date",
                    "employee_id__employee_first_name",
                    "attendance_clock_in",
                ],
                "permissions": [
                    ("change_validateattendance", "Validate Attendance"),
                    ("change_approveovertime", "Change Approve Overtime"),
                    ("clock_in_out", "Chấm công vào và ra"),
                    ("view_own_attendance", "Xem bảng công cá nhân"),
                ],
                "verbose_name": "Attendance",
                "verbose_name_plural": "Attendances",
            },
        ),
    ]
