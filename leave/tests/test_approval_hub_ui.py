"""The approval hub's leave table (2026-11-17, after the Director deploy).

Reported: the Director Đàm Văn Vũ, approver of step 1, saw only "Từ chối".
The "Duyệt" button was there (``can_act`` was true) but invisible: it used
``bg-green-600`` with white text, and the theme's Tailwind config replaces
the colour palette, so that class does not exist — white text on no
background. Every row also had a permanent "Lý do từ chối" box.

Now: one compact table shared by every role (the layout before the Director
upgrade), the leaves waiting for the viewer first; "Duyệt bước N" / "Duyệt"
and "Từ chối" only when the backend allows it; the reason is asked in a
dialog; the buttons have their own colours. Fake data only.
"""

import re

from django.test import TestCase
from django.urls import reverse

from base.checkin_portal import _HUB_RECENT_LIMIT
from base.models import CompanyGroupAssignment
from joydigi.testkit.roles import director_group
from leave.tests.test_multistep_approval import MultiStepWorld


class HubUiWorld(MultiStepWorld):
    def build(self):
        super().build()
        # as in production: Vũ holds the Director role in the company and is
        # Nam's reporting manager (so the approver of step 1)
        group = director_group()
        CompanyGroupAssignment.objects.get_or_create(user=self.vu_user, company=self.company, group=group)
        CompanyGroupAssignment.sync_user_group_membership(self.vu_user, group)

    def hub(self, user):
        response = self.web(user).get(reverse("approval-hub"))
        self.assertEqual(response.status_code, 200)
        return response

    def row_html(self, response, leave):
        """The desktop table row of ``leave``."""
        html = response.content.decode()
        match = re.search(r'<tr class="[^"]*" data-leave-id="%d">(.*?)</tr>' % leave.pk, html, re.S)
        self.assertIsNotNone(match, f"leave {leave.pk} is not in the table")
        return match.group(1)


class DirectorAtStepOneTests(HubUiWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.file_leave()

    def test_the_director_sees_approve_step_one_and_reject(self):
        response = self.hub(self.vu_user)
        row = self.row_html(response, self.leave)
        self.assertIn("Duyệt bước 1", row)
        self.assertIn('class="ahl-btn ahl-btn-approve"', row)
        self.assertIn(reverse("request-approve", args=[self.leave.pk]), row)
        self.assertIn(reverse("approval-hub-leave-reject", args=[self.leave.pk]), row)
        item = next(i for i in response.context["leave_table"] if i.pk == self.leave.pk)
        self.assertTrue(item.can_act and item.can_reject and item.is_mine)
        self.assertFalse(item.can_override, "his own step: an ordinary decision, not an override")

    def test_no_reason_box_in_the_rows_and_one_dialog(self):
        html = self.hub(self.vu_user).content.decode()
        self.assertNotIn("Lý do từ chối", self.row_html(self.hub(self.vu_user), self.leave))
        self.assertEqual(html.count('name="reason"'), 1, "only the dialog asks for a reason")
        self.assertIn('id="leaveDecisionDialog"', html)
        self.assertIn('data-ahl-open="reject"', html)

    def test_no_button_uses_a_colour_the_theme_does_not_have(self):
        html = self.hub(self.vu_user).content.decode()
        section = html[html.index('id="leaveApprovalSection"'):html.index('id="leaveDecisionDialog"')]
        self.assertNotIn("bg-green-600", section)
        self.assertIn(".ahl-btn-approve { background: #16a34a; color: #fff; }", html)

    def test_approving_step_one_hands_the_leave_to_the_admin(self):
        self.assertNotIn("Duyệt bước 2", self.row_html(self.hub(self.admin_user), self.leave))
        self.web_approve(self.vu_user, self.leave)
        first, second = self.steps(self.leave)
        self.assertTrue(first.is_approved and not second.is_approved)
        self.assertEqual(self.fresh(self.leave).status, "requested")
        row = self.row_html(self.hub(self.admin_user), self.leave)
        self.assertIn("Duyệt bước 2", row)
        self.web_approve(self.admin_user, self.leave)
        self.assertEqual(self.fresh(self.leave).status, "approved")


class AdminWaitingForStepOneTests(HubUiWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.file_leave()

    def test_the_admin_sees_who_it_waits_for_and_no_approve_button(self):
        response = self.hub(self.admin_user)
        row = self.row_html(response, self.leave)
        self.assertIn("Đang chờ", row)
        self.assertIn(self.vu.get_full_name(), row)
        self.assertNotIn(reverse("request-approve", args=[self.leave.pk]), row)
        self.assertNotIn('data-ahl-open="reject"', row)
        self.assertNotIn('data-ahl-open="override"', row, "the admin is not a Director")

    def test_the_admin_cannot_skip_step_one_by_posting_either(self):
        self.web_approve(self.admin_user, self.leave)
        self.hub_reject(self.admin_user, self.leave)
        self.web(self.admin_user).post(reverse("approval-hub-leave-override", args=[self.leave.pk]),
                                       {"decision": "approve", "reason": "Giám đốc vắng mặt cả tuần"})
        self.assertFalse(any(s.is_approved or s.is_rejected for s in self.steps(self.leave)))


class DirectorOverrideButtonTests(HubUiWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.file_leave()
        self.web_approve(self.vu_user, self.leave)  # now waiting for the admin

    def test_the_director_may_decide_the_admins_step_with_a_reason_only(self):
        row = self.row_html(self.hub(self.vu_user), self.leave)
        self.assertIn('data-ahl-open="override"', row)
        self.assertIn('data-step="2"', row)
        self.assertNotIn(reverse("request-approve", args=[self.leave.pk]), row)
        url = reverse("approval-hub-leave-override", args=[self.leave.pk])
        self.web(self.vu_user).post(url, {"decision": "approve", "reason": "ngắn"})
        self.assertFalse(self.steps(self.leave)[1].is_approved, "a reason of 10+ characters is required")
        self.web(self.vu_user).post(url, {"decision": "approve", "reason": "Quản trị viên nghỉ phép dài ngày"})
        second = self.steps(self.leave)[1]
        self.assertTrue(second.is_approved and second.is_override)
        self.assertEqual(second.manager_id_id, self.admin.pk)


class TableShapeTests(HubUiWorld, TestCase):
    def setUp(self):
        self.build()

    def test_mine_first_then_a_capped_recent_list(self):
        mine = [self.file_leave() for _ in range(3)]
        for leave in mine[:2]:
            self.web_approve(self.vu_user, leave)  # no longer Vũ's to decide
        # more recent leaves than the cap, none of them Vũ's to decide
        for _ in range(_HUB_RECENT_LIMIT + 2):
            self.file_leave(days=2, employee=self.outsider)  # step 1: the admin
        table = self.hub(self.vu_user).context["leave_table"]
        flags = [item.is_mine for item in table]
        self.assertEqual(flags, sorted(flags, reverse=True), "waiting-for-me rows come first")
        self.assertIn(mine[2].pk, [item.pk for item in table if item.is_mine])
        self.assertLessEqual(len([i for i in table if not i.is_mine]), _HUB_RECENT_LIMIT)

    def test_mobile_cards_carry_the_same_buttons(self):
        leave = self.file_leave()
        html = self.hub(self.vu_user).content.decode()
        cards = html[html.index('class="ahl-cards"'):html.index('id="leaveDecisionDialog"')]
        self.assertIn(f'data-leave-id="{leave.pk}"', cards)
        self.assertIn("Duyệt bước 1", cards)

    def test_a_plain_step_approver_without_any_role_gets_the_same_table(self):
        CompanyGroupAssignment.objects.filter(user=self.vu_user).delete()
        self.vu_user.groups.clear()
        leave = self.file_leave()
        response = self.hub(self.vu_user)
        self.assertFalse(response.context["show_other_sections"])
        self.assertIn("Duyệt bước 1", self.row_html(response, leave))
