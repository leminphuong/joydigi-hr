"""``/api/okr/chat/`` (2026-11-09). Same conventions as the rest of the OKR
API: JWT or HR web session, ``{code, message, fields}`` errors in
Vietnamese, every rule enforced in ``okr.services.chat``.

Message history uses id cursors (``before`` / ``after``) instead of page
numbers, so polling only fetches what is new. See
``okr/docs/chat_api.md`` for the full contract.
"""

from __future__ import annotations

from django.http import FileResponse
from rest_framework import status
from rest_framework.response import Response

from okr.api.base import OKRAPIView
from okr.models import ChatRoomKind
from okr.services import chat
from okr.services.errors import OKRError

TARGET_FIELDS = {
    ChatRoomKind.OBJECTIVE: "objective_id",
    ChatRoomKind.KEY_RESULT: "key_result_id",
    ChatRoomKind.TEAM: "team_id",
    ChatRoomKind.DEPARTMENT: "department_id",
    ChatRoomKind.DIRECT: "employee_id",
}


# -- shapes -------------------------------------------------------------------------


def chat_person(employee):
    if employee is None:
        return None
    image = employee.get_image() if hasattr(employee, "get_image") else None
    return {"id": employee.pk, "name": employee.get_full_name(), "avatar": image or None}


def attachment(item) -> dict:
    return {
        "id": item.pk,
        "name": item.original_name,
        "size": item.size,
        "content_type": item.content_type,
        "is_image": item.is_image,
        "download_url": f"/api/okr/chat/attachments/{item.pk}/download/",
    }


def message(viewer, item, *, moderator: bool = False) -> dict:
    from django.utils import timezone

    recalled = item.is_recalled
    own = item.sender_id == viewer.employee_id
    reply = None
    if item.reply_to_id and item.reply_to is not None:
        original = item.reply_to
        reply = {
            "id": original.pk,
            "sender_name": original.sender.get_full_name() if original.sender else "",
            "excerpt": "" if original.is_recalled else original.content[:120],
            "recalled": original.is_recalled,
        }
    return {
        "id": item.pk,
        "room_id": item.room_id,
        # The sender's own retry key only: other people never see it.
        "client_message_id": item.client_message_id if own else None,
        "sender": chat_person(item.sender),
        "is_mine": own,
        "content": "" if recalled else item.content,
        "created_at": item.created_at,
        "edited_at": item.edited_at,
        "updated_at": item.updated_at,
        "recalled": recalled,
        "recalled_by_moderator": bool(recalled and item.recalled_by_id and item.recalled_by_id != item.sender_id),
        "reply_to": reply,
        "attachments": [] if recalled else [attachment(a) for a in item.attachments.all()],
        "mentions": []
        if recalled
        else [{"id": m.employee_id, "name": m.employee.get_full_name()} for m in item.mentions.all()],
        "permissions": {
            "edit": own and not recalled and timezone.now() - item.created_at <= chat.EDIT_WINDOW,
            "recall": not recalled and (own or moderator),
        },
    }


def _excerpt(item) -> str:
    if item.is_recalled:
        return "Tin nhắn đã được thu hồi"
    if item.content:
        return item.content[:80]
    return "Đã gửi tệp đính kèm"


def target(room) -> dict:
    data = {"type": room.kind}
    if room.kind == ChatRoomKind.OBJECTIVE and room.objective_id:
        data.update(objective_id=room.objective_id, owner=chat_person(room.objective.owner))
    elif room.kind == ChatRoomKind.KEY_RESULT and room.key_result_id:
        kr = room.key_result
        data.update(
            key_result_id=kr.pk,
            objective_id=kr.objective_id,
            objective_title=kr.objective.title,
            owner=chat_person(kr.owner),
        )
    elif room.kind == ChatRoomKind.TEAM and room.team_id:
        data.update(team_id=room.team_id, owner=chat_person(room.team.manager))
    elif room.kind == ChatRoomKind.DEPARTMENT and room.department_id:
        data.update(department_id=room.department_id)
    return data


def room(viewer, item, *, member=None, unread: int = 0, last=None, partner=None) -> dict:
    return {
        "id": item.pk,
        "kind": item.kind,
        "title": chat.room_title(item, partner),
        "target": target(item),
        "partner": chat_person(partner) if item.kind == ChatRoomKind.DIRECT else None,
        "unread": unread,
        "muted": bool(member.muted) if member is not None else False,
        "last_read_id": member.last_read_id if member is not None else 0,
        "last_message_at": item.last_message_at,
        "last_message": None
        if last is None
        else {
            "id": last.pk,
            "sender_name": last.sender.get_full_name() if last.sender else "",
            "excerpt": _excerpt(last),
            "created_at": last.created_at,
        },
        "permissions": {"post": chat.can_post(viewer, item), "moderate": chat.can_moderate(viewer, item)},
    }


def _list(data, key):
    if hasattr(data, "getlist"):
        values = data.getlist(key)
        return values if len(values) != 1 else values[0]
    return data.get(key)


# -- views --------------------------------------------------------------------------


class ChatAPIView(OKRAPIView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        chat.require_enabled(self.viewer)  # flag on, and in the pilot group if there is one

    def room_payload(self, item):
        from okr.models import OKRChatMember

        member = OKRChatMember.objects.filter(room=item, employee_id=self.viewer.employee_id).first()
        partner = chat.direct_partners(self.viewer, [item]).get(item.pk)
        unread = chat.unread_counts(self.viewer, [member]).get(item.pk, 0) if member else 0
        last = chat.last_messages([item.pk]).get(item.pk)
        return room(self.viewer, item, member=member, unread=unread, last=last, partner=partner)


class ChatRoomListView(ChatAPIView):
    def get(self, request):
        kind = request.query_params.get("kind") or ""
        if kind and kind not in ChatRoomKind.values:
            raise OKRError("validation_error", "Dữ liệu chưa hợp lệ.", 400, {"kind": "Giá trị không hợp lệ."})
        rows = chat.list_rooms(self.viewer, kind=kind, search=request.query_params.get("q") or "")
        results = [
            room(self.viewer, item, member=member, unread=unread, last=last, partner=partner)
            for item, member, unread, last, partner in rows
        ]
        return Response({"count": len(results), "results": results})

    def post(self, request):
        kind = str(request.data.get("kind") or "")
        field = TARGET_FIELDS.get(kind)
        if field is None:
            raise OKRError("validation_error", "Dữ liệu chưa hợp lệ.", 400, {"kind": "Loại cuộc trò chuyện không hợp lệ."})
        item = chat.open_room(self.viewer, kind=kind, target_id=self.body_int(field))
        return Response(self.room_payload(item))


class ChatRoomDetailView(ChatAPIView):
    def get(self, request, pk):
        return Response(self.room_payload(chat.get_room(self.viewer, pk)))


class ChatRoomMembersView(ChatAPIView):
    def get(self, request, pk):
        item = chat.get_room(self.viewer, pk)
        people = chat.room_members(self.viewer, item, search=request.query_params.get("q") or "")
        return Response({"count": len(people), "results": [chat_person(p) for p in people]})


class ChatRoomMessagesView(ChatAPIView):
    def get(self, request, pk):
        params = request.query_params
        result = chat.list_messages(
            self.viewer,
            pk,
            before=params.get("before"),
            after=params.get("after"),
            changed_since=params.get("changed_since"),
            search=params.get("q") or "",
        )
        moderator = chat.can_moderate(self.viewer, result["room"])
        return Response(
            {
                "room_id": result["room"].pk,
                "results": [message(self.viewer, m, moderator=moderator) for m in result["results"]],
                "changed": [message(self.viewer, m, moderator=moderator) for m in result["changed"]],
                "has_more_before": result["has_more_before"],
                "has_more_after": result["has_more_after"],
                "server_time": result["server_time"],
            }
        )

    def post(self, request, pk):
        data = request.data
        item, created = chat.send_message(
            self.viewer,
            pk,
            content=data.get("content") or "",
            client_message_id=data.get("client_message_id") or "",
            reply_to_id=data.get("reply_to"),
            attachment_ids=_list(data, "attachment_ids"),
            mention_ids=_list(data, "mention_ids"),
        )
        moderator = chat.can_moderate(self.viewer, item.room)
        return Response(
            message(self.viewer, item, moderator=moderator),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class ChatMessageDetailView(ChatAPIView):
    def patch(self, request, pk):
        item = chat.edit_message(self.viewer, pk, content=request.data.get("content") or "")
        return Response(message(self.viewer, item, moderator=chat.can_moderate(self.viewer, item.room)))

    def delete(self, request, pk):
        item = chat.recall_message(self.viewer, pk)
        return Response(message(self.viewer, item, moderator=chat.can_moderate(self.viewer, item.room)))


class ChatRoomReadView(ChatAPIView):
    def post(self, request, pk):
        last_read = chat.mark_read(self.viewer, pk, request.data.get("message_id"))
        return Response({"room_id": pk, "last_read_id": last_read, **chat.unread_total(self.viewer)})


class ChatRoomMuteView(ChatAPIView):
    def post(self, request, pk):
        raw = request.data.get("muted")
        muted = raw is True or str(raw).lower() in ("1", "true", "yes")
        member = chat.set_muted(self.viewer, pk, muted)
        return Response({"room_id": pk, "muted": member.muted})


class ChatAttachmentView(ChatAPIView):
    def post(self, request):
        item = chat.upload_attachment(self.viewer, self.body_int("room_id"), request.FILES.get("file"))
        return Response(attachment(item), status=status.HTTP_201_CREATED)


class ChatAttachmentDetailView(ChatAPIView):
    def delete(self, request, pk):
        """Drop one of the caller's own files not sent yet (cancelled upload)."""
        chat.delete_pending_attachment(self.viewer, pk)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChatAttachmentDownloadView(ChatAPIView):
    def get(self, request, pk):
        item = chat.attachment_for_download(self.viewer, pk)
        try:
            handle = item.file.open("rb")
        except (FileNotFoundError, OSError):
            raise OKRError("file_missing", "Không tìm thấy tệp.", 404)
        inline = item.is_image and request.query_params.get("inline") in ("1", "true")
        response = FileResponse(
            handle,
            as_attachment=not inline,
            filename=item.original_name,
            content_type=item.content_type or "application/octet-stream",
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


class ChatUnreadCountView(ChatAPIView):
    def get(self, request):
        return Response(chat.unread_total(self.viewer))


class ChatSearchView(ChatAPIView):
    def get(self, request):
        rows = chat.search_messages(self.viewer, request.query_params.get("q") or "")
        partners = chat.direct_partners(self.viewer, {m.room for m in rows})
        return Response(
            {
                "count": len(rows),
                "results": [
                    {
                        "room_id": m.room_id,
                        "room_kind": m.room.kind,
                        "room_title": chat.room_title(m.room, partners.get(m.room_id)),
                        "message": message(self.viewer, m),
                    }
                    for m in rows
                ],
            }
        )
