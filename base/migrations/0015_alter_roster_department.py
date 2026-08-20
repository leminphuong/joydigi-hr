import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("base", "0014_alter_checkinpolicy_late_threshold_minutes")]

    operations = [
        migrations.AlterField(
            model_name="roster",
            name="department",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="roster_entries",
                to="base.department",
                verbose_name="Department",
            ),
        ),
    ]
