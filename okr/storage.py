"""Private storage for OKR evidence files.

Evidence is business material, so it must not land under ``MEDIA_ROOT``: the
production Nginx config documented in the repo serves ``/media/`` directly,
with no permission check. Files go to ``OKR_PRIVATE_MEDIA_ROOT`` (default
``<BASE_DIR>/private_media/okr``), the storage refuses to build public URLs,
and downloads have to go through an authorised view.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils import timezone


class PrivateEvidenceStorage(FileSystemStorage):
    """File system storage rooted outside the public media tree.

    The location is read from settings on every access (not cached) so
    ``override_settings`` works in tests.
    """

    def __init__(self):
        super().__init__(base_url=None)

    @property
    def base_location(self):
        default = Path(settings.BASE_DIR) / "private_media" / "okr"
        return str(getattr(settings, "OKR_PRIVATE_MEDIA_ROOT", default))

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError(
            "OKR evidence has no public URL; serve it through the authorised download view."
        )


_evidence_storage = PrivateEvidenceStorage()


def evidence_storage():
    """Callable used by the model field, so migrations reference this function
    rather than a serialised storage instance."""
    return _evidence_storage


def evidence_upload_to(instance, filename):
    """Random file name grouped by company and month.

    The uploader's file name is kept in ``OKREvidence.original_name`` and
    never becomes part of the path.
    """
    ext = os.path.splitext(filename)[1].lower()[:10]
    now = timezone.now()
    # The file is named before save() copies the company from the key result.
    company_id = instance.company_id or (
        instance.key_result.company_id if instance.key_result_id else "unscoped"
    )
    return f"evidence/{company_id}/{now:%Y}/{now:%m}/{uuid4().hex}{ext}"
