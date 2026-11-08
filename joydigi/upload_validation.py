"""Shared checks for files people upload to HR (attachments, comments, policies).

Audit 2026-11-08 (F-04). Horilla's attachment ``FileField``s accepted any file,
and nginx serves ``/media/`` from the site's own origin, so an uploaded
``.html`` or ``.svg`` could run script as checkin.joydigi.net. Only a few views
had their own extension blocklist (``employee/cbv/document_request.py``,
``employee/views.py``). This module is the one rule for the rest:

* only the document, image and archive types HR actually exchanges;
* the content must start with the bytes of its type where the type has a
  signature (images, PDF, Office, archives), so ``x.html`` renamed ``x.jpg``
  is refused;
* a size cap, ``HR_UPLOAD_MAX_BYTES`` (default 20 MB; nginx allows 100 MB).

``validate_upload`` is a Django validator (model fields, forms, DRF
serializers). ``upload_rejection`` returns the message for views that save
``request.FILES`` directly. OKR evidence keeps its own stricter rule
(``okr/services/engagement.py``), and profile pictures stay ``ImageField``.
"""

from __future__ import annotations

import os
from typing import Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

DEFAULT_MAX_BYTES = 20 * 1024 * 1024

_OLE = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)
_ZIP = (b"PK\x03\x04", b"PK\x05\x06")
_JPEG = (b"\xff\xd8\xff",)

# Extension -> accepted leading bytes; ``None`` means a plain-text type with no
# signature (it is still refused if it starts like an HTML/SVG/XML document).
ALLOWED_UPLOAD_TYPES = {
    ".jpg": _JPEG,
    ".jpeg": _JPEG,
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
    ".bmp": (b"BM",),
    ".heic": ("ftyp",),
    ".heif": ("ftyp",),
    ".pdf": (b"%PDF-",),
    ".doc": _OLE,
    ".xls": _OLE,
    ".ppt": _OLE,
    ".docx": _ZIP,
    ".xlsx": _ZIP,
    ".pptx": _ZIP,
    ".odt": _ZIP,
    ".ods": _ZIP,
    ".odp": _ZIP,
    ".zip": _ZIP,
    ".rar": (b"Rar!\x1a\x07",),
    ".7z": (b"7z\xbc\xaf\x27\x1c",),
    ".rtf": (b"{\\rtf",),
    ".txt": None,
    ".csv": None,
}

# Leading text that makes a browser treat a "text" file as markup.
_MARKUP_STARTS = (b"<!doctype", b"<html", b"<svg", b"<?xml", b"<script", b"<body", b"<head", b"<iframe")


def _max_bytes() -> int:
    return int(getattr(settings, "HR_UPLOAD_MAX_BYTES", DEFAULT_MAX_BYTES))


def _head(upload, length: int = 64) -> bytes:
    try:
        position = upload.tell()
    except (AttributeError, OSError, ValueError):
        position = None
    try:
        upload.seek(0)
        head = upload.read(length) or b""
    finally:
        try:
            upload.seek(position or 0)
        except (AttributeError, OSError, ValueError):
            pass
    return head if isinstance(head, bytes) else head.encode("utf-8", "ignore")


def _signature_matches(extension: str, head: bytes) -> bool:
    signatures = ALLOWED_UPLOAD_TYPES[extension]
    if signatures is None:
        stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        return not stripped.startswith(_MARKUP_STARTS)
    if signatures == ("ftyp",):  # HEIC/HEIF: ISO box, "ftyp" at offset 4
        return head[4:8] == b"ftyp"
    if not any(head.startswith(signature) for signature in signatures):
        return False
    if extension == ".webp":
        return head[8:12] == b"WEBP"
    return True


def upload_rejection(upload) -> Optional[str]:
    """Why ``upload`` must be refused, or ``None`` when it is acceptable.

    Only new uploads are checked: a file already stored (a ``FieldFile`` with
    no ``read`` of its own content in memory) is left alone, so editing a
    record that holds an old attachment keeps working."""
    if upload is None or not getattr(upload, "name", ""):
        return None
    if not hasattr(upload, "content_type") and getattr(upload, "_committed", True):
        return None  # an existing stored file, not a fresh upload
    name = os.path.basename(upload.name)
    extension = os.path.splitext(name)[1].lower()
    if extension not in ALLOWED_UPLOAD_TYPES:
        return f"Không cho phép tải lên tệp {extension or 'không có đuôi'}."
    size = getattr(upload, "size", None)
    if size is not None and size > _max_bytes():
        return f"Tệp vượt quá {_max_bytes() // (1024 * 1024)} MB."
    if not _signature_matches(extension, _head(upload)):
        return f"Nội dung tệp không đúng định dạng {extension}."
    return None


@deconstructible
class UploadValidator:
    """Model/form/serializer validator; deconstructible for migrations."""

    def __call__(self, upload):
        reason = upload_rejection(upload)
        if reason:
            raise ValidationError(reason, code="invalid_upload")

    def __eq__(self, other):
        return isinstance(other, UploadValidator)

    def __hash__(self):
        return hash(UploadValidator)


validate_upload = UploadValidator()


_GUARDED_FIELDS = {}


def _guarded_fields(model):
    """Names of ``model``'s file fields that carry ``validate_upload``."""
    if model not in _GUARDED_FIELDS:
        _GUARDED_FIELDS[model] = tuple(
            field.attname
            for field in model._meta.concrete_fields
            if any(isinstance(validator, UploadValidator) for validator in getattr(field, "validators", ()))
        )
    return _GUARDED_FIELDS[model]


def _check_before_save(sender, instance, raw=False, **kwargs):
    """Safety net for code that assigns an upload and calls ``save()`` without
    ``full_clean()`` (Horilla does this in several form ``save()`` methods):
    a refused file never reaches storage."""
    if raw:
        return
    for attname in _guarded_fields(sender):
        value = getattr(instance, attname, None)
        if value and not getattr(value, "_committed", True):
            reason = upload_rejection(value)
            if reason:
                raise ValidationError({attname: reason}, code="invalid_upload")


def connect_upload_guard():
    """Called once from ``base.apps.BaseConfig.ready``."""
    from django.db.models.signals import pre_save

    pre_save.connect(_check_before_save, dispatch_uid="joydigi_upload_guard")


def accepted_uploads(request, files):
    """``files`` without the refused ones; each refusal is shown as a message."""
    from django.contrib import messages

    accepted = []
    for upload in files:
        reason = upload_rejection(upload)
        if reason:
            messages.error(request, f"{os.path.basename(upload.name)}: {reason}")
        else:
            accepted.append(upload)
    return accepted
