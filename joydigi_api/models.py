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
