from django.conf import settings
from django.db import models


class NotificationPreference(models.Model):
    """Per-user in-app notification preference (Phase UI-5C.1).

    One row per JoydigiUser, created lazily (`get_or_create`) on first
    access rather than backfilled, so existing users keep receiving
    notifications exactly as before until they explicitly opt out —
    see `default=True` below. This only controls whether *future*
    routine notifications are generated; enforcement is a separate,
    deferred integration (see the phase's final report) and toggling
    this field never touches historical `notifications.Notification`
    rows.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )
    all_notifications_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return (
            f"NotificationPreference(user={self.user_id}, "
            f"enabled={self.all_notifications_enabled})"
        )


class PushDeviceToken(models.Model):
    """A device's FCM registration token, owned by the user who registered it.

    One row per device, not per user: someone with a phone and a tablet
    gets a reminder on both, so the token — not the user — is the unique
    thing here. A token that reappears under a different account is moved
    rather than duplicated; Firebase reuses a registration token when a
    second person signs in on the same handset, and sending that person's
    reminders to the previous owner's account would be a privacy leak.

    Rows are deactivated rather than deleted (`is_active`), so a device
    that logs out and back in reuses its row, and so Firebase telling us a
    token is dead leaves a record instead of a silent gap.
    """

    ANDROID = "android"
    IOS = "ios"
    PLATFORM_CHOICES = [(ANDROID, "Android"), (IOS, "iOS")]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_device_tokens",
    )
    token = models.TextField(unique=True)
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES)
    device_id = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["user", "is_active"])]

    def __str__(self):
        return (
            f"PushDeviceToken(user={self.user_id}, platform={self.platform}, "
            f"active={self.is_active})"
        )
