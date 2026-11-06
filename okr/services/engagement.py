"""Comments, evidence and timelines (JOYOKR ``JoyOKR_Engagement_Controller``).

Evidence files go to private storage and are only ever served through an
authorised download, never as a public ``/media/`` URL (JOYOKR stored them
in the public WordPress media library).
"""

from __future__ import annotations

import hashlib
import os

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q

from okr.models import OKRActivity, OKRCheckin, OKRComment, OKREvidence
from okr.models.choices import ActivityAction, EvidenceType, ReferenceType
from okr.services import activity, errors, notifications, okrs
from okr.services.access import OKRViewer, scope_of

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
_OLE = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)
_ZIP = (b"PK\x03\x04",)
# Extension -> (content type, accepted leading bytes). JOYOKR's list:
# JPEG, PNG, WebP, PDF, Word and Excel. The content must match the extension.
EVIDENCE_TYPES = {
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".webp": ("image/webp", (b"RIFF",)),
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".doc": ("application/msword", _OLE),
    ".xls": ("application/vnd.ms-excel", _OLE),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", _ZIP),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _ZIP),
}


def resolve_target(viewer: OKRViewer, objective_id=None, key_result_id=None):
    """``(reference type, item)`` for exactly one of the two ids, visibility checked."""
    if bool(objective_id) == bool(key_result_id):
        raise errors.validation({"target": "Chọn đúng một Objective hoặc một Key Result."})
    if objective_id:
        return ReferenceType.OBJECTIVE, okrs.visible_objective(viewer, objective_id)
    return ReferenceType.KEY_RESULT, okrs.visible_key_result(viewer, key_result_id)


def _target_filter(kind: str, item) -> dict:
    return {"objective": item} if kind == ReferenceType.OBJECTIVE else {"key_result": item}


def list_comments(viewer: OKRViewer, *, objective_id=None, key_result_id=None):
    kind, item = resolve_target(viewer, objective_id, key_result_id)
    return (
        OKRComment.objects.for_company(viewer.company_id)
        .filter(**_target_filter(kind, item))
        .select_related("author")
        .order_by("created_at", "id")
    )


def create_comment(viewer: OKRViewer, *, objective_id=None, key_result_id=None, content="", parent_id=None) -> OKRComment:
    if bool(objective_id) == bool(key_result_id):
        raise errors.validation({"target": "Chọn đúng một Objective hoặc một Key Result."})
    item = okrs.get_objective(viewer, objective_id) if objective_id else okrs.get_key_result(viewer, key_result_id)
    kind = ReferenceType.OBJECTIVE if objective_id else ReferenceType.KEY_RESULT
    if not viewer.can_view_scope(scope_of(item)):
        raise errors.forbidden()
    content = (content or "").strip()
    if not content:
        raise errors.validation({"content": "Nội dung bình luận là bắt buộc."})
    parent = None
    if parent_id:
        parent = (
            OKRComment.objects.for_company(viewer.company_id)
            .filter(pk=parent_id, parent__isnull=True, **_target_filter(kind, item))
            .first()
        )
        if parent is None:
            raise errors.validation({"parent": "Bình luận cha không hợp lệ."})
    with transaction.atomic():
        comment = OKRComment.objects.create(
            company_id=viewer.company_id,
            author=viewer.employee,
            parent=parent,
            content=content,
            **_target_filter(kind, item),
        )
        activity.log(
            viewer.employee, ActivityAction.COMMENTED, metadata={"comment_id": comment.pk}, **_target_filter(kind, item)
        )
        if item.owner_id and item.owner_id != viewer.employee_id:
            notifications.send(
                item.owner,
                company_id=viewer.company_id,
                notification_type="comment",
                title="Bình luận mới",
                message=content,
                reference_type=kind,
                reference_id=item.pk,
                actor=viewer.employee,
            )
    return comment


def list_activity(viewer: OKRViewer, *, objective_id=None, key_result_id=None):
    """Timeline of one objective or key result. An objective's timeline also
    carries its key results' events (the design's "Hoạt động" feed);
    JOYOKR showed objective-level events only."""
    kind, item = resolve_target(viewer, objective_id, key_result_id)
    rows = OKRActivity.objects.for_company(viewer.company_id)
    if kind == ReferenceType.OBJECTIVE:
        rows = rows.filter(Q(objective=item) | Q(key_result__objective=item))
    else:
        rows = rows.filter(key_result=item)
    return rows.select_related("actor", "key_result").order_by("-created_at", "-id")


def list_checkins(viewer: OKRViewer, key_result_id):
    key_result = okrs.visible_key_result(viewer, key_result_id)
    return OKRCheckin.objects.filter(key_result=key_result).select_related("author").order_by("-created_at", "-id")


def list_evidence(viewer: OKRViewer, key_result_id):
    key_result = okrs.visible_key_result(viewer, key_result_id)
    return OKREvidence.objects.filter(key_result=key_result).select_related("author").order_by("-created_at", "-id")


def _check_upload(upload) -> tuple:
    name = os.path.basename(getattr(upload, "name", "") or "")
    extension = os.path.splitext(name)[1].lower()
    if upload.size > MAX_EVIDENCE_BYTES:
        raise errors.validation({"file": "Tệp không được vượt quá 10 MB."})
    if extension not in EVIDENCE_TYPES:
        raise errors.validation({"file": "Định dạng tệp không được hỗ trợ."})
    content_type, signatures = EVIDENCE_TYPES[extension]
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    matches = any(head.startswith(signature) for signature in signatures)
    if extension == ".webp":
        matches = matches and head[8:12] == b"WEBP"
    if not matches:
        raise errors.validation({"file": "Định dạng tệp không được hỗ trợ."})
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return name[:255], content_type, digest.hexdigest()


def create_evidence(viewer: OKRViewer, key_result_id, *, url="", label="", upload=None, checkin_id=None) -> OKREvidence:
    key_result = okrs.get_key_result(viewer, key_result_id)
    if not viewer.can_edit_okr(scope_of(key_result)):
        raise errors.forbidden()
    url = (url or "").strip()
    label = (label or "").strip()
    values = {}
    if url:
        try:
            URLValidator(schemes=["http", "https"])(url)
        except ValidationError:
            raise errors.validation({"url": "Đường dẫn phải bắt đầu bằng http:// hoặc https://."})
        values.update(evidence_type=EvidenceType.URL, url=url)
    else:
        if upload is None:
            raise errors.validation({"file": "Vui lòng chọn tệp bằng chứng."})
        name, content_type, sha256 = _check_upload(upload)
        values.update(
            evidence_type=EvidenceType.FILE,
            file=upload,
            original_name=name,
            content_type=content_type,
            size=upload.size,
            sha256=sha256,
        )
        label = label or name
    if not label:
        raise errors.validation({"label": "Tên bằng chứng là bắt buộc."})
    checkin = None
    if checkin_id:
        checkin = OKRCheckin.objects.filter(pk=checkin_id, key_result=key_result).first()
        if checkin is None:
            raise errors.validation({"checkin": "Check-in liên kết không hợp lệ."})
    with transaction.atomic():
        evidence = OKREvidence.objects.create(
            company_id=viewer.company_id,
            key_result=key_result,
            checkin=checkin,
            author=viewer.employee,
            label=label[:255],
            **values,
        )
        activity.log(
            viewer.employee, ActivityAction.EVIDENCE_ADDED, key_result=key_result, metadata={"evidence_id": evidence.pk}
        )
    return evidence


def evidence_for_download(viewer: OKRViewer, evidence_id) -> OKREvidence:
    """A file evidence the viewer may open; anything else is "not found"."""
    evidence = (
        OKREvidence.objects.for_company(viewer.company_id)
        .filter(pk=evidence_id, evidence_type=EvidenceType.FILE)
        .first()
    )
    if evidence is None:
        raise errors.not_found("evidence")
    okrs.visible_key_result(viewer, evidence.key_result_id)
    return evidence
