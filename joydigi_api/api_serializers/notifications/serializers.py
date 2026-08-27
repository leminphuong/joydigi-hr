from rest_framework import serializers

from notifications.models import Notification

from ...models import NotificationPreference


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "level", "unread", "verb", "timestamp", "deleted", "data"]


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    """Exposes only the toggle itself — `user` is never readable or
    settable through this serializer, so a client can never read or
    spoof another account's ownership (Phase UI-5C.1 Section 9)."""

    class Meta:
        model = NotificationPreference
        fields = ["all_notifications_enabled"]
