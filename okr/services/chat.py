"""OKR chat (2026-11-09): rooms per Objective, Key Result, Team, department
and 1–1, on the JOYDIGI database.

Access is decided here, from live HR and OKR data, on every call
(``can_access``). The rules, as approved on 2026-11-09:

* **Team room**: current members of an active OKR team, and its manager.
* **Department room** (company + HR department): active employees of that
  company whose work information is in the department, and the department's
  OKR manager (``OKRDepartmentRole``).
* **1–1 room**: exactly its two people, both in the same company. Nobody
  else, admins included. When one of them leaves, the other keeps the
  history read-only.
* **Objective / Key Result room**: the person must be able to *see* the OKR
  (``OKRViewer.can_view_scope``, so a private or draft OKR never leaks) and
  also belong to it:

  - owner of the objective or of the key result: always;
  - OKR admin of the company: always (they see every OKR already);
  - team scope: member or manager of that team;
  - department scope: in that department, or its OKR manager;
  - personal scope: same HR department as the owner, the owner's direct
    manager, or the OKR manager of the owner's department or of one of the
    owner's teams;
  - company scope: people with the OKR manager role.

* Admins do not read team, department or 1–1 rooms they are not part of.

Anything outside the viewer's company, or a room they may not use, is
reported as not found. ``OKRChatMember`` rows are state (read position,
mute, last push), never permission.

Push and in-app notifications go out after commit. The push runs in a
background thread through ``joydigi_api.push.send_to_user`` and never carries
message content (the lock screen shows who wrote, not what).

No WebSocket: clients poll ``list_messages(after=...)``. Every write goes
through this module, so a realtime transport can be added later without
changing the API.
"""

from __future__ import annotations

import datetime
import logging
import re
import threading
from typing import Iterable, Optional

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from okr.models import (
    ChatRoomKind,
    OKRChatAttachment,
    OKRChatMember,
    OKRChatMention,
    OKRChatMessage,
    OKRChatRoom,
    OKRDepartmentRole,
    OKRKeyResult,
    OKRNotification,
    OKRRole,
    OKRTeam,
    OKRTeamMember,
)
from okr.models.base import department_allows_company
from okr.models.chat import direct_key
from okr.models.choices import OKRRoleType, RecordStatus, ReferenceType, Scope
from okr.services import errors, notifications, okrs
from okr.services.access import OKRViewer, scope_of
from okr.services.chat_files import check_chat_upload
from okr.services.search import fold

logger = logging.getLogger(__name__)

MAX_CONTENT = 4000
EDIT_WINDOW = datetime.timedelta(hours=24)
MAX_ATTACHMENTS = 5
MAX_PENDING_ATTACHMENTS = 20
MAX_MENTIONS = 20
RATE_WINDOW = datetime.timedelta(seconds=60)
RATE_MESSAGES = 20
RATE_FILES = 10
PAGE = 30
MAX_AFTER = 100
MAX_CHANGED = 200
SEARCH_LIMIT = 50
MAX_ROOMS = 200
MEMBER_LIMIT = 50
PUSH_GAP = datetime.timedelta(minutes=5)
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

MENTION = "chat_mention"
REPLY = "chat_reply"
CHAT_NOTIFICATION_TYPES = (MENTION, REPLY)

KIND_LABELS = {
    ChatRoomKind.OBJECTIVE: "OKR",
    ChatRoomKind.KEY_RESULT: "OKR",
    ChatRoomKind.TEAM: "Team",
    ChatRoomKind.DEPARTMENT: "Phòng ban",
    ChatRoomKind.DIRECT: "1–1",
}

ROOM_RELATED = (
    "objective__cycle",
    "objective__owner",
    "key_result__objective__cycle",
    "key_result__objective__owner",
    "key_result__owner",
    "team__manager",
    "department",
)


# -- feature switch -------------------------------------------------------------


def enabled() -> bool:
    return bool(getattr(settings, "OKR_CHAT_ENABLED", False))


#: Stands for "a pilot list was given but none of it is a valid id": nobody.
_NOBODY = frozenset({0})


def pilot_employee_ids() -> frozenset:
    """``OKR_CHAT_PILOT_EMPLOYEE_IDS`` (2026-11-11): when set, chat is open to
    these employees only — a test group before everyone. Empty: everyone.

    Fails closed: an entry that is not a numeric employee id (a badge such as
    "NV006", a name, a typo) is ignored with a warning, and a list with no
    valid id at all opens chat to nobody — never to the whole company."""
    raw = getattr(settings, "OKR_CHAT_PILOT_EMPLOYEE_IDS", None) or ()
    if isinstance(raw, str):
        raw = raw.split(",")
    items = [str(item).strip() for item in raw if str(item).strip()]
    if not items:
        return frozenset()
    valid = frozenset(int(item) for item in items if item.isdigit() and int(item) > 0)
    invalid = [item for item in items if not (item.isdigit() and int(item) > 0)]
    if invalid:
        logger.warning("OKR_CHAT_PILOT_EMPLOYEE_IDS: ignored %d entr(ies) that are not employee ids", len(invalid))
    return valid or _NOBODY


def enabled_for(employee_id) -> bool:
    """Chat is on and, during a pilot, this employee is in the test group."""
    if not enabled():
        return False
    pilot = pilot_employee_ids()
    return not pilot or (employee_id is not None and int(employee_id) in pilot)


def require_enabled(viewer=None):
    if not enabled() or (viewer is not None and not enabled_for(viewer.employee_id)):
        raise errors.OKRError("chat_disabled", "Tính năng trao đổi chưa được bật.", 404)


def _room_not_found() -> errors.OKRError:
    return errors.OKRError("not_found", "Không tìm thấy cuộc trò chuyện.", 404)


def _message_not_found() -> errors.OKRError:
    return errors.OKRError("not_found", "Không tìm thấy tin nhắn.", 404)


# -- organisation facts (cached on the viewer for one request) ------------------


def _cache(viewer: OKRViewer) -> dict:
    return viewer.__dict__.setdefault("_chat_cache", {})


def _work(employee_id: int, company_id: int) -> tuple:
    """``(department id, reporting manager id)`` of an employee in a company."""
    from employee.models import EmployeeWorkInformation

    row = (
        EmployeeWorkInformation._base_manager.filter(employee_id=employee_id, company_id=company_id)
        .values_list("department_id", "reporting_manager_id")
        .first()
    )
    return row or (None, None)


def department_of(viewer: OKRViewer) -> Optional[int]:
    cache = _cache(viewer)
    if "department" not in cache:
        cache["department"] = _work(viewer.employee_id, viewer.company_id)[0]
    return cache["department"]


def _active_team_ids(company_id: int, employee_id: int) -> frozenset:
    return frozenset(
        OKRTeamMember.objects.for_company(company_id)
        .filter(employee_id=employee_id, team__status=RecordStatus.ACTIVE)
        .values_list("team_id", flat=True)
    )


def team_ids_of(viewer: OKRViewer) -> frozenset:
    cache = _cache(viewer)
    if "teams" not in cache:
        cache["teams"] = _active_team_ids(viewer.company_id, viewer.employee_id)
    return cache["teams"]


def _active_employees(company_id: int):
    from employee.models import Employee

    return Employee._base_manager.filter(
        is_active=True,
        employee_user_id__is_active=True,
        employee_work_info__company_id=company_id,
    )


# -- the access rules ------------------------------------------------------------


def okr_item(room: OKRChatRoom):
    """The live objective / key result of an OKR room, or None once deleted."""
    if room.kind == ChatRoomKind.OBJECTIVE:
        item = room.objective
        return item if item is not None and item.deleted_at is None else None
    if room.kind == ChatRoomKind.KEY_RESULT:
        item = room.key_result
        if item is None or item.deleted_at is not None or item.objective.deleted_at is not None:
            return None
        return item
    return None


def _objective_of(item):
    return item.objective if isinstance(item, OKRKeyResult) else item


def can_join_okr(viewer: OKRViewer, item) -> bool:
    """Whether the viewer may use the chat of this objective / key result."""
    ref = scope_of(item)
    if not viewer.can_view_scope(ref):
        return False
    objective = _objective_of(item)
    if viewer.employee_id in {ref.owner_id, objective.owner_id} - {None}:
        return True
    if viewer.is_admin:
        return True
    scope = objective.scope
    if scope == Scope.TEAM:
        return bool(objective.team_id) and (
            objective.team_id in team_ids_of(viewer) or objective.team_id in viewer.managed_team_ids
        )
    if scope == Scope.DEPARTMENT:
        department = objective.department_id
        return bool(department) and (
            department == department_of(viewer) or department in viewer.managed_department_ids
        )
    if scope == Scope.PERSONAL:
        owner_id = objective.owner_id
        if not owner_id:
            return False
        owner_department, owner_manager = _work(owner_id, viewer.company_id)
        if owner_manager == viewer.employee_id:
            return True
        if owner_department and (
            owner_department == department_of(viewer) or owner_department in viewer.managed_department_ids
        ):
            return True
        return bool(_active_team_ids(viewer.company_id, owner_id) & viewer.managed_team_ids)
    if scope == Scope.COMPANY:
        return viewer.is_manager
    return False


def can_access(viewer: Optional[OKRViewer], room: OKRChatRoom) -> bool:
    """The one permission check every chat operation goes through."""
    if viewer is None or not viewer.is_active or room.company_id != viewer.company_id:
        return False
    kind = room.kind
    if kind == ChatRoomKind.TEAM:
        team = room.team
        if team is None or team.status != RecordStatus.ACTIVE:
            return False
        return room.team_id in team_ids_of(viewer) or room.team_id in viewer.managed_team_ids
    if kind == ChatRoomKind.DEPARTMENT:
        department = room.department
        if department is None or not getattr(department, "is_active", True):
            return False
        return room.department_id == department_of(viewer) or room.department_id in viewer.managed_department_ids
    if kind == ChatRoomKind.DIRECT:
        return viewer.employee_id in room.direct_participant_ids()
    item = okr_item(room)
    return item is not None and can_join_okr(viewer, item)


def direct_partner_active(room: OKRChatRoom, viewer: OKRViewer) -> bool:
    """For a 1–1 room: whether the other person still works in the company."""
    others = [pk for pk in room.direct_participant_ids() if pk != viewer.employee_id]
    return bool(others) and _active_employees(room.company_id).filter(pk=others[0]).exists()


def can_post(viewer: OKRViewer, room: OKRChatRoom) -> bool:
    if room.kind == ChatRoomKind.DIRECT:
        return direct_partner_active(room, viewer)
    return True


def _require_post(viewer: OKRViewer, room: OKRChatRoom):
    if not can_post(viewer, room):
        raise errors.OKRError(
            "chat_read_only",
            "Người nhận không còn làm việc tại công ty nên không gửi được tin nhắn mới.",
            403,
        )


def can_moderate(viewer: OKRViewer, room: OKRChatRoom) -> bool:
    """May hide other people's messages: managers in the rooms they run, OKR
    admins in OKR rooms. Never in a 1–1 room."""
    if room.kind == ChatRoomKind.DIRECT:
        return False
    if room.kind == ChatRoomKind.TEAM:
        return room.team_id in viewer.managed_team_ids
    if room.kind == ChatRoomKind.DEPARTMENT:
        return room.department_id in viewer.managed_department_ids
    item = okr_item(room)
    if item is None:
        return False
    objective = _objective_of(item)
    return (
        viewer.is_admin
        or (bool(objective.team_id) and objective.team_id in viewer.managed_team_ids)
        or (bool(objective.department_id) and objective.department_id in viewer.managed_department_ids)
    )


# -- rooms -----------------------------------------------------------------------


def _rooms(company_id: int):
    return OKRChatRoom.objects.for_company(company_id).select_related(*ROOM_RELATED)


def _get_or_create_room(company_id: int, kind: str, **target) -> OKRChatRoom:
    room = _rooms(company_id).filter(kind=kind, **target).first()
    if room is not None:
        return room
    try:
        with transaction.atomic():
            OKRChatRoom.objects.create(company_id=company_id, kind=kind, **target)
    except IntegrityError:
        pass  # someone else created it a moment ago
    return _rooms(company_id).get(kind=kind, **target)


def _top_message_id(room: OKRChatRoom) -> int:
    return room.messages.aggregate(top=Max("id"))["top"] or 0


def ensure_member(room: OKRChatRoom, employee, *, read_up_to: Optional[int] = None) -> OKRChatMember:
    """The employee's state row in the room. A new row starts as "read up to
    now" unless ``read_up_to`` says otherwise, so joining a busy room does not
    flood the unread badge."""
    member = OKRChatMember.objects.filter(room=room, employee=employee).first()
    if member is not None:
        return member
    start = _top_message_id(room) if read_up_to is None else read_up_to
    try:
        with transaction.atomic():
            return OKRChatMember.objects.create(
                company_id=room.company_id, room=room, employee=employee, last_read_id=start
            )
    except IntegrityError:
        return OKRChatMember.objects.get(room=room, employee=employee)


def get_room(viewer: OKRViewer, room_id) -> OKRChatRoom:
    require_enabled()
    room = _rooms(viewer.company_id).filter(pk=room_id).first() if room_id else None
    if room is None or not can_access(viewer, room):
        raise _room_not_found()
    return room


def open_room(viewer: OKRViewer, *, kind: str, target_id) -> OKRChatRoom:
    """Find or create the room for one target (idempotent), after checking
    the viewer may use it, and register the viewer in it."""
    require_enabled()
    if not target_id:
        raise errors.validation({"target": "Thiếu đối tượng của cuộc trò chuyện."})
    company_id = viewer.company_id
    if kind == ChatRoomKind.OBJECTIVE:
        item = okrs.get_objective(viewer, target_id)
        target = {"objective": item}
    elif kind == ChatRoomKind.KEY_RESULT:
        item = okrs.get_key_result(viewer, target_id)
        target = {"key_result": item}
    elif kind == ChatRoomKind.TEAM:
        team = OKRTeam.objects.for_company(company_id).filter(pk=target_id, status=RecordStatus.ACTIVE).first()
        if team is None:
            raise _room_not_found()
        item, target = None, {"team": team}
    elif kind == ChatRoomKind.DEPARTMENT:
        from base.models import Department

        department = Department._base_manager.filter(pk=target_id, is_active=True).first()
        if department is None or not department_allows_company(department, company_id):
            raise _room_not_found()
        item, target = None, {"department": department}
    elif kind == ChatRoomKind.DIRECT:
        other = _active_employees(company_id).filter(pk=target_id).first()
        if other is None:
            raise _room_not_found()
        if other.pk == viewer.employee_id:
            raise errors.validation({"employee_id": "Không thể tự nhắn tin cho chính mình."})
        item, target = None, {"direct_key": direct_key(viewer.employee_id, other.pk)}
    else:
        raise errors.validation({"kind": "Loại cuộc trò chuyện không hợp lệ."})

    if item is not None:
        if not viewer.can_view_scope(scope_of(item)):
            raise _room_not_found()
        if not can_join_okr(viewer, item):
            raise errors.OKRError(
                "chat_not_member",
                "Bạn không thuộc Team / phòng ban của OKR này nên chưa tham gia trao đổi được.",
                403,
            )
    room = _get_or_create_room(company_id, kind, **target)
    if not can_access(viewer, room):
        raise _room_not_found()
    ensure_member(room, viewer.employee)
    if kind == ChatRoomKind.DIRECT:
        ensure_member(room, other)
    return room


def _ensure_org_rooms(viewer: OKRViewer) -> None:
    """Team and department rooms exist (and list the viewer) without anyone
    having to open them first."""
    company_id = viewer.company_id
    team_ids = set(team_ids_of(viewer)) | set(viewer.managed_team_ids)
    for team in OKRTeam.objects.for_company(company_id).filter(pk__in=team_ids, status=RecordStatus.ACTIVE):
        ensure_member(_get_or_create_room(company_id, ChatRoomKind.TEAM, team=team), viewer.employee)
    from base.models import Department

    department_ids = set(viewer.managed_department_ids)
    if department_of(viewer):
        department_ids.add(department_of(viewer))
    for department in Department._base_manager.filter(pk__in=department_ids, is_active=True):
        if department_allows_company(department, company_id):
            ensure_member(
                _get_or_create_room(company_id, ChatRoomKind.DEPARTMENT, department=department), viewer.employee
            )


def accessible_memberships(viewer: OKRViewer, *, ensure: bool = True) -> list:
    """The viewer's ``OKRChatMember`` rows whose room they may still use."""
    require_enabled()
    if ensure:
        _ensure_org_rooms(viewer)
    rows = (
        OKRChatMember.objects.for_company(viewer.company_id)
        .filter(employee_id=viewer.employee_id)
        .select_related(*(f"room__{path}" for path in ROOM_RELATED), "room")
        .order_by("-room__last_message_at", "-room_id")[: MAX_ROOMS * 2]
    )
    return [member for member in rows if can_access(viewer, member.room)][:MAX_ROOMS]


def unread_counts(viewer: OKRViewer, memberships: Iterable[OKRChatMember]) -> dict:
    """``{room id: unread}`` in one query: other people's live messages after
    the viewer's read position."""
    memberships = list(memberships)
    if not memberships:
        return {}
    condition = Q()
    for member in memberships:
        condition |= Q(room_id=member.room_id, id__gt=member.last_read_id)
    return dict(
        OKRChatMessage.objects.filter(condition, recalled_at__isnull=True)
        .exclude(sender_id=viewer.employee_id)
        .values("room_id")
        .annotate(n=Count("id"))
        .values_list("room_id", "n")
    )


def last_messages(room_ids: Iterable[int]) -> dict:
    room_ids = list(room_ids)
    if not room_ids:
        return {}
    tops = (
        OKRChatMessage.objects.filter(room_id__in=room_ids)
        .values("room_id")
        .annotate(top=Max("id"))
        .values_list("top", flat=True)
    )
    return {
        message.room_id: message
        for message in OKRChatMessage.objects.filter(pk__in=list(tops)).select_related("sender")
    }


def direct_partners(viewer: OKRViewer, rooms: Iterable[OKRChatRoom]) -> dict:
    """``{room id: the other employee}`` for 1–1 rooms, in one query."""
    from employee.models import Employee

    wanted = {}
    for room in rooms:
        for pk in room.direct_participant_ids():
            if pk != viewer.employee_id:
                wanted[room.pk] = pk
    people = Employee._base_manager.in_bulk(set(wanted.values()))
    return {room_id: people.get(pk) for room_id, pk in wanted.items()}


def room_title(room: OKRChatRoom, partner=None) -> str:
    if room.kind == ChatRoomKind.OBJECTIVE and room.objective_id:
        return room.objective.title
    if room.kind == ChatRoomKind.KEY_RESULT and room.key_result_id:
        return room.key_result.title
    if room.kind == ChatRoomKind.TEAM and room.team_id:
        return room.team.name
    if room.kind == ChatRoomKind.DEPARTMENT and room.department_id:
        return room.department.department
    if room.kind == ChatRoomKind.DIRECT:
        return partner.get_full_name() if partner is not None else "Trò chuyện 1–1"
    return "Trò chuyện"


def list_rooms(viewer: OKRViewer, *, kind: str = "", search: str = "") -> list:
    """``[(room, member, unread, last message, partner)]``, newest activity first."""
    memberships = accessible_memberships(viewer)
    if kind:
        memberships = [m for m in memberships if m.room.kind == kind]
    partners = direct_partners(viewer, [m.room for m in memberships])
    term = fold(search).strip()
    if term:
        memberships = [m for m in memberships if term in fold(room_title(m.room, partners.get(m.room_id)))]
    unread = unread_counts(viewer, memberships)
    lasts = last_messages(m.room_id for m in memberships)
    return [
        (m.room, m, unread.get(m.room_id, 0), lasts.get(m.room_id), partners.get(m.room_id))
        for m in memberships
    ]


def unread_total(viewer: OKRViewer) -> dict:
    memberships = accessible_memberships(viewer)
    counts = unread_counts(viewer, memberships)
    return {"unread": sum(counts.values()), "rooms": sum(1 for n in counts.values() if n)}


# -- who belongs to a room (pickers, push) ----------------------------------------


def _org_member_ids(room: OKRChatRoom) -> set:
    """Employee ids of a team, department or 1–1 room, from live data."""
    company_id = room.company_id
    if room.kind == ChatRoomKind.TEAM:
        team = room.team
        if team is None or team.status != RecordStatus.ACTIVE:
            return set()
        ids = set(OKRTeamMember.objects.for_company(company_id).filter(team=team).values_list("employee_id", flat=True))
        if team.manager_id:
            ids.add(team.manager_id)
    elif room.kind == ChatRoomKind.DEPARTMENT:
        ids = set(
            _active_employees(company_id)
            .filter(employee_work_info__department_id=room.department_id)
            .values_list("pk", flat=True)
        )
        ids.update(
            OKRDepartmentRole.objects.for_company(company_id)
            .filter(department_id=room.department_id)
            .values_list("manager_id", flat=True)
        )
    elif room.kind == ChatRoomKind.DIRECT:
        ids = set(room.direct_participant_ids())
    else:
        return set()
    return set(_active_employees(company_id).filter(pk__in=ids).values_list("pk", flat=True))


def _okr_candidate_ids(room: OKRChatRoom) -> set:
    """Everyone who could belong to an OKR room, before the exact check."""
    item = okr_item(room)
    if item is None:
        return set()
    company_id = room.company_id
    objective = _objective_of(item)
    ids = {objective.owner_id, getattr(item, "owner_id", None)}
    ids.update(
        OKRRole.objects.for_company(company_id)
        .filter(role__in=[OKRRoleType.ADMIN, OKRRoleType.MANAGER])
        .values_list("employee_id", flat=True)
    )
    ids.update(room.members.values_list("employee_id", flat=True))
    if objective.scope == Scope.TEAM and objective.team_id:
        ids.update(
            OKRTeamMember.objects.for_company(company_id).filter(team_id=objective.team_id).values_list(
                "employee_id", flat=True
            )
        )
        ids.add(objective.team.manager_id)
    elif objective.scope == Scope.DEPARTMENT and objective.department_id:
        ids.update(
            _active_employees(company_id)
            .filter(employee_work_info__department_id=objective.department_id)
            .values_list("pk", flat=True)
        )
    elif objective.scope == Scope.PERSONAL and objective.owner_id:
        owner_department, owner_manager = _work(objective.owner_id, company_id)
        ids.add(owner_manager)
        if owner_department:
            ids.update(
                _active_employees(company_id)
                .filter(employee_work_info__department_id=owner_department)
                .values_list("pk", flat=True)
            )
    ids.discard(None)
    return ids


def _viewer_for(employee) -> Optional[OKRViewer]:
    viewer = OKRViewer.for_employee(employee)
    return viewer if viewer is not None and viewer.is_active else None


def room_members(viewer: OKRViewer, room: OKRChatRoom, *, search: str = "", limit: int = MEMBER_LIMIT) -> list:
    """People who may use the room, for the member list and the @ picker.

    Team, department and 1–1 rooms come straight from organisation data.
    OKR rooms are checked person by person with ``can_access``, on at most
    ``limit`` candidates after the search."""
    company_id = room.company_id
    if room.kind in (ChatRoomKind.TEAM, ChatRoomKind.DEPARTMENT, ChatRoomKind.DIRECT):
        ids = _org_member_ids(room)
        exact = True
    else:
        ids = _okr_candidate_ids(room)
        exact = False
    rows = _active_employees(company_id).filter(pk__in=ids).order_by("employee_first_name", "employee_last_name", "id")
    term = fold(search).strip()
    # During a pilot, people outside the test group have no chat: they are
    # neither listed nor offered in the @ picker.
    people = [e for e in rows if enabled_for(e.pk) and (not term or term in fold(e.get_full_name()))]
    if exact:
        return people[:limit]
    allowed = []
    for employee in people:
        if len(allowed) >= limit:
            break
        if can_access(_viewer_for(employee), room):
            allowed.append(employee)
    return allowed


# -- messages ----------------------------------------------------------------------


def _messages(room: OKRChatRoom):
    return (
        room.messages.select_related("sender", "reply_to__sender")
        .prefetch_related("attachments", "mentions__employee")
    )


def _parse_cursor(value, name: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = -1
    if number < 0:
        raise errors.validation({name: "Giá trị không hợp lệ."})
    return number


def list_messages(viewer: OKRViewer, room_id, *, before=None, after=None, changed_since=None, search: str = "") -> dict:
    """One window of a room's history.

    * no cursor: the latest ``PAGE`` messages;
    * ``before=<id>``: the ``PAGE`` messages before that id (scrolling up);
    * ``after=<id>``: up to ``MAX_AFTER`` messages after it (polling);
    * ``changed_since=<server_time>`` with ``after``: messages the client
      already has that were edited or recalled since its last poll;
    * ``search``: up to ``SEARCH_LIMIT`` live messages matching the text.

    Results are always oldest first."""
    room = get_room(viewer, room_id)
    before = _parse_cursor(before, "before")
    after = _parse_cursor(after, "after")
    since = None
    if changed_since not in (None, ""):
        since = parse_datetime(str(changed_since))
        if since is None:
            raise errors.validation({"changed_since": "Thời điểm không hợp lệ."})
        if timezone.is_naive(since):
            since = timezone.make_aware(since, datetime.timezone.utc)
    server_time = timezone.now()
    base = _messages(room)
    result = {"room": room, "has_more_before": False, "has_more_after": False, "changed": [], "server_time": server_time}
    term = fold(search).strip()
    if term:
        rows = list(base.filter(recalled_at__isnull=True, search_text__contains=term).order_by("-id")[:SEARCH_LIMIT])
        result["results"] = rows[::-1]
        return result
    if after is not None:
        rows = list(base.filter(id__gt=after).order_by("id")[: MAX_AFTER + 1])
        result["has_more_after"] = len(rows) > MAX_AFTER
        result["results"] = rows[:MAX_AFTER]
        if since is not None:
            result["changed"] = list(base.filter(id__lte=after, updated_at__gt=since).order_by("id")[:MAX_CHANGED])
        return result
    query = base.filter(id__lt=before) if before else base
    rows = list(query.order_by("-id")[: PAGE + 1])
    result["has_more_before"] = len(rows) > PAGE
    result["results"] = rows[:PAGE][::-1]
    return result


def _rate_limited(queryset, limit: int) -> bool:
    return queryset.filter(created_at__gte=timezone.now() - RATE_WINDOW).count() >= limit


def _clean_content(content) -> str:
    text = str(content or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_CONTENT:
        raise errors.validation({"content": f"Tin nhắn tối đa {MAX_CONTENT:,} ký tự.".replace(",", ".")})
    return text


def _int_list(values, name: str, limit: int) -> list:
    if values in (None, ""):
        return []
    if isinstance(values, (str, int)):
        values = [values]
    try:
        numbers = list(dict.fromkeys(int(v) for v in values))
    except (TypeError, ValueError):
        raise errors.validation({name: "Giá trị không hợp lệ."})
    if any(n < 1 for n in numbers) or len(numbers) > limit:
        raise errors.validation({name: "Giá trị không hợp lệ."})
    return numbers


def send_message(
    viewer: OKRViewer,
    room_id,
    *,
    content="",
    client_message_id="",
    reply_to_id=None,
    attachment_ids=None,
    mention_ids=None,
) -> tuple:
    """``(message, created)``. Re-sending the same ``client_message_id``
    returns the first message (``created`` False), so a double tap or a
    retry over a flaky network never posts twice."""
    room = get_room(viewer, room_id)
    _require_post(viewer, room)
    client_id = str(client_message_id or "")
    if not CLIENT_ID.match(client_id):
        raise errors.validation({"client_message_id": "Mã tin nhắn không hợp lệ."})
    existing = room.messages.filter(sender_id=viewer.employee_id, client_message_id=client_id).first()
    if existing is not None:
        return existing, False
    text = _clean_content(content)
    attachment_ids = _int_list(attachment_ids, "attachment_ids", MAX_ATTACHMENTS)
    if not text and not attachment_ids:
        raise errors.validation({"content": "Nhập nội dung hoặc chọn tệp đính kèm."})
    attachments = list(
        OKRChatAttachment.objects.filter(
            pk__in=attachment_ids, room=room, uploader_id=viewer.employee_id, message__isnull=True
        )
    )
    if len(attachments) != len(attachment_ids):
        raise errors.validation({"attachment_ids": "Tệp đính kèm không hợp lệ hoặc đã được gửi."})
    reply = None
    if reply_to_id not in (None, ""):
        reply = room.messages.filter(pk=_parse_cursor(reply_to_id, "reply_to")).first()
        if reply is None:
            raise errors.validation({"reply_to": "Tin nhắn được trả lời không hợp lệ."})
    if _rate_limited(OKRChatMessage.objects.filter(sender_id=viewer.employee_id), RATE_MESSAGES):
        raise errors.OKRError("throttled", "Bạn gửi tin quá nhanh, vui lòng thử lại sau ít phút.", 429)
    mentioned = _valid_mentions(viewer, room, _int_list(mention_ids, "mention_ids", MAX_MENTIONS))

    with transaction.atomic():
        try:
            with transaction.atomic():
                message = OKRChatMessage.objects.create(
                    company_id=room.company_id,
                    room=room,
                    sender=viewer.employee,
                    client_message_id=client_id,
                    content=text,
                    search_text=fold(text),
                    reply_to=reply,
                )
        except IntegrityError:
            return room.messages.get(sender_id=viewer.employee_id, client_message_id=client_id), False
        if attachments:
            linked = OKRChatAttachment.objects.filter(
                pk__in=[a.pk for a in attachments], message__isnull=True
            ).update(message=message)
            if linked != len(attachments):
                raise errors.validation({"attachment_ids": "Tệp đính kèm không hợp lệ hoặc đã được gửi."})
        OKRChatMention.objects.bulk_create(
            [OKRChatMention(company_id=room.company_id, message=message, employee=e) for e in mentioned]
        )
        OKRChatRoom.objects.filter(pk=room.pk).update(last_message_at=message.created_at)
        member = ensure_member(room, viewer.employee)
        OKRChatMember.objects.filter(pk=member.pk).update(last_read_id=message.pk, last_read_at=timezone.now())
        _notify_in_app(viewer, room, message, mentioned, reply)
        transaction.on_commit(lambda: dispatch(message.pk))
    return _messages(room).get(pk=message.pk), True


def _valid_mentions(viewer: OKRViewer, room: OKRChatRoom, ids: list) -> list:
    """The mentioned people who may use the room (others are ignored, not
    told they exist)."""
    if not ids:
        return []
    people = _active_employees(room.company_id).filter(pk__in=[i for i in ids if i != viewer.employee_id])
    return [person for person in people if enabled_for(person.pk) and can_access(_viewer_for(person), room)]


def _notify_in_app(viewer: OKRViewer, room: OKRChatRoom, message: OKRChatMessage, mentioned: list, reply):
    """In-app notification for a mention or a reply. No message content: the
    notification only says who did what; the text stays in the room."""
    sender_name = viewer.employee.get_full_name()
    targets = {person.pk: (person, MENTION) for person in mentioned}
    if reply is not None and reply.sender_id and reply.sender_id != viewer.employee_id and reply.sender_id not in targets:
        author = reply.sender
        if author is not None and enabled_for(author.pk) and can_access(_viewer_for(author), room):
            targets[author.pk] = (author, REPLY)
    label = KIND_LABELS.get(room.kind, "OKR")
    for person, kind in targets.values():
        title = (
            f"{sender_name} đã nhắc đến bạn ({label})"
            if kind == MENTION
            else f"{sender_name} đã trả lời tin nhắn của bạn ({label})"
        )
        notifications.send(
            person,
            company_id=room.company_id,
            notification_type=kind,
            title=title,
            reference_type=ReferenceType.CHAT_MESSAGE,
            reference_id=message.pk,
            dedupe_key=f"chat:{kind}:{message.pk}:{person.pk}",
            actor=viewer.employee,
        )


def _message_for(viewer: OKRViewer, message_id) -> OKRChatMessage:
    require_enabled()
    message = (
        OKRChatMessage.objects.for_company(viewer.company_id)
        .select_related(*(f"room__{path}" for path in ROOM_RELATED), "room", "sender")
        .filter(pk=message_id)
        .first()
        if message_id
        else None
    )
    if message is None or not can_access(viewer, message.room):
        raise _message_not_found()
    return message


def edit_message(viewer: OKRViewer, message_id, *, content="") -> OKRChatMessage:
    message = _message_for(viewer, message_id)
    if message.sender_id != viewer.employee_id:
        raise errors.forbidden("Bạn chỉ sửa được tin nhắn của chính mình.")
    if message.is_recalled:
        raise errors.rule("chat_recalled", "Tin nhắn đã được thu hồi.")
    if timezone.now() - message.created_at > EDIT_WINDOW:
        raise errors.rule("chat_edit_window", "Chỉ sửa được tin nhắn trong 24 giờ sau khi gửi.")
    _require_post(viewer, message.room)
    text = _clean_content(content)
    if not text and not message.attachments.exists():
        raise errors.validation({"content": "Nội dung tin nhắn là bắt buộc."})
    message.content = text
    message.search_text = fold(text)
    message.edited_at = timezone.now()
    message.save(update_fields=["content", "search_text", "edited_at", "updated_at"])
    return _messages(message.room).get(pk=message.pk)


def recall_message(viewer: OKRViewer, message_id) -> OKRChatMessage:
    """Take a message back: its text and files are removed for everyone and
    only "Tin nhắn đã được thu hồi" remains. The sender may always do it;
    managers and OKR admins in the rooms they moderate."""
    message = _message_for(viewer, message_id)
    own = message.sender_id == viewer.employee_id
    if not own and not can_moderate(viewer, message.room):
        raise errors.forbidden("Bạn không có quyền thu hồi tin nhắn này.")
    if not message.is_recalled:
        with transaction.atomic():
            files = list(message.attachments.values_list("file", flat=True))
            message.attachments.all().delete()
            message.content = ""
            message.search_text = ""
            message.recalled_at = timezone.now()
            message.recalled_by = viewer.employee
            message.save(update_fields=["content", "search_text", "recalled_at", "recalled_by", "updated_at"])
            OKRChatMention.objects.filter(message=message).delete()
            transaction.on_commit(lambda: _delete_files(files))
    return _messages(message.room).get(pk=message.pk)


def _delete_files(names: Iterable[str]):
    from okr.storage import evidence_storage

    storage = evidence_storage()
    for name in names:
        try:
            storage.delete(name)
        except OSError:
            logger.warning("okr chat: could not delete a recalled attachment file")


def mark_read(viewer: OKRViewer, room_id, message_id=None) -> int:
    """Move the viewer's read position forward (never back). Settles the chat
    notifications about the messages now read."""
    room = get_room(viewer, room_id)
    top = _top_message_id(room)
    wanted = _parse_cursor(message_id, "message_id")
    target = top if wanted is None else min(wanted, top)
    member = ensure_member(room, viewer.employee)
    OKRChatMember.objects.filter(pk=member.pk, last_read_id__lt=target).update(
        last_read_id=target, last_read_at=timezone.now()
    )
    OKRNotification.objects.for_company(viewer.company_id).filter(
        recipient_id=viewer.employee_id,
        notification_type__in=CHAT_NOTIFICATION_TYPES,
        reference_type=ReferenceType.CHAT_MESSAGE,
        reference_id__in=room.messages.filter(id__lte=target).values("id"),
        is_read=False,
    ).update(is_read=True)
    return max(target, member.last_read_id)


def set_muted(viewer: OKRViewer, room_id, muted: bool) -> OKRChatMember:
    room = get_room(viewer, room_id)
    member = ensure_member(room, viewer.employee)
    OKRChatMember.objects.filter(pk=member.pk).update(muted=bool(muted))
    member.muted = bool(muted)
    return member


def search_messages(viewer: OKRViewer, search: str) -> list:
    """Live messages matching ``search`` in the rooms the viewer may use."""
    term = fold(search).strip()
    if len(term) < 2:
        raise errors.validation({"q": "Nhập ít nhất 2 ký tự để tìm."})
    room_ids = [member.room_id for member in accessible_memberships(viewer)]
    return list(
        OKRChatMessage.objects.filter(room_id__in=room_ids, recalled_at__isnull=True, search_text__contains=term)
        .select_related("sender", "room", "reply_to__sender", *(f"room__{path}" for path in ROOM_RELATED))
        .prefetch_related("attachments", "mentions__employee")
        .order_by("-id")[:SEARCH_LIMIT]
    )


# -- attachments ----------------------------------------------------------------------


def upload_attachment(viewer: OKRViewer, room_id, upload) -> OKRChatAttachment:
    room = get_room(viewer, room_id)
    _require_post(viewer, room)
    if upload is None:
        raise errors.validation({"file": "Vui lòng chọn tệp."})
    mine = OKRChatAttachment.objects.filter(uploader_id=viewer.employee_id)
    if _rate_limited(mine, RATE_FILES):
        raise errors.OKRError("throttled", "Bạn gửi tệp quá nhanh, vui lòng thử lại sau ít phút.", 429)
    if mine.filter(message__isnull=True).count() >= MAX_PENDING_ATTACHMENTS:
        raise errors.rule("chat_too_many_pending", "Có quá nhiều tệp chưa gửi. Hãy gửi hoặc tải lại trang trước.")
    name, content_type, sha256 = check_chat_upload(upload)
    return OKRChatAttachment.objects.create(
        company_id=room.company_id,
        room=room,
        uploader=viewer.employee,
        file=upload,
        original_name=name,
        content_type=content_type,
        size=upload.size,
        sha256=sha256,
        is_image=content_type in IMAGE_TYPES,
    )


def delete_pending_attachment(viewer: OKRViewer, attachment_id) -> None:
    """Drop a file the viewer uploaded and has not sent (a cancelled or
    removed upload). Anything else — someone else's file, a sent file — is
    404, as if it did not exist."""
    require_enabled()
    with transaction.atomic():
        item = (
            OKRChatAttachment.objects.for_company(viewer.company_id)
            .select_for_update()
            .filter(pk=attachment_id or 0, uploader_id=viewer.employee_id, message__isnull=True)
            .first()
        )
        if item is None:
            raise errors.OKRError("not_found", "Không tìm thấy tệp.", 404)
        name = item.file.name
        item.delete()
        transaction.on_commit(lambda: _delete_files([name]))


def attachment_for_download(viewer: OKRViewer, attachment_id) -> OKRChatAttachment:
    require_enabled()
    attachment = (
        OKRChatAttachment.objects.for_company(viewer.company_id)
        .select_related(*(f"room__{path}" for path in ROOM_RELATED), "room", "message")
        .filter(pk=attachment_id)
        .first()
        if attachment_id
        else None
    )
    if attachment is None or not can_access(viewer, attachment.room):
        raise errors.OKRError("not_found", "Không tìm thấy tệp.", 404)
    if attachment.message_id is None and attachment.uploader_id != viewer.employee_id:
        raise errors.OKRError("not_found", "Không tìm thấy tệp.", 404)
    if attachment.message_id is not None and attachment.message.is_recalled:
        raise errors.OKRError("not_found", "Không tìm thấy tệp.", 404)
    return attachment


# -- notifications reference (used by okr.services.notifications) ---------------------


def notification_target(viewer: OKRViewer, message_id) -> Optional[OKRChatMessage]:
    """The message a chat notification points at, if the viewer may still
    open it (otherwise the notification is shown as no longer available)."""
    if not enabled_for(viewer.employee_id) or not message_id:
        return None
    message = (
        OKRChatMessage.objects.for_company(viewer.company_id)
        .select_related(*(f"room__{path}" for path in ROOM_RELATED), "room")
        .filter(pk=message_id)
        .first()
    )
    if message is None or message.is_recalled or not can_access(viewer, message.room):
        return None
    return message


# -- push fan-out ------------------------------------------------------------------------


def _run_in_background(function, *args):
    def runner():
        try:
            function(*args)
        finally:
            connection.close()

    threading.Thread(target=runner, name="okr-chat-push", daemon=True).start()


#: How fan-out runs; tests replace it with a direct call.
runner = _run_in_background


def dispatch(message_id: int):
    if enabled():
        runner(fan_out, message_id)


def _push_allowed(user) -> bool:
    from joydigi_api.models import NotificationPreference

    return not NotificationPreference.objects.filter(user=user, all_notifications_enabled=False).exists()


def fan_out(message_id: int) -> dict:
    """Push one new message to the people who should hear about it.

    Recipients: the other person (1–1); every member (team, department); the
    people who joined the room (OKR); plus whoever was mentioned or replied
    to. Each is checked again with ``can_access`` right before sending, so
    nobody who lost access is pushed. A plain new message reaches a person at
    most once per room every ``PUSH_GAP``; mentions and replies always go
    out. Muted rooms only push mentions. Nothing of the message text is sent.
    """
    tally = {"pushed": 0, "skipped": 0}
    try:
        message = (
            OKRChatMessage.objects.select_related(*(f"room__{path}" for path in ROOM_RELATED), "room", "sender", "reply_to")
            .filter(pk=message_id, recalled_at__isnull=True)
            .first()
        )
        if message is None or message.sender is None:
            return tally
        room = message.room
        reasons = {}
        for employee_id in message.mentions.values_list("employee_id", flat=True):
            reasons[employee_id] = MENTION
        if message.reply_to_id and message.reply_to.sender_id:
            reasons.setdefault(message.reply_to.sender_id, REPLY)
        if room.kind in (ChatRoomKind.TEAM, ChatRoomKind.DEPARTMENT, ChatRoomKind.DIRECT):
            pool = _org_member_ids(room)
            # Members who never opened the room still get an unread badge.
            OKRChatMember.objects.bulk_create(
                [
                    OKRChatMember(company_id=room.company_id, room=room, employee_id=pk, last_read_id=message.pk - 1)
                    for pk in pool - set(room.members.values_list("employee_id", flat=True))
                ],
                ignore_conflicts=True,
            )
        else:
            pool = set(room.members.values_list("employee_id", flat=True))
        for employee_id in pool:
            reasons.setdefault(employee_id, "message")
        reasons.pop(message.sender_id, None)
        if not reasons:
            return tally

        from joydigi_api.push import send_to_user

        members = {m.employee_id: m for m in room.members.filter(employee_id__in=reasons)}
        sender_name = message.sender.get_full_name()
        label = KIND_LABELS.get(room.kind, "OKR")
        now = timezone.now()
        people = _active_employees(room.company_id).filter(pk__in=reasons).select_related("employee_user_id")
        for person in people:
            reason = reasons[person.pk]
            member = members.get(person.pk)
            if reason == "message" and member is not None:
                if member.muted or (member.last_push_at and now - member.last_push_at < PUSH_GAP):
                    tally["skipped"] += 1
                    continue
            if (
                not enabled_for(person.pk)
                or not can_access(_viewer_for(person), room)
                or not _push_allowed(person.employee_user_id)
            ):
                tally["skipped"] += 1
                continue
            body = {
                MENTION: f"{sender_name} đã nhắc đến bạn ({label})",
                REPLY: f"{sender_name} đã trả lời tin nhắn của bạn ({label})",
            }.get(reason, f"{sender_name} · Tin nhắn mới ({label})")
            send_to_user(
                person.employee_user_id,
                "JOYDIGI OKR",
                body,
                data={"type": "okr_chat", "room_id": room.pk, "message_id": message.pk, "reason": reason},
            )
            tally["pushed"] += 1
            if member is not None:
                OKRChatMember.objects.filter(pk=member.pk).update(last_push_at=now)
    except Exception:  # pragma: no cover - never let a push failure surface
        # The message id only: never its content.
        logger.exception("okr chat: push fan-out failed for message %s", message_id)
    return tally
