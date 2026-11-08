"""Audit 2026-11-08 (F-04): HR attachments refuse active content and fakes.

Before the fix, Horilla's attachment FileFields accepted any file and nginx
served it from /media/ on the site's own origin, so an uploaded .html/.svg
could run script as checkin.joydigi.net.
"""

from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework import serializers

from attendance.models import AttendanceRequestFile
from base.models import Attachment, BaserequestFile
from employee.models import DisciplinaryAction, NoteFiles, PolicyMultipleFile
from joydigi.upload_validation import (
    UploadValidator,
    accepted_uploads,
    upload_rejection,
    validate_upload,
)
from leave.models import LeaveAllocationRequest, LeaverequestFile, LeaveRequest

PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
DOCX = b"PK\x03\x04" + b"\x00" * 32
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 16
HTML = b"<!DOCTYPE html><script>alert(document.cookie)</script>"


def upload(name, content, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, content, content_type=content_type)


class UploadRuleTests(SimpleTestCase):
    def test_accepts_the_types_hr_exchanges(self):
        for name, content in [
            ("don.pdf", PDF),
            ("anh.png", PNG),
            ("anh.JPG", JPEG),
            ("hop-dong.docx", DOCX),
            ("iphone.heic", HEIC),
            ("ghi-chu.txt", b"Xin nghi phep ngay 10/11"),
            ("bang.csv", b"ma,ten\n1,An\n"),
        ]:
            with self.subTest(name=name):
                self.assertIsNone(upload_rejection(upload(name, content)))

    def test_refuses_active_content_and_unknown_types(self):
        for name, content in [
            ("trang.html", HTML),
            ("trang.htm", HTML),
            ("logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'),
            ("x.js", b"alert(1)"),
            ("setup.exe", b"MZ\x90\x00"),
            ("khongduoi", b"abc"),
            ("x.php", b"<?php echo 1;"),
        ]:
            with self.subTest(name=name):
                self.assertIsNotNone(upload_rejection(upload(name, content)))

    def test_refuses_content_that_does_not_match_the_extension(self):
        for name, content in [
            ("anh.jpg", HTML),
            ("don.pdf", HTML),
            ("hop-dong.docx", PDF),
            ("ghi-chu.txt", HTML),
            ("bang.csv", b"  <html><body>x</body></html>"),
        ]:
            with self.subTest(name=name):
                self.assertIn("không đúng định dạng", upload_rejection(upload(name, content)))

    @override_settings(HR_UPLOAD_MAX_BYTES=10)
    def test_refuses_files_over_the_size_cap(self):
        self.assertIn("vượt quá", upload_rejection(upload("don.pdf", PDF)))

    def test_validator_raises_validation_error(self):
        with self.assertRaises(ValidationError):
            validate_upload(upload("trang.html", HTML))
        validate_upload(upload("don.pdf", PDF))

    def test_the_file_position_is_left_where_it_was(self):
        item = upload("don.pdf", PDF)
        item.seek(3)
        upload_rejection(item)
        self.assertEqual(item.tell(), 3)


class AttachmentFieldsCarryTheRuleTests(SimpleTestCase):
    FIELDS = [
        (BaserequestFile, "file"),
        (Attachment, "file"),
        (NoteFiles, "files"),
        (PolicyMultipleFile, "attachment"),
        (DisciplinaryAction, "attachment"),
        (LeaveRequest, "attachment"),
        (LeaverequestFile, "file"),
        (LeaveAllocationRequest, "attachment"),
        (AttendanceRequestFile, "file"),
    ]

    def test_every_attachment_field_has_the_validator(self):
        for model, field in self.FIELDS:
            with self.subTest(model=model.__name__):
                validators = model._meta.get_field(field).validators
                self.assertTrue(any(isinstance(v, UploadValidator) for v in validators))

    def test_drf_model_serializers_inherit_the_rule(self):
        class LeaveAttachmentSerializer(serializers.ModelSerializer):
            class Meta:
                model = LeaveRequest
                fields = ["attachment"]

        bad = LeaveAttachmentSerializer(data={"attachment": upload("trang.html", HTML)}, partial=True)
        self.assertFalse(bad.is_valid())
        self.assertIn("attachment", bad.errors)
        good = LeaveAttachmentSerializer(data={"attachment": upload("don.pdf", PDF)}, partial=True)
        self.assertTrue(good.is_valid(), good.errors)


class SaveGuardTests(TestCase):
    """Views that assign request.FILES and call save() without full_clean()."""

    def test_html_upload_is_refused_on_save(self):
        row = AttendanceRequestFile(file=upload("trang.html", HTML))
        with self.assertRaises(ValidationError):
            row.save()
        self.assertFalse(AttendanceRequestFile.objects.exists())

    def test_fake_image_is_refused_on_save(self):
        row = BaserequestFile(file=upload("anh.jpg", HTML))
        with self.assertRaises(ValidationError):
            row.save()

    def test_valid_upload_is_saved_and_resaving_does_not_recheck(self):
        row = BaserequestFile(file=upload("don.pdf", PDF))
        row.save()
        self.assertTrue(row.file.name.endswith(".pdf"))
        row.save()  # committed file: no new check, no error
        self.assertEqual(BaserequestFile.objects.count(), 1)
        row.file.delete(save=False)


class AcceptedUploadsTests(SimpleTestCase):
    def test_refused_files_are_dropped_with_a_message(self):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: HttpResponse()).process_request(request)
        request._messages = FallbackStorage(request)
        kept = accepted_uploads(request, [upload("don.pdf", PDF), upload("trang.html", HTML)])
        self.assertEqual([item.name for item in kept], ["don.pdf"])
        texts = [str(message) for message in get_messages(request)]
        self.assertEqual(len(texts), 1)
        self.assertIn("trang.html", texts[0])
