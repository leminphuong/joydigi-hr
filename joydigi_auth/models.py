from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


class JoydigiUser(AbstractUser):
    is_new_employee = models.BooleanField(default=False)

    # Phase AUTH-6A.2: bumped by "Đăng xuất khỏi thiết bị" (admin force
    # logout) to invalidate every mobile access token issued before the
    # bump — see joydigi_api.auth.SessionVersionJWTAuthentication. A
    # normal login never changes this (no Single-Device-Login yet); it
    # only ever increases via the admin action. Existing production
    # rows get 0 via `default=0`, and a legacy token with no
    # `session_version` claim is treated as version 0 too, so a plain
    # deploy of this field never logs anyone out — see that module's
    # docstring for the full compatibility rule.
    session_version = models.PositiveIntegerField(default=0)

    class Meta:
        swappable = "AUTH_USER_MODEL"
        verbose_name = _("User")
        verbose_name_plural = _("Users")


class LegacyUser(models.Model):
    id = models.BigAutoField(primary_key=True)
    username = models.CharField(max_length=150)
    password = models.CharField(max_length=128)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_superuser = models.BooleanField(default=False)
    last_login = models.DateTimeField(blank=True, null=True)
    date_joined = models.DateTimeField(blank=True, null=True)

    class Meta:
        db_table = "auth_user"
        managed = False


class AuthUserGroups(models.Model):
    user = models.ForeignKey(LegacyUser, db_column="user_id", on_delete=models.CASCADE)
    group_id = models.IntegerField(db_column="group_id")

    class Meta:
        db_table = "auth_user_groups"
        managed = False


class AuthUserUserPermissions(models.Model):
    user = models.ForeignKey(LegacyUser, db_column="user_id", on_delete=models.CASCADE)
    permission_id = models.IntegerField(db_column="permission_id")

    class Meta:
        db_table = "auth_user_user_permissions"
        managed = False
