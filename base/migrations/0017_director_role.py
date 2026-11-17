"""The Director role (2026-11-17).

* Declares the marker permission ``base.director_role``.
* Creates ONE empty group carrying it: the Director role. Nobody is added
  to it; a superuser assigns it per company afterwards.

The role is recognised by the marker, never by its name (``base.roles``).
A group called "Giám đốc" that already exists (created by hand) is left
exactly as it is and does not become the Director role: the new group is
then named "Giám đốc (vai trò hệ thống)" instead.

Reversing removes the Director group only while nobody holds it.
"""

from django.db import migrations

MARKER_APP_LABEL = "base"
MARKER_MODEL = "companygroupassignment"
MARKER_CODENAME = "director_role"
MARKER_NAME = "Đánh dấu vai trò Giám đốc (hệ thống)"
NAMES = ("Giám đốc", "Giám đốc (vai trò hệ thống)")


def create_director_role(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    content_type, _ = ContentType.objects.get_or_create(
        app_label=MARKER_APP_LABEL, model=MARKER_MODEL
    )
    marker, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename=MARKER_CODENAME,
        defaults={"name": MARKER_NAME},
    )
    if Group.objects.filter(permissions=marker).exists():
        return  # already there (migration re-applied)
    name = next((n for n in NAMES if not Group.objects.filter(name=n).exists()), None)
    number = 2
    while name is None:
        candidate = f"{NAMES[1]} {number}"
        if not Group.objects.filter(name=candidate).exists():
            name = candidate
        number += 1
    group = Group.objects.create(name=name)
    group.permissions.add(marker)


def remove_empty_director_role(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    CompanyGroupAssignment = apps.get_model("base", "CompanyGroupAssignment")
    for group in Group.objects.filter(
        permissions__codename=MARKER_CODENAME,
        permissions__content_type__app_label=MARKER_APP_LABEL,
    ).distinct():
        if group.user_set.exists() or CompanyGroupAssignment.objects.filter(group=group).exists():
            continue  # somebody holds the role: never removed silently
        group.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("base", "0016_add_announcement_reaction"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="companygroupassignment",
            options={
                "permissions": [("director_role", "Đánh dấu vai trò Giám đốc (hệ thống)")],
                "verbose_name": "Company group assignment",
                "verbose_name_plural": "Company group assignments",
            },
        ),
        migrations.RunPython(create_director_role, remove_empty_director_role),
    ]
