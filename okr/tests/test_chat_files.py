"""OKR chat — file content checks, unsent uploads, pilot group (2026-11-11).

Fake data only, private media in a temporary folder.
"""

import io
import os
import zipfile
from datetime import timedelta
from io import StringIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from okr.models import OKRChatAttachment, OKRNotification
from okr.tests.test_chat import ChatFixtures, png


def _image_bytes(fmt, size=(4, 4), mode="RGB"):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new(mode, size).save(buffer, format=fmt)
    return buffer.getvalue()


def _office(main_part, extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr(main_part, "<doc/>")
        for name, content in (extra or {}).items():
            package.writestr(name, content)
    return buffer.getvalue()


def upload_file(name, data, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, data, content_type=content_type)


class ChatFileContentTests(ChatFixtures):
    def setUp(self):
        super().setUp()
        self.room = self.open(self.alice, "team", self.team_a)["id"]

    def upload(self, file, expect=201, employee=None):
        return self.api(employee or self.alice, "post", "attachments/", {"room_id": self.room, "file": file},
                        expect=expect, fmt="multipart")

    def test_real_files_are_accepted(self):
        for name, data in (
            ("anh.png", _image_bytes("PNG")),
            ("anh.jpg", _image_bytes("JPEG")),
            ("anh.webp", _image_bytes("WEBP")),
            ("bao-cao.docx", _office("word/document.xml")),
            ("so-lieu.xlsx", _office("xl/workbook.xml")),
            ("ke-hoach.pdf", b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF"),
            ("cu.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64),
        ):
            with self.subTest(name=name):
                self.upload(upload_file(name, data))

    def test_disguised_or_active_content_is_refused(self):
        cases = {
            "signature only": ("anh.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
            "jpeg named png": ("anh.png", _image_bytes("JPEG")),
            "macro docx": ("bao-cao.docx", _office("word/document.xml", {"word/vbaProject.bin": b"VBA"})),
            "activex xlsx": ("so-lieu.xlsx", _office("xl/workbook.xml", {"xl/activeX/activeX1.xml": "<x/>"})),
            "zip, not office": ("bao-cao.docx", _office("readme.txt")),
            "pdf javascript": ("ke-hoach.pdf", b"%PDF-1.4\n<< /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >>"),
            "pdf launch": ("ke-hoach.pdf", b"%PDF-1.4\n<< /S /Launch /F (cmd.exe) >>"),
            "doc with macros": ("cu.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + "_VBA_PROJECT".encode("utf-16-le")),
            "html": ("trang.html", b"<script>alert(1)</script>"),
            "svg": ("anh.svg", b"<svg onload=alert(1)/>"),
        }
        for label, (name, data) in cases.items():
            with self.subTest(label):
                self.upload(upload_file(name, data), expect=400)
        self.assertFalse(OKRChatAttachment.objects.exists(), "nothing refused was stored")

    def test_huge_images_are_refused(self):
        self.upload(upload_file("rong.png", _image_bytes("PNG", size=(9000, 5000), mode="1")), expect=400)

    def test_names_lose_control_and_bidi_characters(self):
        uploaded = self.upload(upload_file("hop-dong‮gnp.png", _image_bytes("PNG")))
        self.assertEqual(uploaded["name"], "hop-donggnp.png")
        stored = OKRChatAttachment.objects.get(pk=uploaded["id"])
        self.assertNotIn("‮", stored.original_name)

    def test_a_pending_upload_can_be_dropped_by_its_uploader_only(self):
        mine = self.upload(png())
        path = OKRChatAttachment.objects.get(pk=mine["id"]).file.path
        self.api(self.bob, "delete", f"attachments/{mine['id']}/", expect=404)
        self.api(self.carol, "delete", f"attachments/{mine['id']}/", expect=404)
        self.api(self.alice, "delete", f"attachments/{mine['id']}/", expect=204)
        self.assertFalse(OKRChatAttachment.objects.filter(pk=mine["id"]).exists())
        self.assertFalse(os.path.exists(path), "the stored file goes too")
        sent = self.upload(png())
        self.send(self.alice, self.room, "", attachment_ids=[sent["id"]])
        self.api(self.alice, "delete", f"attachments/{sent['id']}/", expect=404)
        self.assertTrue(OKRChatAttachment.objects.filter(pk=sent["id"]).exists(), "a sent file is never dropped here")


class ChatUploadCleanupTests(ChatFixtures):
    def setUp(self):
        super().setUp()
        self.room = self.open(self.alice, "team", self.team_a)["id"]

    def make(self, hours_old, sent=False):
        item = self.api(self.alice, "post", "attachments/", {"room_id": self.room, "file": png()}, expect=201,
                        fmt="multipart")
        if sent:
            self.send(self.alice, self.room, "", attachment_ids=[item["id"]])
        OKRChatAttachment.objects.filter(pk=item["id"]).update(created_at=timezone.now() - timedelta(hours=hours_old))
        return OKRChatAttachment.objects.get(pk=item["id"])

    def run_cleanup(self, *args):
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("okr_chat_cleanup_uploads", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_lists_and_deletes_nothing(self):
        old = self.make(48)
        out = self.run_cleanup()
        self.assertIn(f"UNSENT  attachment={old.pk}", out)
        self.assertNotIn("anh.png", out, "no file names in the output")
        self.assertTrue(OKRChatAttachment.objects.filter(pk=old.pk).exists())

    def test_only_old_unsent_files_are_deleted(self):
        old = self.make(48)
        recent = self.make(2)
        sent = self.make(72, sent=True)
        path = old.file.path
        out = self.run_cleanup("--apply", "--expect-count", "1")
        self.assertIn("deleted: 1", out)
        self.assertFalse(OKRChatAttachment.objects.filter(pk=old.pk).exists())
        self.assertFalse(os.path.exists(path))
        self.assertTrue(OKRChatAttachment.objects.filter(pk=recent.pk).exists(), "inside the grace period")
        self.assertTrue(OKRChatAttachment.objects.filter(pk=sent.pk).exists(), "a sent file is never deleted")
        self.assertTrue(os.path.exists(sent.file.path))

    def test_guards(self):
        self.make(48)
        with self.assertRaisesMessage(CommandError, "needs --expect-count"):
            self.run_cleanup("--apply")
        with self.assertRaisesMessage(CommandError, "--expect-count 5 but 1 found"):
            self.run_cleanup("--apply", "--expect-count", "5")
        with self.assertRaisesMessage(CommandError, "at least 1"):
            self.run_cleanup("--older-than-hours", "0.5")
        self.assertEqual(OKRChatAttachment.objects.count(), 1)

    def test_a_file_sent_between_dry_run_and_delete_is_kept(self):
        from okr.services import chat_files

        old = self.make(48)
        self.send(self.alice, self.room, "", attachment_ids=[old.pk])  # sent meanwhile
        result = chat_files.cleanup_stale_uploads(dry_run=False)
        self.assertEqual(result["deleted"], 0)
        self.assertTrue(OKRChatAttachment.objects.filter(pk=old.pk).exists())

    def test_other_companies_with_a_filter(self):
        old = self.make(48)
        out = self.run_cleanup("--company", str(self.other_company.pk))
        self.assertIn(": 0 (", out)
        self.assertTrue(OKRChatAttachment.objects.filter(pk=old.pk).exists())


class ChatPilotGroupTests(ChatFixtures):
    def setUp(self):
        super().setUp()
        pilot = override_settings(OKR_CHAT_PILOT_EMPLOYEE_IDS=[self.alice.pk, self.bob.pk, self.boss.pk])
        pilot.enable()
        self.addCleanup(pilot.disable)

    def test_outside_the_pilot_chat_does_not_exist(self):
        room = self.open(self.alice, "team", self.team_a)["id"]
        self.api(self.bob, "get", f"rooms/{room}/messages/")
        for outsider in (self.eve, self.carol):
            with self.subTest(outsider=outsider.pk):
                response = self.api(outsider, "get", "rooms/", expect=404)
                self.assertEqual(response["code"], "chat_disabled")
                self.api(outsider, "get", "unread-count/", expect=404)

    def test_no_mention_notification_or_push_outside_the_pilot(self):
        room = self.open(self.alice, "department", self.sales)["id"]
        self.send(self.alice, room, "@Eve @Bob xem nhé", mention_ids=[self.eve.pk, self.bob.pk])
        notified = set(OKRNotification.objects.filter(notification_type="chat_mention").values_list("recipient_id", flat=True))
        self.assertEqual(notified, {self.bob.pk})
        pushed_users = {call.args[0] for call in self.push.call_args_list}
        self.assertNotIn(self.eve.employee_user_id, pushed_users)

    def test_the_member_list_and_picker_skip_people_outside_the_pilot(self):
        room = self.open(self.alice, "department", self.sales)["id"]
        listed = {person["id"] for person in self.api(self.alice, "get", f"rooms/{room}/members/")["results"]}
        self.assertIn(self.bob.pk, listed)
        self.assertNotIn(self.eve.pk, listed)
        with override_settings(OKR_CHAT_PILOT_EMPLOYEE_IDS=[]):
            everyone = {person["id"] for person in self.api(self.alice, "get", f"rooms/{room}/members/")["results"]}
        self.assertIn(self.eve.pk, everyone, "without a pilot the same room lists her")

    def test_the_web_page_and_menu_follow_the_pilot(self):
        from django.test import Client

        web = Client()
        web.force_login(self.eve.employee_user_id)
        self.assertEqual(web.get("/okr/messages").status_code, 404)
        shell = web.get("/okr/")
        self.assertEqual(shell.status_code, 200)
        self.assertNotIn("chat.js", shell.content.decode())
        web.force_login(self.alice.employee_user_id)
        self.assertIn("chat.js", web.get("/okr/").content.decode())

    def test_a_mistyped_pilot_list_fails_closed(self):
        # Badges instead of ids must not open chat to the whole company.
        with override_settings(OKR_CHAT_PILOT_EMPLOYEE_IDS=["NV006", " ", "abc"]):
            self.assertEqual(self.api(self.alice, "get", "rooms/", expect=404)["code"], "chat_disabled")
            self.api(self.carol, "get", "rooms/", expect=404)
        with override_settings(OKR_CHAT_PILOT_EMPLOYEE_IDS=f"NV006,{self.alice.pk},0,-3"):
            self.api(self.alice, "get", "rooms/")
            self.api(self.bob, "get", "rooms/", expect=404)

    def test_without_a_pilot_list_everyone_has_chat(self):
        with override_settings(OKR_CHAT_PILOT_EMPLOYEE_IDS=[]):
            self.api(self.carol, "get", "rooms/")
