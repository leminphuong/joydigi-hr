from django.db import migrations


STANDARD_ROLE_NAMES = ("Quản trị viên", "Trưởng nhóm", "Nhân viên")


def assign_default_employee_role(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    EmployeeWorkInformation = apps.get_model(
        "employee", "EmployeeWorkInformation"
    )
    CompanyGroupAssignment = apps.get_model("base", "CompanyGroupAssignment")

    groups = {
        name: Group.objects.get_or_create(name=name)[0] for name in STANDARD_ROLE_NAMES
    }
    employee_group = groups["Nhân viên"]

    work_infos = EmployeeWorkInformation.objects.select_related(
        "employee_id__employee_user_id", "company_id"
    ).filter(employee_id__is_active=True)
    for work_info in work_infos.iterator():
        employee = work_info.employee_id
        user = getattr(employee, "employee_user_id", None)
        if user is None:
            continue
        if user.is_superuser:
            continue

        company = work_info.company_id
        if company is None:
            if not user.groups.filter(name__in=STANDARD_ROLE_NAMES).exists():
                user.groups.add(employee_group)
            continue

        if CompanyGroupAssignment.objects.filter(
            user=user,
            company=company,
            group__name__in=STANDARD_ROLE_NAMES,
        ).exists():
            continue

        # Tài khoản quản lý cũ có thể chỉ lưu vai trò trong auth_group.
        # Không hạ vai trò của họ xuống Nhân viên khi nâng cấp.
        if user.groups.filter(name__in=STANDARD_ROLE_NAMES[:2]).exists():
            continue

        CompanyGroupAssignment.objects.get_or_create(
            user=user,
            company=company,
            group=employee_group,
        )
        user.groups.add(employee_group)


class Migration(migrations.Migration):
    dependencies = [
        ("base", "0015_alter_roster_department"),
        ("employee", "0006_employeeworkinformation_allow_remote_and_more"),
    ]

    operations = [
        migrations.RunPython(assign_default_employee_role, migrations.RunPython.noop),
    ]
