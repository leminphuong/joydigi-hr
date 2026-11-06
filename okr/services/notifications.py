"""In-app OKR notifications (JOYOKR ``JoyOKR_Notification_Service`` and
``JoyOKR_Notification_Repository``).

Only the in-app channel is ported. JOYOKR's e-mail and Telegram delivery
(check-in reminders) are not: the Telegram bot token is a secret this module
deliberately does not store, and push delivery belongs to the HR
notification app.
"""

from __future__ import annotations

import datetime
from typing import Iterable, Optional

from django.db import IntegrityError, transaction

from okr.models import OKRKeyResult, OKRNotification, OKRObjective
from okr.models.base import employee_company_id
from okr.models.choices import ApprovalStatus, ReferenceType
from okr.services import errors
from okr.services.access import OKRViewer, scope_of

APPROVAL_REQUESTED = "approval_requested"


def stamp(moment) -> str:
    """Digits of a timestamp, as JOYOKR puts ``updated_at`` in dedupe keys."""
    if moment is None:
        return "current"
    return moment.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S%f")


def send(
    recipient,
    *,
    company_id: int,
    notification_type: str,
    title: str,
    message: str = "",
    reference_type: str = "",
    reference_id: Optional[int] = None,
    dedupe_key: Optional[str] = None,
    actor=None,
) -> Optional[OKRNotification]:
    """Store one notification; with a dedupe key, an existing one is returned instead.

    ``actor`` (F7) is the employee who caused it. It is informational only:
    one outside the company is dropped rather than failing the action.
    """
    if recipient is None:
        return None
    if actor is not None and employee_company_id(actor) != company_id:
        actor = None
    values = {
        "company_id": company_id,
        "recipient": recipient,
        "actor": actor,
        "notification_type": notification_type[:40],
        "title": (title or "Thông báo OKR").strip()[:255],
        "message": message or "",
        "reference_type": reference_type or "",
        "reference_id": reference_id or None,
        "dedupe_key": (dedupe_key or "")[:190] or None,
    }
    if values["dedupe_key"]:
        existing = OKRNotification.objects.for_company(company_id).filter(
            dedupe_key=values["dedupe_key"]
        ).first()
        if existing is not None:
            return existing
        try:
            with transaction.atomic():
                return OKRNotification.objects.create(**values)
        except IntegrityError:
            return OKRNotification.objects.for_company(company_id).get(dedupe_key=values["dedupe_key"])
    return OKRNotification.objects.create(**values)


def mark_reference_read(company_id: int, reference_type: str, reference_id: int, notification_type: str = "") -> int:
    """Settle every notification about one objective/KR (all recipients)."""
    rows = OKRNotification.objects.for_company(company_id).filter(
        reference_type=reference_type, reference_id=reference_id, is_read=False
    )
    if notification_type:
        rows = rows.filter(notification_type=notification_type)
    return rows.update(is_read=True)


def for_viewer(viewer: OKRViewer):
    return OKRNotification.objects.for_company(viewer.company_id).filter(recipient_id=viewer.employee_id)


def mark_read(viewer: OKRViewer, notification_id: int) -> OKRNotification:
    notification = for_viewer(viewer).filter(pk=notification_id).first()
    if notification is None:
        raise errors.not_found("notification")
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=["is_read"])
    return notification


def mark_all_read(viewer: OKRViewer) -> int:
    return for_viewer(viewer).filter(is_read=False).update(is_read=True)


def unread_count(viewer: OKRViewer) -> int:
    return for_viewer(viewer).filter(is_read=False).count()


def _live_target(viewer: OKRViewer, reference_type: str, reference_id: int):
    if reference_type == ReferenceType.OBJECTIVE:
        return (
            OKRObjective.objects.for_company(viewer.company_id)
            .select_related("cycle")
            .filter(pk=reference_id)
            .first()
        )
    if reference_type == ReferenceType.KEY_RESULT:
        return (
            OKRKeyResult.objects.for_company(viewer.company_id)
            .select_related("objective__cycle")
            .filter(pk=reference_id, objective__deleted_at__isnull=True)
            .first()
        )
    return None


def reference_availability(viewer: OKRViewer, notifications: Iterable[OKRNotification]) -> dict:
    """``{notification id: available}`` for notifications that have a target.

    Same rules as JOYOKR ``decorate_notification_references``: a target that
    was deleted, or that the viewer can no longer see, is unavailable. An
    approval request the viewer can no longer act on is marked read as a
    side effect, so it stops counting as unread.
    """
    availability = {}
    settle = []
    for item in notifications:
        if not item.reference_type or not item.reference_id:
            continue
        live = _live_target(viewer, item.reference_type, item.reference_id)
        if live is None:
            availability[item.pk] = False
            if item.notification_type == APPROVAL_REQUESTED and not item.is_read:
                settle.append(item)
            continue
        ref = scope_of(live)
        availability[item.pk] = viewer.can_view_scope(ref)
        if item.notification_type == APPROVAL_REQUESTED and not item.is_read:
            pending = ref.approval_status == ApprovalStatus.SUBMITTED and viewer.can_review(ref)
            if not pending:
                settle.append(item)
    if settle:
        OKRNotification.objects.filter(pk__in=[item.pk for item in settle]).update(is_read=True)
        for item in settle:
            item.is_read = True
    return availability


def send_checkin_reminders(company_id: int, now=None) -> dict:
    """JOYOKR ``JoyOKR_Cron::run_reminders``, in-app channel only.

    Every approved key result in an active cycle whose last check-in (or,
    with none, its creation) is older than the reminder interval gets one
    reminder per interval window; the dedupe key makes reruns harmless.
    """
    from django.db.models import Max
    from django.db.models.functions import Coalesce
    from django.utils import timezone

    from okr.models.choices import CycleStatus
    from okr.services.reports import Policy

    now = now or timezone.now()
    days = Policy.for_company(company_id).reminder_days
    cutoff = now - datetime.timedelta(days=days)
    window = int(now.timestamp() // (max(1, days) * 86400))
    due = (
        OKRKeyResult.objects.for_company(company_id)
        .filter(
            approval_status=ApprovalStatus.APPROVED,
            objective__approval_status=ApprovalStatus.APPROVED,
            objective__deleted_at__isnull=True,
            objective__cycle__status=CycleStatus.ACTIVE,
            owner__isnull=False,
        )
        .annotate(last_checkin=Coalesce(Max("checkins__created_at"), "created_at"))
        .filter(last_checkin__lt=cutoff)
        .select_related("objective", "owner")
        .order_by("id")
    )
    summary = {"checked": 0, "sent": 0}
    for key_result in due:
        summary["checked"] += 1
        overdue_days = max(1, int((now - key_result.last_checkin).total_seconds() // 86400) - days)
        last = timezone.localtime(key_result.last_checkin).strftime("%d/%m/%Y %H:%M")
        dedupe_key = f"reminder:{key_result.pk}:{window}"
        existed = OKRNotification.objects.for_company(company_id).filter(dedupe_key=dedupe_key).exists()
        send(
            key_result.owner,
            company_id=company_id,
            notification_type="checkin_reminder",
            title="Đến hạn check-in Key Result",
            message=(
                f"KR “{key_result.title}” thuộc Objective “{key_result.objective.title}” đã quá hạn "
                f"check-in {overdue_days} ngày. Lần cập nhật cuối: {last}."
            ),
            reference_type=ReferenceType.KEY_RESULT,
            reference_id=key_result.pk,
            dedupe_key=dedupe_key,
        )
        summary["sent"] += not existed
    return summary
