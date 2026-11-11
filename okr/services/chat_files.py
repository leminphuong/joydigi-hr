"""OKR chat attachments: what a file must look like inside, and cleanup of
uploads never sent (2026-11-11).

Uploads go through ``engagement._check_upload`` first (extension allow-list,
10 MB, magic bytes). For chat that is not enough — anyone in a room opens the
file — so the content is checked too:

* images (jpg / png / webp): decoded with Pillow, the real format must match
  the extension, at most ``MAX_IMAGE_PIXELS`` (decompression bombs);
* docx / xlsx: a real OOXML package (``[Content_Types].xml`` and its main
  part), no macros (``vbaProject.bin``) and no ActiveX, and not a zip bomb;
* doc / xls: no VBA macro storage;
* pdf: no JavaScript, launch actions or embedded files;
* the name: no control or bidirectional-override characters (they can
  disguise ``tai-lieu<RLO>fdp.exe`` as a PDF), never empty.

Nothing here makes a file safe to *run*: downloads stay ``attachment``
(images may be shown inline, sandboxed, ``nosniff``) — see chat_views.

Cleanup: a file uploaded but never attached to a message is visible to its
uploader only and counts toward their 20 pending files. Older than the grace
period (default 24 h, never under 1 h) it is deleted — row first, inside a
transaction that re-checks it is still unsent, the stored file after commit.
A file attached to a message is never touched.
"""

import datetime
import io
import logging
import os
import re
import unicodedata
import zipfile

from django.db import transaction
from django.utils import timezone

from okr.services import errors
from okr.services.engagement import _check_upload

logger = logging.getLogger(__name__)

MAX_IMAGE_PIXELS = 40_000_000  # e.g. 8000 x 5000
MAX_ZIP_ENTRIES = 3000
MAX_ZIP_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ZIP_RATIO = 200
MIN_CLEANUP_AGE = datetime.timedelta(hours=1)
DEFAULT_CLEANUP_AGE = datetime.timedelta(hours=24)

_IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
_OOXML_MAIN = {".docx": "word/", ".xlsx": "xl/"}
_ZIP_FORBIDDEN = re.compile(r"(^|/)(vbaproject\.bin|vbadata\.xml)$|(^|/)activex/", re.IGNORECASE)
_OLE_MACROS = ("_VBA_PROJECT".encode("utf-16-le"), "Macros".encode("utf-16-le"), "_VBA_PROJECT_CUR".encode("utf-16-le"))
_PDF_ACTIVE = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFiles?|RichMedia)(?![A-Za-z])")
_NAME_DROP = re.compile("[\u0000-\u001f\u007f​-‏‪-‮⁦-⁩﻿]")

REJECTED = "Tệp không hợp lệ hoặc có nội dung không được phép (macro, mã chạy, tệp nhúng)."


def clean_name(name: str) -> str:
    """The display name without control / bidi-override characters."""
    name = unicodedata.normalize("NFC", name or "")
    name = _NAME_DROP.sub("", name)
    name = " ".join(name.split()).strip(" .")
    stem, extension = os.path.splitext(name)
    return (stem or "tep") + extension.lower()


def check_chat_upload(upload) -> tuple:
    """``(name, content_type, sha256)`` for a chat upload, or a validation error."""
    name, content_type, sha256 = _check_upload(upload)
    name = clean_name(name)
    extension = os.path.splitext(name)[1].lower()
    upload.seek(0)
    data = upload.read()
    upload.seek(0)
    try:
        if extension in _IMAGE_FORMATS:
            _check_image(data, _IMAGE_FORMATS[extension])
        elif extension in _OOXML_MAIN:
            _check_ooxml(data, _OOXML_MAIN[extension])
        elif extension in (".doc", ".xls"):
            if any(marker in data for marker in _OLE_MACROS):
                raise ValueError("ole macros")
        elif extension == ".pdf":
            if _PDF_ACTIVE.search(data):
                raise ValueError("pdf active content")
    except errors.OKRError:
        raise
    except Exception as exc:  # any parser failure means "not that kind of file"
        logger.info("okr chat: upload rejected (%s, %s)", extension, type(exc).__name__)
        raise errors.validation({"file": REJECTED})
    return name[:255], content_type, sha256


def _check_image(data: bytes, expected: str):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        if image.format != expected:
            raise ValueError(f"format {image.format} is not {expected}")
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise errors.validation({"file": "Ảnh quá lớn (tối đa khoảng 40 triệu điểm ảnh)."})
        image.verify()


def _check_ooxml(data: bytes, main_part: str):
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        entries = package.infolist()
        if not entries or len(entries) > MAX_ZIP_ENTRIES:
            raise ValueError("entry count")
        names = {entry.filename for entry in entries}
        if "[Content_Types].xml" not in names or not any(n.startswith(main_part) for n in names):
            raise ValueError("not an office package")
        total = 0
        for entry in entries:
            if _ZIP_FORBIDDEN.search(entry.filename) or entry.filename.startswith(("/", "..")) or "/../" in entry.filename:
                raise ValueError("forbidden part")
            total += entry.file_size
            if entry.compress_size and entry.file_size / entry.compress_size > MAX_ZIP_RATIO and entry.file_size > 1_000_000:
                raise ValueError("compression ratio")
        if total > MAX_ZIP_UNCOMPRESSED:
            raise ValueError("uncompressed size")


# -- cleanup of uploads never sent -----------------------------------------------------


def stale_uploads(older_than=DEFAULT_CLEANUP_AGE, *, company_ids=None, now=None):
    """Attachments never attached to a message and older than ``older_than``."""
    from okr.models import OKRChatAttachment

    if older_than < MIN_CLEANUP_AGE:
        raise ValueError(f"older_than must be at least {MIN_CLEANUP_AGE}")
    cutoff = (now or timezone.now()) - older_than
    rows = OKRChatAttachment.objects.filter(message__isnull=True, created_at__lt=cutoff)
    if company_ids:
        rows = rows.filter(company_id__in=company_ids)
    return rows.order_by("id")


def cleanup_stale_uploads(older_than=DEFAULT_CLEANUP_AGE, *, dry_run=True, company_ids=None, limit=500, now=None) -> dict:
    """Delete (or, dry run, list) uploads never sent. Returns a summary."""
    from okr.models import OKRChatAttachment
    from okr.storage import evidence_storage

    now = now or timezone.now()
    candidates = list(
        stale_uploads(older_than, company_ids=company_ids, now=now).values_list(
            "id", "company_id", "uploader_id", "size", "created_at"
        )[:limit]
    )
    summary = {"found": len(candidates), "deleted": 0, "skipped": 0, "bytes": 0, "dry_run": dry_run, "rows": candidates}
    if dry_run:
        summary["bytes"] = sum(row[3] or 0 for row in candidates)
        return summary
    cutoff = now - older_than
    storage = evidence_storage()
    for attachment_id, _company, _uploader, size, _created in candidates:
        with transaction.atomic():
            item = (
                OKRChatAttachment.objects.select_for_update()
                .filter(pk=attachment_id, message__isnull=True, created_at__lt=cutoff)
                .first()
            )
            if item is None:  # sent (or deleted) meanwhile: leave it
                summary["skipped"] += 1
                continue
            name = item.file.name
            item.delete()
            transaction.on_commit(lambda name=name: _delete_file(storage, name))
        summary["deleted"] += 1
        summary["bytes"] += size or 0
        logger.info("okr chat cleanup: deleted unsent attachment %s (%s bytes)", attachment_id, size)
    return summary


def _delete_file(storage, name):
    try:
        storage.delete(name)
    except OSError:
        logger.warning("okr chat cleanup: could not delete a stored file")
