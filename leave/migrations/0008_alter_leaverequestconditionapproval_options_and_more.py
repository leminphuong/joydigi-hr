import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("employee", "0006_employeeworkinformation_allow_remote_and_more"),
        ("leave", "0007_alter_historicalleaverequest_reject_reason_and_more"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="leaverequestconditionapproval",
            options={"ordering": ("sequence", "id")},
        ),
        migrations.AddField(
            model_name="leaverequestconditionapproval",
            name="acted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="leaverequestconditionapproval",
            name="acted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="leave_approval_actions",
                to="employee.employee",
            ),
        ),
    ]
