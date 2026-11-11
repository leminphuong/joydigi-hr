"""OKR chat (2026-11-09): access rules, privacy and behaviour through the real
``/api/okr/chat/`` URLs with JWT. Fake data only.

Rules under test (approved 2026-11-09): same Team / department may chat;
other teams, other companies, private OKRs and third parties may not;
company OKRs: owner + OKR admins + OKR managers; personal OKRs: owner's
department + managers; admins do not read team / department / 1–1 rooms.
"""

import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from okr.models import (
    OKRChatAttachment,
    OKRChatMember,
    OKRChatMention,
    OKRChatMessage,
    OKRChatRoom,
    OKRDepartmentRole,
    OKRNotification,
    OKRTeam,
    OKRTeamMember,
)
from okr.models.choices import ApprovalStatus, Scope
from okr.services import chat
from okr.tests.factories import (
    okr_company,
    okr_cycle,
    okr_department,
    okr_employee,
    okr_key_result,
    okr_objective,
    okr_role,
    set_reporting_manager,
)

def _real_png() -> bytes:
    # A real, decodable image: chat uploads are opened with Pillow since
    # 2026-11-11, so a PNG signature followed by zeros no longer passes.
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (22, 104, 242)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _real_png()


def png(name="anh.png"):
    return SimpleUploadedFile(name, PNG, content_type="image/png")


class ChatFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = okr_company("Chat Co A")
        cls.other_company = okr_company("Chat Co B")
        cls.sales = okr_department(cls.company, "Phong Kinh Doanh")
        cls.tech = okr_department(cls.company, "Phong Ky Thuat")
        cls.alice = okr_employee(cls.company, "chat_alice", cls.sales)
        cls.bob = okr_employee(cls.company, "chat_bob", cls.sales)
        cls.eve = okr_employee(cls.company, "chat_eve", cls.sales)
        cls.carol = okr_employee(cls.company, "chat_carol", cls.tech)
        cls.boss = okr_employee(cls.company, "chat_boss", cls.tech)
        cls.admin = okr_employee(cls.company, "chat_admin", cls.tech)
        cls.dave = okr_employee(cls.other_company, "chat_dave")
        okr_role(cls.boss, "manager")
        okr_role(cls.admin, "admin")
        okr_role(cls.dave, "admin")
        set_reporting_manager(cls.bob, cls.boss)
        cls.team_a = OKRTeam.objects.create(company=cls.company, name="Team A", manager=cls.boss)
        cls.team_b = OKRTeam.objects.create(company=cls.company, name="Team B")
        for person in (cls.alice, cls.bob):
            OKRTeamMember.objects.create(company=cls.company, team=cls.team_a, employee=person)
        OKRTeamMember.objects.create(company=cls.company, team=cls.team_b, employee=cls.carol)
        OKRDepartmentRole.objects.create(company=cls.company, department=cls.sales, manager=cls.boss)
        cycle = okr_cycle(cls.company)
        approved = {"approval_status": ApprovalStatus.APPROVED}
        cls.team_okr = okr_objective(cycle, cls.alice, scope=Scope.TEAM, team=cls.team_a, **approved)
        cls.team_draft = okr_objective(cycle, cls.alice, scope=Scope.TEAM, team=cls.team_a, title="Ban nhap Team A")
        cls.bob_okr = okr_objective(cycle, cls.bob, title="OKR ca nhan Bob", **approved)
        cls.bob_draft = okr_objective(cycle, cls.bob, title="Nhap ca nhan Bob")
        cls.company_okr = okr_objective(cycle, cls.admin, scope=Scope.COMPANY, title="OKR cong ty", **approved)
        cls.sales_okr = okr_objective(cycle, cls.eve, scope=Scope.DEPARTMENT, department=cls.sales, **approved)
        cls.team_kr = okr_key_result(cls.team_okr, cls.bob, approval_status=ApprovalStatus.APPROVED)

    def setUp(self):
        self.private_root = tempfile.mkdtemp(prefix="okr_chat_private_")
        self.addCleanup(shutil.rmtree, self.private_root, True)
        settings_patch = override_settings(OKR_CHAT_ENABLED=True, OKR_PRIVATE_MEDIA_ROOT=self.private_root)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        runner = patch("okr.services.chat.runner", side_effect=lambda function, *args: function(*args))
        runner.start()
        self.addCleanup(runner.stop)
        push = patch("joydigi_api.push.send_to_user", return_value={"sent": 1})
        self.push = push.start()
        self.addCleanup(push.stop)
        self._ids = 0

    # -- helpers ----------------------------------------------------------------

    def jwt_client(self, employee):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(employee.employee_user_id)}")
        return client

    def api(self, employee, method, path, data=None, expect=200, fmt="json"):
        with self.captureOnCommitCallbacks(execute=True):
            response = getattr(self.jwt_client(employee), method)(f"/api/okr/chat/{path}", data, format=fmt)
        if response.status_code != expect:
            self.fail(f"{method.upper()} {path}: {response.status_code} != {expect}: {getattr(response, 'data', '')}")
        return response.json() if response.get("Content-Type", "").startswith("application/json") else response

    def open(self, employee, kind, target, expect=200):
        field = {
            "objective": "objective_id", "key_result": "key_result_id", "team": "team_id",
            "department": "department_id", "direct": "employee_id",
        }[kind]
        return self.api(employee, "post", "rooms/", {"kind": kind, field: target.pk}, expect=expect)

    def send(self, employee, room_id, content="Xin chào cả nhóm 👋", expect=201, **extra):
        self._ids += 1
        body = {"content": content, "client_message_id": extra.pop("client_message_id", f"msg-{self._ids:08d}")}
        body.update(extra)
        return self.api(employee, "post", f"rooms/{room_id}/messages/", body, expect=expect)

    def messages(self, employee, room_id, expect=200, query=""):
        return self.api(employee, "get", f"rooms/{room_id}/messages/{query}", expect=expect)

    def room_ids(self, employee):
        return {row["id"] for row in self.api(employee, "get", "rooms/")["results"]}


class TeamAndDepartmentRoomTests(ChatFixtures):
    def test_team_member_reads_and_writes_the_team_room(self):
        room = self.open(self.alice, "team", self.team_a)
        sent = self.send(self.alice, room["id"], "Báo cáo tiến độ tuần — đã xong 80%")
        listed = self.messages(self.bob, room["id"])["results"]
        self.assertEqual([m["id"] for m in listed], [sent["id"]])
        self.assertEqual(listed[0]["content"], "Báo cáo tiến độ tuần — đã xong 80%")
        self.assertEqual(listed[0]["sender"]["id"], self.alice.pk)

    def test_other_team_cannot_read_write_or_list_the_room(self):
        room = self.open(self.alice, "team", self.team_a)
        self.send(self.alice, room["id"])
        self.messages(self.carol, room["id"], expect=404)
        self.api(self.carol, "get", f"rooms/{room['id']}/", expect=404)
        self.send(self.carol, room["id"], expect=404)
        self.open(self.carol, "team", self.team_a, expect=404)
        self.assertNotIn(room["id"], self.room_ids(self.carol))

    def test_other_company_is_blocked_everywhere(self):
        room = self.open(self.alice, "team", self.team_a)
        message = self.send(self.alice, room["id"])
        self.messages(self.dave, room["id"], expect=404)
        self.send(self.dave, room["id"], expect=404)
        self.open(self.dave, "team", self.team_a, expect=404)
        self.api(self.dave, "patch", f"messages/{message['id']}/", {"content": "x"}, expect=404)
        self.api(self.dave, "delete", f"messages/{message['id']}/", expect=404)
        self.assertEqual(self.room_ids(self.dave), set())

    def test_team_manager_joins_the_team_room(self):
        room = self.open(self.boss, "team", self.team_a)
        self.send(self.boss, room["id"], "Thông báo: họp lúc 9h")
        self.assertIn(room["id"], self.room_ids(self.alice))

    def test_department_room_is_for_that_department_of_that_company(self):
        room = self.open(self.eve, "department", self.sales)
        self.send(self.eve, room["id"])
        self.messages(self.alice, room["id"])
        self.messages(self.boss, room["id"])  # OKR manager of the department
        self.messages(self.carol, room["id"], expect=404)
        self.open(self.carol, "department", self.sales, expect=404)
        self.open(self.dave, "department", self.sales, expect=404)

    def test_admin_does_not_read_team_or_department_rooms(self):
        team_room = self.open(self.alice, "team", self.team_a)
        dept_room = self.open(self.alice, "department", self.sales)
        self.messages(self.admin, team_room["id"], expect=404)
        self.messages(self.admin, dept_room["id"], expect=404)

    def test_team_and_department_rooms_are_listed_without_opening_them(self):
        ids = self.room_ids(self.bob)
        kinds = {r.kind for r in OKRChatRoom.objects.filter(pk__in=ids)}
        self.assertEqual(kinds, {"team", "department"})

    def test_opening_the_same_room_twice_or_by_two_people_never_duplicates(self):
        first = self.open(self.alice, "team", self.team_a)["id"]
        second = self.open(self.bob, "team", self.team_a)["id"]
        third = self.open(self.alice, "team", self.team_a)["id"]
        self.assertEqual({first, second, third}, {first})
        self.assertEqual(OKRChatRoom.objects.filter(team=self.team_a).count(), 1)


class OKRRoomTests(ChatFixtures):
    def test_team_okr_room_for_team_members_only(self):
        room = self.open(self.bob, "objective", self.team_okr)
        self.send(self.bob, room["id"])
        self.messages(self.alice, room["id"])
        self.open(self.carol, "objective", self.team_okr, expect=403)  # sees the approved OKR, not in the team
        self.messages(self.carol, room["id"], expect=404)

    def test_private_or_draft_okr_never_leaks_through_chat(self):
        # Bob is in Team A but cannot see its draft objective, so no chat either.
        self.open(self.bob, "objective", self.team_draft, expect=404)
        self.open(self.eve, "objective", self.bob_draft, expect=404)
        room = self.open(self.alice, "objective", self.team_draft)
        self.messages(self.bob, room["id"], expect=404)

    def test_personal_okr_room_is_for_the_owners_department_and_managers(self):
        room = self.open(self.bob, "objective", self.bob_okr)
        self.messages(self.eve, room["id"])  # same department
        self.messages(self.boss, room["id"])  # direct manager
        self.open(self.carol, "objective", self.bob_okr, expect=403)  # other department

    def test_company_okr_room_is_for_owner_admins_and_managers(self):
        room = self.open(self.admin, "objective", self.company_okr)
        self.messages(self.boss, room["id"])
        self.open(self.alice, "objective", self.company_okr, expect=403)
        self.messages(self.alice, room["id"], expect=404)

    def test_department_okr_room(self):
        room = self.open(self.eve, "objective", self.sales_okr)
        self.messages(self.alice, room["id"])
        self.open(self.carol, "objective", self.sales_okr, expect=403)

    def test_key_result_room_follows_its_objective_and_links_back(self):
        room = self.open(self.alice, "key_result", self.team_kr)
        self.assertEqual(room["target"]["key_result_id"], self.team_kr.pk)
        self.assertEqual(room["target"]["objective_id"], self.team_okr.pk)
        self.open(self.carol, "key_result", self.team_kr, expect=403)

    def test_admin_reads_okr_rooms(self):
        room = self.open(self.alice, "objective", self.team_okr)
        self.messages(self.admin, room["id"])

    def test_deleted_objective_closes_its_room(self):
        room = self.open(self.alice, "objective", self.team_okr)
        self.team_okr.soft_delete()
        self.messages(self.alice, room["id"], expect=404)


class DirectRoomTests(ChatFixtures):
    def test_only_the_two_people_read_a_direct_room_and_it_is_never_duplicated(self):
        room = self.open(self.alice, "direct", self.bob)
        self.assertEqual(self.open(self.bob, "direct", self.alice)["id"], room["id"])
        self.send(self.alice, room["id"], "Chiều nay mình trao đổi riêng nhé")
        self.messages(self.bob, room["id"])
        for third in (self.eve, self.admin, self.boss, self.dave):
            self.messages(third, room["id"], expect=404)
        self.assertEqual(OKRChatRoom.objects.filter(kind="direct").count(), 1)

    def test_no_direct_room_with_someone_from_another_company_or_oneself(self):
        self.open(self.alice, "direct", self.dave, expect=404)
        self.open(self.alice, "direct", self.alice, expect=400)

    def test_partner_who_left_makes_the_room_read_only(self):
        room = self.open(self.alice, "direct", self.bob)
        self.send(self.alice, room["id"])
        self.bob.is_active = False
        self.bob.save()
        self.messages(self.alice, room["id"])
        body = self.send(self.alice, room["id"], expect=403)
        self.assertEqual(body["code"], "chat_read_only")


class MessageRuleTests(ChatFixtures):
    def setUp(self):
        super().setUp()
        self.room = self.open(self.alice, "team", self.team_a)["id"]

    def test_sender_cannot_be_spoofed(self):
        sent = self.send(self.alice, self.room, sender_id=self.bob.pk, sender=self.bob.pk)
        self.assertEqual(OKRChatMessage.objects.get(pk=sent["id"]).sender_id, self.alice.pk)

    def test_nobody_edits_someone_elses_message(self):
        sent = self.send(self.alice, self.room)
        self.api(self.bob, "patch", f"messages/{sent['id']}/", {"content": "sửa lén"}, expect=403)
        self.api(self.boss, "patch", f"messages/{sent['id']}/", {"content": "sửa lén"}, expect=403)
        edited = self.api(self.alice, "patch", f"messages/{sent['id']}/", {"content": "Đã cập nhật"})
        self.assertEqual(edited["content"], "Đã cập nhật")
        self.assertIsNotNone(edited["edited_at"])

    def test_edit_window_is_24_hours(self):
        sent = self.send(self.alice, self.room)
        OKRChatMessage.objects.filter(pk=sent["id"]).update(created_at=timezone.now() - timedelta(hours=25))
        self.api(self.alice, "patch", f"messages/{sent['id']}/", {"content": "muộn"}, expect=400)

    def test_recall_by_sender_or_room_manager_only(self):
        mine = self.send(self.alice, self.room, "Tin cần thu hồi")
        self.api(self.bob, "delete", f"messages/{mine['id']}/", expect=403)
        recalled = self.api(self.alice, "delete", f"messages/{mine['id']}/")
        self.assertTrue(recalled["recalled"])
        self.assertEqual(recalled["content"], "")
        bobs = self.send(self.bob, self.room, "Nội dung vi phạm")
        hidden = self.api(self.boss, "delete", f"messages/{bobs['id']}/")  # Team A manager
        self.assertTrue(hidden["recalled_by_moderator"])
        listed = {m["id"]: m for m in self.messages(self.alice, self.room)["results"]}
        self.assertEqual(listed[mine["id"]]["content"], "")
        self.assertEqual(listed[bobs["id"]]["content"], "")

    def test_same_client_message_id_posts_once(self):
        first = self.send(self.alice, self.room, client_message_id="retry-0001")
        again = self.send(self.alice, self.room, client_message_id="retry-0001", expect=200)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(OKRChatMessage.objects.filter(room_id=self.room).count(), 1)

    def test_rate_limit_answers_429(self):
        with patch.object(chat, "RATE_MESSAGES", 3):
            for _ in range(3):
                self.send(self.alice, self.room)
            body = self.send(self.alice, self.room, expect=429)
        self.assertEqual(body["code"], "throttled")

    def test_unicode_html_and_length(self):
        text = "<script>alert(1)</script> Tiếng Việt có dấu ✅ 🎯"
        sent = self.send(self.alice, self.room, text)
        self.assertEqual(sent["content"], text)  # stored as text; clients escape it
        self.send(self.alice, self.room, "x" * (chat.MAX_CONTENT + 1), expect=400)
        self.send(self.alice, self.room, "   ", expect=400)

    def test_reply_and_ordering_and_polling_cursor(self):
        first = self.send(self.alice, self.room, "Một")
        second = self.send(self.bob, self.room, "Hai", reply_to=first["id"])
        self.assertEqual(second["reply_to"]["id"], first["id"])
        page = self.messages(self.alice, self.room)
        self.assertEqual([m["id"] for m in page["results"]], [first["id"], second["id"]])
        newer = self.messages(self.alice, self.room, query=f"?after={first['id']}")
        self.assertEqual([m["id"] for m in newer["results"]], [second["id"]])
        since = page["server_time"]
        self.api(self.alice, "patch", f"messages/{first['id']}/", {"content": "Một (sửa)"})
        changed = self.messages(self.bob, self.room, query=f"?after={second['id']}&changed_since={since}")
        self.assertEqual([m["id"] for m in changed["changed"]], [first["id"]])

    def test_reply_to_a_message_of_another_room_is_refused(self):
        other = self.open(self.alice, "direct", self.bob)["id"]
        elsewhere = self.send(self.alice, other)
        self.send(self.alice, self.room, reply_to=elsewhere["id"], expect=400)

    def test_mentions_only_people_of_the_room(self):
        sent = self.send(self.alice, self.room, "@Bob @Carol xem giúp", mention_ids=[self.bob.pk, self.carol.pk])
        self.assertEqual({m["id"] for m in sent["mentions"]}, {self.bob.pk})
        self.assertFalse(OKRChatMention.objects.filter(employee=self.carol).exists())

    def test_unread_count_and_mark_read(self):
        self.room_ids(self.bob)  # Bob's membership starts "read up to now"
        sent = self.send(self.alice, self.room)
        self.assertEqual(self.api(self.bob, "get", "unread-count/")["unread"], 1)
        self.api(self.bob, "post", f"rooms/{self.room}/read/", {"message_id": sent["id"]})
        self.assertEqual(self.api(self.bob, "get", "unread-count/")["unread"], 0)
        self.assertEqual(self.api(self.alice, "get", "unread-count/")["unread"], 0, "own messages are read")

    def test_search_only_in_rooms_the_viewer_may_use(self):
        self.send(self.alice, self.room, "Kế hoạch khuyến mãi tháng 12")
        found = self.api(self.bob, "get", "search/?q=khuyen mai")["results"]  # accent-insensitive
        self.assertEqual(len(found), 1)
        self.assertEqual(self.api(self.carol, "get", "search/?q=khuyen mai")["results"], [])
        self.assertEqual(self.api(self.dave, "get", "search/?q=khuyen mai")["results"], [])


class AttachmentTests(ChatFixtures):
    def setUp(self):
        super().setUp()
        self.room = self.open(self.alice, "team", self.team_a)["id"]

    def upload(self, employee, room_id=None, expect=201, file=None):
        return self.api(employee, "post", "attachments/", {"room_id": room_id or self.room, "file": file or png()},
                        expect=expect, fmt="multipart")

    def test_file_is_downloaded_only_by_people_of_the_room(self):
        uploaded = self.upload(self.alice)
        self.assertTrue(uploaded["is_image"])
        # Not sent yet: private to the uploader.
        self.api(self.bob, "get", f"attachments/{uploaded['id']}/download/", expect=404)
        sent = self.send(self.alice, self.room, "", attachment_ids=[uploaded["id"]])
        self.assertEqual([a["id"] for a in sent["attachments"]], [uploaded["id"]])
        response = self.api(self.bob, "get", f"attachments/{uploaded['id']}/download/?inline=1")
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        for outsider in (self.carol, self.admin, self.dave):
            self.api(outsider, "get", f"attachments/{uploaded['id']}/download/", expect=404)

    def test_attachment_of_someone_else_or_another_room_cannot_be_sent(self):
        uploaded = self.upload(self.alice)
        self.send(self.bob, self.room, "lấy tệp", attachment_ids=[uploaded["id"]], expect=400)
        other = self.open(self.alice, "direct", self.bob)["id"]
        self.send(self.alice, other, "chuyển phòng", attachment_ids=[uploaded["id"]], expect=400)

    def test_outsider_cannot_upload_and_bad_files_are_refused(self):
        self.upload(self.carol, expect=404)
        fake = SimpleUploadedFile("virus.png", b"MZ\x90\x00not an image", content_type="image/png")
        self.upload(self.alice, file=fake, expect=400)
        self.upload(self.alice, file=SimpleUploadedFile("run.exe", b"MZ", content_type="application/octet-stream"),
                    expect=400)

    def test_recall_removes_the_files(self):
        uploaded = self.upload(self.alice)
        sent = self.send(self.alice, self.room, "", attachment_ids=[uploaded["id"]])
        self.api(self.alice, "delete", f"messages/{sent['id']}/")
        self.assertFalse(OKRChatAttachment.objects.filter(pk=uploaded["id"]).exists())
        self.api(self.bob, "get", f"attachments/{uploaded['id']}/download/", expect=404)


class AccessChangeTests(ChatFixtures):
    def setUp(self):
        super().setUp()
        self.room = self.open(self.alice, "team", self.team_a)["id"]
        self.room_ids(self.bob)

    def test_leaving_the_team_removes_access_at_once(self):
        OKRTeamMember.objects.filter(team=self.team_a, employee=self.bob).delete()
        self.messages(self.bob, self.room, expect=404)
        self.assertNotIn(self.room, self.room_ids(self.bob))

    def test_joining_another_team_gives_its_room(self):
        OKRTeamMember.objects.create(company=self.company, team=self.team_b, employee=self.bob)
        room_b = self.open(self.bob, "team", self.team_b)
        self.messages(self.bob, room_b["id"])

    def test_department_transfer_moves_department_room_access(self):
        dept_room = self.open(self.alice, "department", self.sales)["id"]
        from employee.models import EmployeeWorkInformation

        EmployeeWorkInformation._base_manager.filter(employee_id=self.bob.pk).update(department_id=self.tech)
        self.messages(self.bob, dept_room, expect=404)

    def test_resigned_or_locked_account_is_blocked(self):
        self.bob.is_active = False
        self.bob.save()
        self.messages(self.bob, self.room, expect=403)
        user = self.eve.employee_user_id
        user.is_active = False
        user.save()
        response = self.jwt_client(self.eve).get(f"/api/okr/chat/rooms/{self.room}/messages/")
        self.assertIn(response.status_code, (401, 403))

    def test_push_never_reaches_someone_who_lost_access_and_carries_no_content(self):
        self.send(self.alice, self.room, "Bí mật kinh doanh quý 4")
        bodies = [call.args[2] for call in self.push.call_args_list]
        self.assertTrue(bodies, "Bob and the manager are pushed")
        self.assertTrue(all("Bí mật" not in body for body in bodies))
        self.push.reset_mock()
        OKRTeamMember.objects.filter(team=self.team_a, employee=self.bob).delete()
        OKRChatMember.objects.filter(room_id=self.room).update(last_push_at=None)
        self.send(self.alice, self.room, "Tin thứ hai")
        pushed_users = {call.args[0].pk for call in self.push.call_args_list}
        self.assertNotIn(self.bob.employee_user_id.pk, pushed_users)

    def test_plain_messages_are_pushed_at_most_once_per_gap(self):
        self.send(self.alice, self.room, "Một")
        first = self.push.call_count
        self.send(self.alice, self.room, "Hai")
        self.assertEqual(self.push.call_count, first, "second plain message inside PUSH_GAP is not pushed again")

    def test_mention_notification_has_no_content_and_closes_with_access(self):
        self.send(self.alice, self.room, "@Bob xem số liệu mật", mention_ids=[self.bob.pk])
        note = OKRNotification.objects.get(recipient=self.bob, notification_type="chat_mention")
        self.assertNotIn("mật", note.title + note.message)
        listed = self.jwt_client(self.bob).get("/api/okr/notifications/").json()["results"]
        reference = next(n["reference"] for n in listed if n["id"] == note.pk)
        self.assertTrue(reference["available"])
        self.assertEqual(reference["room_id"], self.room)
        OKRTeamMember.objects.filter(team=self.team_a, employee=self.bob).delete()
        listed = self.jwt_client(self.bob).get("/api/okr/notifications/").json()["results"]
        reference = next(n["reference"] for n in listed if n["id"] == note.pk)
        self.assertFalse(reference["available"])
        self.assertIsNone(reference["room_id"])

    def test_reply_notifies_the_author(self):
        mine = self.send(self.bob, self.room, "Câu hỏi của Bob")
        self.send(self.alice, self.room, "Trả lời", reply_to=mine["id"])
        self.assertTrue(OKRNotification.objects.filter(recipient=self.bob, notification_type="chat_reply").exists())


class FeatureFlagTests(ChatFixtures):
    def test_everything_is_404_while_the_flag_is_off(self):
        room = self.open(self.alice, "team", self.team_a)
        with override_settings(OKR_CHAT_ENABLED=False):
            for path in ("rooms/", f"rooms/{room['id']}/messages/", "unread-count/"):
                response = self.jwt_client(self.alice).get(f"/api/okr/chat/{path}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["code"], "chat_disabled")
            self.assertIsNone(chat.notification_target(chat.OKRViewer.for_user(self.alice.employee_user_id), 1))

    def test_anonymous_is_401(self):
        self.assertEqual(APIClient().get("/api/okr/chat/rooms/").status_code, 401)


class WebShellTests(ChatFixtures):
    def test_menu_script_and_page_only_while_enabled(self):
        self.client.force_login(self.alice.employee_user_id)
        page = self.client.get("/okr/messages")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn('data-route="messages"', html)
        self.assertIn("okr_web/js/chat.js", html)
        self.assertIn('"chatEnabled": true', html)
        self.assertEqual(self.client.get("/okr/messages/5").status_code, 200)
        with override_settings(OKR_CHAT_ENABLED=False):
            self.assertEqual(self.client.get("/okr/messages").status_code, 404)
            html = self.client.get("/okr/dashboard").content.decode()
            self.assertNotIn('data-route="messages"', html)
            self.assertIn('"chatEnabled": false', html)

    def test_web_session_needs_csrf_to_post(self):
        from django.test import Client

        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.alice.employee_user_id)
        response = browser.post("/api/okr/chat/rooms/", {"kind": "team", "team_id": self.team_a.pk},
                                content_type="application/json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "csrf_failed")


class ChatContractTests(ChatFixtures):
    """The bodies the app parses. With ``OKR_CONTRACT_FIXTURES=<dir>`` they are
    also written there for the app's two-sided contract test
    (appcheckin ``test/features/okr/contract/okr_chat_contract_test.dart``)."""

    MESSAGE_KEYS = {
        "id", "room_id", "client_message_id", "sender", "is_mine", "content", "created_at", "edited_at",
        "updated_at", "recalled", "recalled_by_moderator", "reply_to", "attachments", "mentions", "permissions",
    }
    ROOM_KEYS = {
        "id", "kind", "title", "target", "partner", "unread", "muted", "last_read_id", "last_message_at",
        "last_message", "permissions",
    }

    def record(self, name, body):
        import json
        import os
        from pathlib import Path

        target = os.environ.get("OKR_CONTRACT_FIXTURES")
        if target:
            path = Path(target) / f"{name}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        return body

    def test_bodies_keep_the_contract(self):
        room = self.record("chat_room", self.open(self.alice, "team", self.team_a))
        self.assertEqual(set(room), self.ROOM_KEYS)
        self.room_ids(self.bob)
        uploaded = self.api(self.alice, "post", "attachments/", {"room_id": room["id"], "file": png()},
                            expect=201, fmt="multipart")
        sent = self.record("chat_message", self.send(
            self.alice, room["id"], "Xin chào @Bob 👋", mention_ids=[self.bob.pk], attachment_ids=[uploaded["id"]]))
        self.assertEqual(set(sent), self.MESSAGE_KEYS)
        self.send(self.bob, room["id"], "Đã nhận", reply_to=sent["id"])
        history = self.record("chat_messages", self.messages(self.bob, room["id"]))
        self.assertEqual(set(history), {"room_id", "results", "changed", "has_more_before", "has_more_after",
                                        "server_time"})
        self.assertEqual(len(history["results"]), 2)
        rooms = self.record("chat_rooms", self.api(self.bob, "get", "rooms/"))
        self.assertTrue(all(set(r) == self.ROOM_KEYS for r in rooms["results"]))
        self.record("chat_members", self.api(self.bob, "get", f"rooms/{room['id']}/members/"))
        self.record("chat_unread", self.api(self.alice, "get", "unread-count/"))
        direct = self.record("chat_room_direct", self.open(self.alice, "direct", self.bob))
        self.assertEqual(direct["partner"]["id"], self.bob.pk)
        notifications = self.record(
            "chat_notifications", self.jwt_client(self.bob).get("/api/okr/notifications/").json())
        chat_note = next(n for n in notifications["results"] if n["type"] == "chat_mention")
        self.assertEqual(chat_note["reference"]["type"], "chat_message")
        self.assertEqual(chat_note["reference"]["room_id"], room["id"])
