"""A step decided on its approver's behalf (2026-11-17).

Two new columns on ``leave_leaverequestconditionapproval``, both with a value
for every existing row (False / NULL): no existing step, leave or balance is
changed.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0011_leave_cancellation_request"),
    ]

    operations = [
        migrations.AddField(
            model_name="leaverequestconditionapproval",
            name="is_override",
            field=models.BooleanField(default=False, verbose_name="Duyệt thay"),
        ),
        migrations.AddField(
            model_name="leaverequestconditionapproval",
            name="override_reason",
            field=models.TextField(blank=True, null=True, verbose_name="Lý do duyệt thay"),
        ),
    ]
