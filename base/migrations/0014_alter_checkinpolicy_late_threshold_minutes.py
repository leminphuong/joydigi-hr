from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("base", "0013_announcement_is_pinned_and_more")]

    operations = [
        migrations.AlterField(
            model_name="checkinpolicy",
            name="late_threshold_minutes",
            field=models.PositiveSmallIntegerField(
                default=10, verbose_name="Ngưỡng đi muộn (phút)"
            ),
        )
    ]
