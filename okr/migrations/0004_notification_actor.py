# F7: who caused an OKR notification. Nullable: JOYOKR stored no actor, so
# migrated rows and system reminders keep none.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('employee', '0008_employeeface'),
        ('okr', '0003_role_events'),
    ]

    operations = [
        migrations.AddField(
            model_name='okrnotification',
            name='actor',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='employee.employee', verbose_name='Actor'),
        ),
    ]
