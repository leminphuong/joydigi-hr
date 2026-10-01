"""Phase REQUEST-STATUS-AND-APPROVAL-NOTIFICATIONS — the employee learns the outcome.

## Why this is a signal and not a line in each approval view

An employee request can be approved or rejected from a great many places: the
Django admin change form, the HR approval hub (`base/checkin_portal.py`), the
older per-type HR pages (`base/views.py`, `leave/views.py`), class-based form
views, one ModelForm, and a dozen mobile API endpoints including bulk ones.
There is no shared approval service in this codebase and no common base class
for request models — each model re-declares its own `approved`/`canceled` pair
or its own `status` string. Adding a notification call to each site would mean
roughly fifty edits, and would guarantee that the fifty-first site somebody
writes is silent.

So this module observes the one thing every one of those sites has in common:
the row's own ``save()``. ``pre_save`` remembers the state the row has in the
database, ``post_save`` compares it with the state the row now has, and a real
``pending -> approved`` or ``pending -> rejected`` transition is what notifies —
no matter which screen caused it. That is also what makes a double-click safe:
``approved -> approved`` is not a transition, so the second save sends nothing.

## What this deliberately does not cover

Four writes in the repository change an approval field without going through
``save()``, so no signal can see them:

* ``attendance/methods/september_2026_repair.py:688`` —
  ``overtime_requests.update(approved=True)``. A historical repair command for
  two named days in the past. Notifying about it would push months-old news to
  everybody it touched, so it stays silent on purpose.
* ``employee/views.py:1117`` and ``:1171`` — bulk document approve/reject.
  ``joydigi_documents.Document`` is not an employee request in the "Đơn từ"
  sense and has no screen in the app.
* ``joydigi_api/api_views/leave/views.py:753`` and ``leave/signals.py:216`` —
  ``LeaveRequestConditionApproval.update(is_approved=True)``. That is one stage
  of a multi-step approval, not the request's own outcome; the request's own
  ``status`` is written with ``save()`` immediately afterwards
  (``joydigi_api/api_views/leave/views.py:755``), which is the transition the
  employee cares about and the one this module reports.

Creation is also not a transition. A request that is born approved (a leave
type with ``require_approval == "no"``) tells the employee the outcome in the
response to their own submission, so there is nothing to announce.

## Order of operations

The in-app row is written first and the push is registered with
``transaction.on_commit`` — both for the reason the announcement push already
documents (``base/cbv/announcement_cbv.py``): a push cannot be rolled back, so
announcing a decision whose transaction then failed would be a notification
about something that did not happen. The push is also skipped entirely when no
in-app row was written, which is the only thing that makes it honour the
recipient's notification preference — ``joydigi_api.push`` never consults
``NotificationPreference`` itself.
"""

import logging
from functools import partial

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

#: The three states this module reasons about. "cancelled" exists for leave
#: requests but is the employee's own doing, so it is never announced to them.
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
CANCELLED = "cancelled"

#: `data["type"]` on the wire. The app parses these into a destination; see
#: `appcheckin/lib/features/push/domain/push_notification_type.dart`.
TYPE_APPROVED = "request_approved"
TYPE_REJECTED = "request_rejected"

PUSH_TYPES = {APPROVED: TYPE_APPROVED, REJECTED: TYPE_REJECTED}

#: Where the dedupe marker is stored inside `Notification.data`, following the
#: reminder convention (`attendance/methods/reminders.py`).
MARKER_KEY = "request_decision"

TITLES = {
    APPROVED: "Đơn của bạn đã được duyệt",
    REJECTED: "Đơn của bạn đã bị từ chối",
}

OUTCOMES = {APPROVED: "đã được duyệt", REJECTED: "đã bị từ chối"}


def _get(source, name):
    """Read a field from either a model instance or a `values()` dict."""
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def _boolean_state(source):
    """The state of a model that carries an `approved`/`canceled` pair.

    `canceled` is read as "rejected" because that is what the models
    themselves say it means — every one of them derives the label "Rejected"
    from it in its own `request_status()`. The bit is genuinely overloaded: an
    employee cancelling their own pending request writes exactly the same one,
    so on its own it cannot tell "a manager refused this" from "I withdrew
    this". That limitation is recorded in this phase's report, and the one
    place it would have been user-visible is closed in `_announce_decision`,
    which does not announce a decision the owner made themselves.
    """
    if _get(source, "canceled"):
        return REJECTED
    if _get(source, "approved"):
        return APPROVED
    return PENDING


#: `leave.LeaveRequest` is the one request type with a real four-value status
#: field, and the only one that can tell "rejected" and "cancelled" apart.
LEAVE_STATES = {
    "requested": PENDING,
    "approved": APPROVED,
    "rejected": REJECTED,
    "cancelled": CANCELLED,
}


def _leave_state(source):
    return LEAVE_STATES.get(_get(source, "status"), PENDING)


def _format_date(value):
    return value.strftime("%d/%m/%Y") if value else ""


def _one_day(source, field):
    day = _format_date(_get(source, field))
    return f"ngày {day}" if day else ""


def _range(source, start_field, end_field):
    """"ngày X" for a single day, "từ ngày X đến ngày Y" for a real range."""
    start = _format_date(_get(source, start_field))
    end = _format_date(_get(source, end_field))
    if start and end and end != start:
        return f"từ ngày {start} đến ngày {end}"
    return f"ngày {start or end}" if (start or end) else ""


class RequestKind:
    """Everything this module needs to know about one request model.

    `state_fields` is what gets read back from the database to establish the
    previous state, so it is deliberately the smallest possible column set —
    a `pre_save` runs on every save of these models.
    """

    def __init__(self, slug, noun, state, state_fields, when):
        self.slug = slug
        self.noun = noun
        self.state = state
        self.state_fields = state_fields
        self.when = when

    def body(self, instance, status):
        when = self.when(instance)
        subject = f"{self.noun} {when}".strip()
        return f"{subject} {OUTCOMES[status]}."


BOOLEAN_FIELDS = ("approved", "canceled")


def _kinds():
    """The registry, built lazily so importing this module imports no models."""
    from attendance.models import (
        AttendanceExplanationRequest,
        AttendanceLateEarlyRequest,
        OvertimeRequest,
        RemoteWorkRequest,
    )
    from base.models import ShiftRequest, WorkTypeRequest
    from leave.models import LeaveRequest

    return {
        LeaveRequest: RequestKind(
            "leave",
            "Đơn nghỉ phép",
            _leave_state,
            ("status",),
            lambda source: _range(source, "start_date", "end_date"),
        ),
        ShiftRequest: RequestKind(
            "shift",
            "Đơn đổi ca",
            _boolean_state,
            BOOLEAN_FIELDS,
            lambda source: _range(source, "requested_date", "requested_till"),
        ),
        WorkTypeRequest: RequestKind(
            "work_type",
            "Đơn đổi loại hình làm việc",
            _boolean_state,
            BOOLEAN_FIELDS,
            lambda source: _range(source, "requested_date", "requested_till"),
        ),
        AttendanceLateEarlyRequest: RequestKind(
            "late_early",
            "Đơn đi muộn/về sớm",
            _boolean_state,
            BOOLEAN_FIELDS,
            lambda source: _one_day(source, "request_date"),
        ),
        OvertimeRequest: RequestKind(
            "overtime",
            "Đơn làm thêm giờ",
            _boolean_state,
            BOOLEAN_FIELDS,
            lambda source: _one_day(source, "request_date"),
        ),
        AttendanceExplanationRequest: RequestKind(
            "explanation",
            "Đơn giải trình",
            _boolean_state,
            BOOLEAN_FIELDS,
            lambda source: _one_day(source, "request_date"),
        ),
        RemoteWorkRequest: RequestKind(
            "remote",
            "Đơn làm việc từ xa",
            _boolean_state,
            BOOLEAN_FIELDS,
            lambda source: _range(source, "start_date", "end_date"),
        ),
    }


_REGISTRY = None


def registry():
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _kinds()
    return _REGISTRY


def kind_for(model):
    return registry().get(model)


def marker_for(kind, instance, status):
    """The value that makes one decision unrepeatable."""
    return f"{kind.slug}:{instance.pk}:{status}"


def owner_user(instance):
    """The account to notify, or None when the row has no reachable owner."""
    employee = getattr(instance, "employee_id", None)
    if employee is None:
        return None
    return getattr(employee, "employee_user_id", None)


def already_sent(instance, marker):
    """Whether this exact decision has already been announced.

    Identity comes from the generic `action_object` FK, as announcements do,
    and the marker itself is compared in Python rather than inside a JSON
    column lookup — the reminder passes learned that the hard way, because a
    `data__key=value` filter does not behave the same on SQLite and
    PostgreSQL and this project has already lost production to one.
    """
    from django.contrib.contenttypes.models import ContentType
    from notifications.models import Notification

    rows = Notification.objects.filter(
        action_object_content_type=ContentType.objects.get_for_model(
            instance, for_concrete_model=True
        ),
        # The generic-FK id column is a CharField.
        action_object_object_id=str(instance.pk),
    ).values_list("data", flat=True)
    return any((data or {}).get(MARKER_KEY) == marker for data in rows)


def payload(kind, instance, status):
    """The push/in-app data payload.

    Ids and a type only. Nothing about the request's content, the employee or
    the company goes over the wire — the app already has to be authenticated
    to read any of that, and a push is the one place it would be readable from
    a locked screen.
    """
    return {
        "type": PUSH_TYPES[status],
        "request_type": kind.slug,
        "request_id": str(instance.pk),
        "status": status,
    }


def _push_decision(user_pk, title, body, data):
    """Push to the owner's devices, best effort, after the commit."""
    from joydigi_api.push import send_to_user
    from joydigi_auth.models import JoydigiUser

    user = JoydigiUser.objects.filter(pk=user_pk).first()
    if user is None:
        return None
    try:
        return send_to_user(user, title, body, data=data)
    except Exception as error:  # pragma: no cover - transport guard
        logger.warning(
            "push request decision failed for user %s: %s",
            user_pk,
            type(error).__name__,
        )
        return None


def notify_request_decision(instance, status):
    """Announce one decision to the employee who filed the request.

    Returns True when an in-app row was written (and a push therefore
    registered), False when there was nothing to do — an unknown model, no
    owner, or a decision that has already been announced.

    Callable directly, not only from the signal, so a future write that
    bypasses `save()` can opt in with one line.
    """
    from notifications.signals import notify

    kind = kind_for(type(instance))
    if kind is None or status not in PUSH_TYPES:
        return False
    user = owner_user(instance)
    if user is None:
        return False

    marker = marker_for(kind, instance, status)
    if already_sent(instance, marker):
        return False

    body = kind.body(instance, status)
    data = payload(kind, instance, status)
    sent = notify.send(
        user,
        recipient=user,
        verb=body,
        action_object=instance,
        icon="document-text-outline",
        **{MARKER_KEY: marker},
        **data,
    )
    if not _notification_created(sent):
        # The recipient has notifications switched off. `joydigi_api.push`
        # does not check that preference itself, so not pushing here is what
        # honours it.
        return False

    title = TITLES[status]
    transaction.on_commit(partial(_push_decision, user.pk, title, body, data))
    return True


def _notification_created(send_result):
    """Whether `notify.send` actually wrote a row."""
    for _receiver, result in send_result or []:
        if result:
            return True
    return False


_PREVIOUS = "_request_decision_previous_state"


def _state_in_db(kind, instance):
    """The state the row has in the database right now, or None if it is new.

    Read through `_base_manager`: `objects` on every one of these models is a
    company-scoped manager, and a lookup that silently returns nothing for the
    wrong thread-local company would read as "this row was just created" and
    announce a decision twice.
    """
    if instance.pk is None:
        return None
    values = (
        type(instance)
        ._base_manager.filter(pk=instance.pk)
        .values(*kind.state_fields)
        .first()
    )
    return kind.state(values) if values is not None else None


def _remember_previous_state(sender, instance, **kwargs):
    kind = kind_for(sender)
    if kind is None:
        return
    if getattr(instance, _PREVIOUS, None) is not None:
        # A nested save of the same row, from inside another receiver that is
        # handling this very `post_save` — `leave/signals.py` does exactly
        # that for a one-day request whose two breakdowns disagree. Re-reading
        # the database here would find the state the outer save has already
        # written and overwrite "it used to be pending" with "it is approved",
        # and the decision would then be announced by nobody.
        #
        # Today this module's receiver happens to run before that one, so the
        # hole is not reachable; but that order is only the order of
        # INSTALLED_APPS, which is not something a notification should depend
        # on. Keeping the first remembered value makes the outcome the same
        # either way, and `_announce_decision` clears it, so an ordinary
        # second save of the same in-memory object still re-reads.
        return
    try:
        setattr(instance, _PREVIOUS, _state_in_db(kind, instance))
    except Exception as error:  # pragma: no cover - never block a save
        setattr(instance, _PREVIOUS, None)
        logger.warning(
            "could not read the previous state of %s: %s",
            sender.__name__,
            type(error).__name__,
        )


def _decided_by_owner(instance):
    """Whether the employee who filed the request is the one who just saved it.

    `JoydigiModel.save()` stamps `modified_by` from the authenticated user on
    the current request (`joydigi/models.py`), so this is the actor without
    having to thread a request through the signal.

    It matters because the four attendance request types and the two base ones
    carry a single `canceled` bit that an approver's "Từ chối" and the owner's
    own cancel both write. Without this check, cancelling your own pending
    request would push you "Đơn ... đã bị từ chối" about your own action. A
    missing `modified_by` — a scheduler, a management command, a direct save in
    a test — reads as "not the owner", which is the safe direction: the
    notification is still sent.
    """
    user = owner_user(instance)
    actor_pk = getattr(instance, "modified_by_id", None)
    return user is not None and actor_pk is not None and actor_pk == user.pk


def _announce_decision(sender, instance, created=False, **kwargs):
    """Notify on a real transition out of pending, and on nothing else.

    Wrapped so that a notification failure can never fail an approval: the
    manager's click has already changed the row, and refusing the whole save
    because a message could not be composed would be the worse outcome.
    """
    kind = kind_for(sender)
    if kind is None:
        return
    previous = getattr(instance, _PREVIOUS, None)
    setattr(instance, _PREVIOUS, None)
    if created or previous != PENDING:
        return
    try:
        current = kind.state(instance)
        if current in PUSH_TYPES and not _decided_by_owner(instance):
            notify_request_decision(instance, current)
    except Exception as error:  # pragma: no cover - never block a save
        logger.warning(
            "could not announce the decision on %s: %s",
            sender.__name__,
            type(error).__name__,
        )


def connect():
    """Wire the receivers. Called once, from `base.apps.BaseConfig.ready`.

    `dispatch_uid` makes this idempotent: `ready()` can run more than once in
    a test process, and a second connection would send every notification
    twice.
    """
    for model in registry():
        pre_save.connect(
            _remember_previous_state,
            sender=model,
            dispatch_uid=f"request_decision_pre_{model._meta.label_lower}",
        )
        post_save.connect(
            _announce_decision,
            sender=model,
            dispatch_uid=f"request_decision_post_{model._meta.label_lower}",
        )
