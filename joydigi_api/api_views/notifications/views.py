from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ...api_serializers.notifications.serializers import (
    NotificationPreferenceSerializer,
    NotificationSerializer,
)
from ...models import NotificationPreference, PushDeviceToken

# Create your views here.


class NotificationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, type):
        if type == "all":
            queryset = request.user.notifications.all()
        elif type == "unread":
            queryset = request.user.notifications.unread()

        pagination = PageNumberPagination()
        page = pagination.paginate_queryset(queryset, request)
        serializer = NotificationSerializer(page, many=True)
        return pagination.get_paginated_response(serializer.data)


class NotificationReadDelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, id):
        obj = request.user.notifications.filter(id=id).first()
        obj.mark_as_read()
        serializer = NotificationSerializer(obj)
        return Response(serializer.data, status=200)

    def delete(self, request, id):
        obj = request.user.notifications.filter(id=id).first()
        obj.deleted = True
        obj.save()
        return Response({"status": "deleted"}, status=200)


class NotificationBulkReadDelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        obj = request.user.notifications.all()
        obj.mark_all_as_read()
        return Response({"status": "marked as read"}, status=200)

    def delete(self, request):
        obj = request.user.notifications.all()
        obj.mark_all_as_deleted()
        return Response({"status": "deleted"}, status=200)


class NotificationBulkDelUnreadMessageView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        obj = request.user.notifications.unread()
        obj.mark_all_as_deleted()
        return Response({"status": "deleted"}, status=200)


class NotificationSettingsView(APIView):
    """Phase UI-5C.1: per-user in-app notification preference.

    Identity always comes from `request.user` — there is no path for a
    client to read or update another user's row (no user/employee/
    company id is ever accepted from the request body; the serializer
    doesn't even expose those fields). The row is created lazily on
    first access with `all_notifications_enabled=True`, so an existing
    user who has never touched this setting keeps receiving
    notifications exactly as before.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        preference, _ = NotificationPreference.objects.get_or_create(
            user=request.user
        )
        serializer = NotificationPreferenceSerializer(preference)
        return Response(serializer.data, status=200)

    def _update(self, request):
        preference, _ = NotificationPreference.objects.get_or_create(
            user=request.user
        )
        serializer = NotificationPreferenceSerializer(
            preference, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=200)

    def patch(self, request):
        return self._update(request)

    def put(self, request):
        return self._update(request)


class PushDeviceTokenView(APIView):
    """Register or retire this device's push token (Phase FCM-1).

    The owner is always `request.user`. There is deliberately no way to
    name an employee: a client that could register a token against
    somebody else's account would be able to receive their reminders.

    A token that turns up under a different user is *moved*, not
    duplicated. Firebase hands the same registration token to whoever
    signs in on that handset, so after a second person logs in, the row
    must follow them — otherwise their reminders would keep arriving in
    the previous owner's session.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = (request.data.get("token") or "").strip()
        platform = (request.data.get("platform") or "").strip().lower()
        device_id = (request.data.get("device_id") or "").strip()

        if not token:
            return Response({"message": "token is required"}, status=400)
        valid_platforms = dict(PushDeviceToken.PLATFORM_CHOICES)
        if platform not in valid_platforms:
            return Response(
                {"message": "platform must be one of %s" % ", ".join(valid_platforms)},
                status=400,
            )

        device, _created = PushDeviceToken.objects.update_or_create(
            token=token,
            defaults={
                "user": request.user,
                "platform": platform,
                "device_id": device_id,
                "is_active": True,
            },
        )
        return Response(
            {"message": "registered", "id": device.id, "platform": device.platform},
            status=200,
        )

    def delete(self, request):
        """
        Retire one device, on logout.

        Scoped to the caller's own tokens, and to the single token they
        name: logging out of a phone must not stop the same person's
        tablet from being reminded. Retiring a token that is already gone
        is not an error — logout should never fail over this.
        """
        token = (request.data.get("token") or "").strip()
        if not token:
            return Response({"message": "token is required"}, status=400)

        updated = PushDeviceToken.objects.filter(
            user=request.user, token=token
        ).update(is_active=False)
        return Response({"message": "unregistered", "deactivated": updated}, status=200)
