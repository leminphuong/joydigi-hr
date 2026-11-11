# 2026-11-11: one balance (fund) shared by two leave types.
#
# Adds a nullable column only: no existing row is read or written, every
# existing leave type keeps its own balance (NULL = no sharing). Sharing is
# switched on per type later, by `manage.py convert_shared_annual_balance`,
# after HR approval. Reversible: removing the column restores the schema.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0009_alter_leaverequest_employee_self_permissions"),
    ]

    operations = [
        migrations.AddField(
            model_name="leavetype",
            name="shared_balance_with",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="shared_balance_users",
                to="leave.leavetype",
                verbose_name="Dùng chung quỹ với loại phép",
            ),
        ),
    ]
