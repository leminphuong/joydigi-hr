"""A Director decides the current step on its approver's behalf (2026-11-17).

Only a Director of the employee's company (or a superuser); only the
current step of a waiting leave, one step per action; a reason of at least
ten characters; the assigned approver stays on the step; the decision, who
took it and why are on the step and in the activity log. The last step
takes the days exactly once. Fake data only.
"""

from django.test import TestCase
from django.urls import reverse

from base.models import CompanyGroupAssignment
from joydigi.testkit import make_company, make_employee, make_user
from joydigi.testkit.roles import director_group
from joydigi_audit.models import UserActivityLog
from leave.models import LeaverequestComment
from leave.request_access import override_problem
from leave.tests.test_multistep_approval import MultiStepWorld

REASON = "Trưởng bộ phận đi công tác dài ngày"


class OverrideWorld(MultiStepWorld):
    def build(self):
        super().build()
        self.director_group = director_group()
        self.boss_user = make_user("ov_boss", password="x-Ov-Boss-1")
        self.boss = make_employee(company=self.company, email="boss@ov.test", user=self.boss_user)
        self.grant_director(self.boss_user, self.company)
        self.far_boss_user = make_user("ov_far_boss", password="x-Ov-Far-1")
        self.far_boss = make_employee(company=self.other_company, email="farboss@ov.test", user=self.far_boss_user)
        self.grant_director(self.far_boss_user, self.other_company)

    def grant_director(self, user, company):
        CompanyGroupAssignment.objects.get_or_create(user=user, company=company, group=self.director_group)
        CompanyGroupAssignment.sync_user_group_membership(user, self.director_group)

    def override(self, user, leave, decision="approve", reason=REASON):
        return self.web(user).post(
            reverse("approval-hub-leave-override", args=[leave.pk]), {"decision": decision, "reason": reason}
        )

    def api_override(self, user, leave, decision="approve", reason=REASON):
        return self.api(user).put(f"/api/leave/override/{leave.pk}/", {"decision": decision, "reason": reason},
                                  format="json")


class OverrideTests(OverrideWorld, TestCase):
    def setUp(self):
        self.build()
        self.leave = self.file_leave()

    def test_the_director_approves_step_one_on_behalf_of_vu(self):
        self.override(self.boss_user, self.leave)
        first, second = self.steps(self.leave)
        self.assertTrue(first.is_approved)
        self.assertTrue(first.is_override)
        self.assertEqual(first.override_reason, REASON)
        self.assertEqual(first.manager_id_id, self.vu.pk, "Vũ stays the assigned approver")
        self.assertEqual(first.acted_by_id, self.boss.pk)
        self.assertFalse(second.is_approved, "one step only")
        self.assertEqual(self.fresh(self.leave).status, "requested")
        self.assertEqual(self.fund(), 30)
        log = UserActivityLog.objects.get(details__leave_id=self.leave.pk, details__override=True,
                                          details__result="ok")
        self.assertEqual(log.details["step"], 1)
        self.assertEqual(log.details["reason"], REASON)
        self.assertEqual(log.role, "director")

    def test_the_last_step_on_behalf_approves_and_takes_the_days_once(self):
        self.web_approve(self.vu_user, self.leave)
        self.override(self.boss_user, self.leave)
        self.assertEqual(self.fresh(self.leave).status, "approved")
        self.assertEqual(self.fund(), 25)
        self.override(self.boss_user, self.leave)
        self.assertEqual(self.fund(), 25, "nothing waits any more")

    def test_reject_on_behalf_rejects_the_leave_with_the_reason(self):
        self.override(self.boss_user, self.leave, decision="reject")
        leave = self.fresh(self.leave)
        self.assertEqual(leave.status, "rejected")
        self.assertEqual(leave.reject_reason, REASON)
        first = self.steps(self.leave)[0]
        self.assertTrue(first.is_rejected and first.is_override)
        self.assertTrue(LeaverequestComment.objects.filter(request_id=self.leave, comment=REASON).exists())

    def test_a_reason_is_required(self):
        for reason in ("", "   ", "ngắn"):
            self.override(self.boss_user, self.leave, reason=reason)
        self.assertFalse(any(s.is_approved or s.is_rejected for s in self.steps(self.leave)))
        self.assertTrue(UserActivityLog.objects.filter(details__leave_id=self.leave.pk,
                                                       details__result="refused").exists())

    def test_only_a_director_of_the_employees_company(self):
        for user in (self.far_boss_user, self.admin_user, self.outsider_user):
            self.override(user, self.leave)
            self.assertIsNotNone(override_problem(user, self.leave))
        self.assertFalse(any(s.is_approved or s.is_rejected for s in self.steps(self.leave)))

    def test_never_on_ones_own_leave(self):
        own = self.file_leave(employee=self.boss)
        self.override(self.boss_user, own)
        self.assertFalse(any(s.is_approved for s in self.steps(own) if s.manager_id_id != self.boss.pk))

    def test_not_on_ones_own_step(self):
        self.web_approve(self.vu_user, self.leave)
        # make the Director the approver of step 2
        second = self.steps(self.leave)[1]
        type(second)._base_manager.filter(pk=second.pk).update(manager_id=self.boss)
        self.assertEqual(override_problem(self.boss_user, self.fresh(self.leave)),
                         "Đây là bước của bạn, hãy duyệt hoặc từ chối bình thường.")

    def test_a_superuser_overrides_with_a_reason(self):
        self.override(self.root_user, self.leave)
        first = self.steps(self.leave)[0]
        self.assertTrue(first.is_approved and first.is_override)

    def test_the_api_endpoint(self):
        self.assertEqual(self.api_override(self.boss_user, self.leave, reason="").status_code, 400)
        self.assertEqual(self.api_override(self.far_boss_user, self.leave).status_code, 403)
        self.assertEqual(self.api_override(self.boss_user, self.leave, decision="nope").status_code, 400)
        self.assertEqual(self.api(self.boss_user).put("/api/leave/override/999999/",
                                                      {"decision": "approve", "reason": REASON},
                                                      format="json").status_code, 404)
        self.assertEqual(self.api_override(self.boss_user, self.leave).status_code, 200)
        self.assertTrue(self.steps(self.leave)[0].is_override)

    def test_the_hub_offers_it_to_the_director_only(self):
        response = self.web(self.boss_user).get(reverse("approval-hub"))
        rows = {item.pk: item for item in response.context["leave_requests"]}
        self.assertTrue(rows[self.leave.pk].can_override)
        self.assertFalse(rows[self.leave.pk].can_act, "not the approver of step 1")
        response = self.web(self.admin_user).get(reverse("approval-hub"))
        rows = {item.pk: item for item in response.context["leave_requests"]}
        self.assertFalse(rows[self.leave.pk].can_override)

    def test_the_director_approves_a_one_step_leave_of_the_company_as_an_admin(self):
        single = self.file_leave(days=1)
        self.assertEqual(self.steps(single), [])
        self.web_approve(self.boss_user, single)
        self.assertEqual(self.fresh(single).status, "approved")
        far_single = self.file_leave(days=1)
        self.web_approve(self.far_boss_user, far_single)
        self.assertEqual(self.fresh(far_single).status, "requested")
