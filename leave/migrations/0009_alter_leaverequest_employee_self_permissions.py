from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("leave", "0008_alter_leaverequestconditionapproval_options_and_more"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="leaverequest",
            options={
                "ordering": ["-id"],
                "permissions": (
                    ("can_view_on_leave", "Can View On Leave"),
                    ("view_own_leave_request", "Xem đơn của mình"),
                    ("add_own_leave_request", "Gửi đơn của mình"),
                    (
                        "change_own_leave_request",
                        "Sửa hoặc hủy đơn của mình",
                    ),
                ),
                "verbose_name": "Leave Request",
                "verbose_name_plural": "Leave Requests",
            },
        ),
    ]
