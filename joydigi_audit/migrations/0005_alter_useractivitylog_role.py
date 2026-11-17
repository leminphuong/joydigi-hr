"""The Director role in the activity log (2026-11-17): a new choice only, no
database change."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("joydigi_audit", "0004_useractivitylog"),
    ]

    operations = [
        migrations.AlterField(
            model_name="useractivitylog",
            name="role",
            field=models.CharField(
                choices=[
                    ("director", "Giám đốc"),
                    ("admin", "Quản trị viên"),
                    ("leader", "Trưởng nhóm"),
                    ("employee", "Nhân viên"),
                ],
                db_index=True,
                default="employee",
                max_length=20,
                verbose_name="Vai trò",
            ),
        ),
    ]
