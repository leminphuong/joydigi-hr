"""OKR chat: rooms, messages, attachments, mentions and read state (2026-11-09).

Built on the JOYDIGI database, not the external chat service JOYOKR used.

Who may use a room is never stored: it is worked out from HR and OKR data on
every request (``okr.services.chat.can_access``), so a transfer, a
resignation or a locked account takes effect at once. ``OKRChatMember`` only
keeps per-person state: what they have read, whether they muted the room,
and when they were last pushed.
"""

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from okr.models.base import CompanyScopedModel, OKRQuerySet, TimeStampedModel
from okr.storage import chat_upload_to, evidence_storage


class ChatRoomKind(models.TextChoices):
    OBJECTIVE = "objective", _("Objective")
    KEY_RESULT = "key_result", _("Key result")
    TEAM = "team", _("Team")
    DEPARTMENT = "department", _("Department")
    DIRECT = "direct", _("Direct")


def direct_key(first_id: int, second_id: int) -> str:
    """The one key of the 1–1 room of two employees, whoever starts it."""
    low, high = sorted((int(first_id), int(second_id)))
    return f"{low}:{high}"


class OKRChatRoom(CompanyScopedModel, TimeStampedModel):
    kind = models.CharField(max_length=20, choices=ChatRoomKind.choices)
    objective = models.ForeignKey(
        "okr.OKRObjective", on_delete=models.CASCADE, null=True, blank=True, related_name="chat_rooms"
    )
    key_result = models.ForeignKey(
        "okr.OKRKeyResult", on_delete=models.CASCADE, null=True, blank=True, related_name="chat_rooms"
    )
    team = models.ForeignKey(
        "okr.OKRTeam", on_delete=models.CASCADE, null=True, blank=True, related_name="chat_rooms"
    )
    department = models.ForeignKey(
        "base.Department", on_delete=models.CASCADE, null=True, blank=True, related_name="okr_chat_rooms"
    )
    #: "<smaller employee id>:<larger employee id>" for a 1–1 room, else "".
    direct_key = models.CharField(max_length=64, blank=True, default="")
    last_message_at = models.DateTimeField(null=True, blank=True)

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_chat_room"
        verbose_name = _("OKR chat room")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "objective"], condition=Q(kind="objective"), name="okr_chat_room_objective_unique"
            ),
            models.UniqueConstraint(
                fields=["company", "key_result"], condition=Q(kind="key_result"), name="okr_chat_room_kr_unique"
            ),
            models.UniqueConstraint(
                fields=["company", "team"], condition=Q(kind="team"), name="okr_chat_room_team_unique"
            ),
            models.UniqueConstraint(
                fields=["company", "department"], condition=Q(kind="department"), name="okr_chat_room_dept_unique"
            ),
            models.UniqueConstraint(
                fields=["company", "direct_key"], condition=Q(kind="direct"), name="okr_chat_room_direct_unique"
            ),
            models.CheckConstraint(
                condition=(
                    Q(kind="objective", objective__isnull=False, key_result__isnull=True, team__isnull=True,
                      department__isnull=True, direct_key="")
                    | Q(kind="key_result", objective__isnull=True, key_result__isnull=False, team__isnull=True,
                        department__isnull=True, direct_key="")
                    | Q(kind="team", objective__isnull=True, key_result__isnull=True, team__isnull=False,
                        department__isnull=True, direct_key="")
                    | Q(kind="department", objective__isnull=True, key_result__isnull=True, team__isnull=True,
                        department__isnull=False, direct_key="")
                    | (Q(kind="direct", objective__isnull=True, key_result__isnull=True, team__isnull=True,
                         department__isnull=True) & ~Q(direct_key=""))
                ),
                name="okr_chat_room_target_matches_kind",
            ),
        ]
        indexes = [models.Index(fields=["company", "kind"], name="okr_chat_room_company_kind")]

    def company_references(self):
        yield "objective", self.objective.company_id if self.objective_id else None
        yield "key_result", self.key_result.company_id if self.key_result_id else None
        yield "team", self.team.company_id if self.team_id else None

    def department_references(self):
        yield "department", self.department

    def direct_participant_ids(self) -> tuple:
        if self.kind != ChatRoomKind.DIRECT or not self.direct_key:
            return ()
        return tuple(int(part) for part in self.direct_key.split(":"))


class OKRChatMember(CompanyScopedModel):
    """Per-person state in a room. Not a permission: see the module docstring."""

    room = models.ForeignKey(OKRChatRoom, on_delete=models.CASCADE, related_name="members")
    # Named ``employee`` + CASCADE: HR's account deletion removes it with them.
    employee = models.ForeignKey(
        "employee.Employee", on_delete=models.CASCADE, related_name="okr_chat_memberships"
    )
    joined_at = models.DateTimeField(default=timezone.now)
    #: Highest message id this person has read (0: none).
    last_read_id = models.BigIntegerField(default=0)
    last_read_at = models.DateTimeField(null=True, blank=True)
    muted = models.BooleanField(default=False)
    last_push_at = models.DateTimeField(null=True, blank=True)

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_chat_member"
        verbose_name = _("OKR chat member")
        constraints = [models.UniqueConstraint(fields=["room", "employee"], name="okr_chat_member_unique")]
        indexes = [models.Index(fields=["company", "employee"], name="okr_chat_member_employee")]

    def inherit_company(self):
        return self.room.company_id if self.room_id else None

    def company_references(self):
        yield "room", self.room.company_id if self.room_id else None


class OKRChatMessage(CompanyScopedModel):
    room = models.ForeignKey(OKRChatRoom, on_delete=models.CASCADE, related_name="messages")
    # Nullable + SET_NULL: HR can delete an employee without destroying a
    # conversation other people took part in.
    sender = models.ForeignKey(
        "employee.Employee", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    #: The sender's own idempotency key: the same send retried returns the
    #: first message instead of a duplicate.
    client_message_id = models.CharField(max_length=64)
    content = models.TextField(blank=True, default="")
    #: ``okr.services.search.fold(content)``: accent-insensitive search
    #: without any database extension.
    search_text = models.TextField(blank=True, default="")
    reply_to = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    recalled_at = models.DateTimeField(null=True, blank=True)
    recalled_by = models.ForeignKey(
        "employee.Employee", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_chat_message"
        verbose_name = _("OKR chat message")
        constraints = [
            models.UniqueConstraint(
                fields=["room", "sender", "client_message_id"], name="okr_chat_message_client_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["room", "id"], name="okr_chat_msg_room_id"),
            models.Index(fields=["room", "updated_at"], name="okr_chat_msg_room_updated"),
            models.Index(fields=["sender", "created_at"], name="okr_chat_msg_sender_created"),
        ]

    def inherit_company(self):
        return self.room.company_id if self.room_id else None

    def company_references(self):
        yield "room", self.room.company_id if self.room_id else None
        yield "reply_to", self.reply_to.company_id if self.reply_to_id else None

    @property
    def is_recalled(self) -> bool:
        return self.recalled_at is not None


class OKRChatAttachment(CompanyScopedModel):
    """A file sent in a room. Uploaded first (no message yet), then attached
    to the uploader's next message in the same room."""

    room = models.ForeignKey(OKRChatRoom, on_delete=models.CASCADE, related_name="attachments")
    message = models.ForeignKey(
        OKRChatMessage, on_delete=models.CASCADE, null=True, blank=True, related_name="attachments"
    )
    uploader = models.ForeignKey(
        "employee.Employee", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    file = models.FileField(storage=evidence_storage, upload_to=chat_upload_to, max_length=255)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120)
    size = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    is_image = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_chat_attachment"
        verbose_name = _("OKR chat attachment")
        indexes = [models.Index(fields=["uploader", "created_at"], name="okr_chat_att_uploader")]

    def inherit_company(self):
        return self.room.company_id if self.room_id else None

    def company_references(self):
        yield "room", self.room.company_id if self.room_id else None
        yield "message", self.message.company_id if self.message_id else None


class OKRChatMention(CompanyScopedModel):
    message = models.ForeignKey(OKRChatMessage, on_delete=models.CASCADE, related_name="mentions")
    employee = models.ForeignKey("employee.Employee", on_delete=models.CASCADE, related_name="okr_chat_mentions")

    objects = OKRQuerySet.as_manager()

    class Meta:
        db_table = "okr_chat_mention"
        verbose_name = _("OKR chat mention")
        constraints = [models.UniqueConstraint(fields=["message", "employee"], name="okr_chat_mention_unique")]

    def inherit_company(self):
        return self.message.company_id if self.message_id else None

    def company_references(self):
        yield "message", self.message.company_id if self.message_id else None
