from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("employee", "0005_alter_employee_phone_and_more")]

    operations = [
        migrations.AddField(
            model_name="employeeworkinformation",
            name="allow_remote",
            field=models.BooleanField(
                default=False,
                help_text="Chỉ bật cho vị trí được công ty cho phép làm việc từ xa.",
                verbose_name="Được phép làm việc từ xa",
            ),
        ),
        migrations.AddField(
            model_name="historicalemployeeworkinformation",
            name="allow_remote",
            field=models.BooleanField(
                default=False,
                help_text="Chỉ bật cho vị trí được công ty cho phép làm việc từ xa.",
                verbose_name="Được phép làm việc từ xa",
            ),
        ),
    ]
