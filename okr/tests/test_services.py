"""Gate 2: OKR business rules ported from JOYOKR v1.6.45 (services layer).

Fake companies and people only. Each test names the JOYOKR rule it pins.
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from okr.models import (
    OKRActivity,
    OKRCheckin,
    OKRCycle,
    OKRDepartmentRole,
    OKRKeyResult,
    OKRNotification,
    OKRObjective,
)
from okr.models.choices import ActivityAction, ApprovalStatus, CycleStatus, Scope
from okr.services import calculator, cycles, notifications, okrs, reports
from okr.services.access import OKRViewer, scope_of
from okr.services.errors import OKRError
from okr.tests.factories import (
    okr_company,
    okr_cycle,
    okr_department,
    okr_employee,
    okr_objective,
    okr_role,
    set_reporting_manager,
)


def as_viewer(employee):
    return OKRViewer.for_user(employee.employee_user_id)


class ServiceTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = okr_company("OKR Svc A")
        cls.other_company = okr_company("OKR Svc B")
        cls.department = okr_department(cls.company)
        cls.owner = okr_employee(cls.company, "svc_owner", cls.department)
        cls.manager = okr_employee(cls.company, "svc_manager", cls.department)
        cls.admin = okr_employee(cls.company, "svc_admin")
        cls.colleague = okr_employee(cls.company, "svc_colleague", cls.department)
        cls.outsider = okr_employee(cls.other_company, "svc_outsider")
        okr_role(cls.manager, "manager")
        okr_role(cls.admin, "admin")
        okr_role(cls.outsider, "admin")
        set_reporting_manager(cls.owner, cls.manager)
        cls.cycle = okr_cycle(cls.company)
        cls.company_objective = okr_objective(
            cls.cycle,
            cls.admin,
            scope=Scope.COMPANY,
            approval_status=ApprovalStatus.APPROVED,
            title="Tang truong doanh thu cong ty",
        )

    def draft(self, owner=None, weights=("50", "50"), **data):
        viewer = as_viewer(owner or self.owner)
        values = {
            "title": "Nang chat luong cham soc khach hang",
            "cycle_id": self.cycle.pk,
            "scope": Scope.PERSONAL,
            "parent_id": self.company_objective.pk,
        }
        values.update(data)
        objective = okrs.create_objective(viewer, values)
        for index, weight in enumerate(weights):
            okrs.create_key_result(
                viewer,
                {
                    "objective_id": objective.pk,
                    "title": f"Ket qua then chot {index + 1}",
                    "unit": "%",
                    "start_value": "0",
                    "target_value": "100",
                    "weight": weight,
                },
            )
        return objective

    def approved(self, owner=None, approver=None, **data):
        objective = self.draft(owner, **data)
        okrs.submit_objective(as_viewer(owner or self.owner), objective.pk)
        okrs.approve_objective(as_viewer(approver or self.manager), objective.pk)
        objective.refresh_from_db()
        return objective

    def assertOKRError(self, code, status, call, *args, **kwargs):
        with self.assertRaises(OKRError) as caught:
            call(*args, **kwargs)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status), caught.exception.message)
        return caught.exception


class ViewerAndPermissionTests(ServiceTestBase):
    def test_account_without_employee_has_no_okr_access(self):
        from joydigi.testkit import make_user

        user = make_user("svc_no_employee", password="okr-fake-pass-1", email="svc_no_employee@example.com")
        self.assertOKRError("no_employee", 403, OKRViewer.for_user, user)

    def test_superuser_without_okr_role_is_not_okr_admin(self):
        boss = okr_employee(self.company, "svc_superuser")
        boss.employee_user_id.is_superuser = True
        boss.employee_user_id.is_staff = True
        boss.employee_user_id.save()
        viewer = as_viewer(boss)
        self.assertFalse(viewer.is_admin)
        self.assertFalse(viewer.is_manager)

    def test_role_in_another_company_grants_nothing_here(self):
        objective = self.draft()
        outsider = as_viewer(self.outsider)
        self.assertTrue(outsider.is_admin)  # admin of company B only
        self.assertFalse(outsider.can_view_scope(scope_of(objective)))
        self.assertOKRError("not_found", 404, okrs.get_objective, outsider, objective.pk)
        visible = OKRObjective.objects.for_company(outsider.company_id).filter(outsider.objective_visibility())
        self.assertFalse(visible.filter(pk=objective.pk).exists())

    def test_visibility_follows_joyokr(self):
        draft = self.draft()

        def visible_ids(employee):
            viewer = as_viewer(employee)
            return set(
                OKRObjective.objects.for_company(self.company)
                .filter(viewer.objective_visibility())
                .values_list("pk", flat=True)
            )

        # A colleague sees company-level and approved OKRs, not someone's draft.
        self.assertEqual(visible_ids(self.colleague), {self.company_objective.pk})
        # The owner's reporting manager (with the OKR manager role) sees it.
        self.assertIn(draft.pk, visible_ids(self.manager))
        self.assertIn(draft.pk, visible_ids(self.admin))
        self.assertIn(draft.pk, visible_ids(self.owner))

    def test_reporting_manager_without_okr_role_cannot_review(self):
        set_reporting_manager(self.colleague, self.owner)
        objective = self.draft(owner=self.colleague)
        self.assertFalse(as_viewer(self.owner).can_review(scope_of(objective)))

    def test_manager_edits_managed_department_okrs_not_reports_personal_ones(self):
        personal = self.draft()
        manager = as_viewer(self.manager)
        self.assertTrue(manager.can_manage_scope(scope_of(personal)))
        self.assertFalse(manager.can_edit_okr(scope_of(personal)))
        OKRDepartmentRole.objects.create(company=self.company, department=self.department, manager=self.manager)
        department_okr = okrs.create_objective(
            as_viewer(self.manager),
            {
                "title": "Muc tieu phong ban",
                "cycle_id": self.cycle.pk,
                "scope": Scope.DEPARTMENT,
                "department_id": self.department.pk,
                "parent_id": self.company_objective.pk,
                "owner_id": self.colleague.pk,
            },
        )
        self.assertTrue(as_viewer(self.manager).can_edit_okr(scope_of(department_okr)))

    def test_employees_cannot_create_company_or_others_personal_objectives(self):
        viewer = as_viewer(self.colleague)
        self.assertOKRError(
            "forbidden", 403, okrs.create_objective, viewer,
            {"title": "X", "cycle_id": self.cycle.pk, "scope": Scope.COMPANY},
        )
        self.assertOKRError(
            "forbidden", 403, okrs.create_objective, viewer,
            {"title": "X", "cycle_id": self.cycle.pk, "scope": Scope.PERSONAL, "owner_id": self.owner.pk},
        )

    def test_parent_must_be_higher_level_in_same_cycle(self):
        personal = self.draft()
        error = self.assertOKRError(
            "validation_error", 400, okrs.create_objective, as_viewer(self.owner),
            {"title": "X", "cycle_id": self.cycle.pk, "scope": Scope.PERSONAL, "parent_id": personal.pk},
        )
        self.assertIn("parent_id", error.fields)


class ApprovalWorkflowTests(ServiceTestBase):
    def test_submit_checks_run_in_joyokr_order(self):
        owner = as_viewer(self.owner)
        one_kr = self.draft(weights=("100",))
        self.assertOKRError("invalid_kr_count", 400, okrs.submit_objective, owner, one_kr.pk)
        wrong_weights = self.draft(weights=("60", "50"))
        self.assertOKRError("invalid_weight", 400, okrs.submit_objective, owner, wrong_weights.pk)
        almost = self.draft(weights=("50", "49.99"))
        self.assertOKRError("invalid_weight", 400, okrs.submit_objective, owner, almost.pk)
        unaligned = self.draft(parent_id=None)
        self.assertOKRError("alignment_required", 400, okrs.submit_objective, owner, unaligned.pk)
        self.assertOKRError("forbidden", 403, okrs.submit_objective, as_viewer(self.colleague), one_kr.pk)

    def test_submit_notifies_approvers_but_not_the_owner(self):
        objective = self.draft()
        result = okrs.submit_objective(as_viewer(self.owner), objective.pk)
        objective.refresh_from_db()
        self.assertEqual(objective.approval_status, ApprovalStatus.SUBMITTED)
        recipients = set(
            OKRNotification.objects.filter(notification_type="approval_requested", reference_id=objective.pk)
            .values_list("recipient_id", flat=True)
        )
        self.assertEqual(recipients, {self.manager.pk, self.admin.pk})
        self.assertEqual(result["notification_count"], 2)
        self.assertTrue(OKRActivity.objects.filter(objective=objective, action=ActivityAction.SUBMITTED).exists())
        self.assertOKRError("invalid_transition", 409, okrs.submit_objective, as_viewer(self.owner), objective.pk)

    def test_approve_marks_objective_and_every_key_result_approved(self):
        objective = self.draft()
        kr = objective.key_results.first()
        OKRKeyResult.objects.filter(pk=kr.pk).update(current_value=Decimal("40"), progress=Decimal("40"))
        okrs.submit_objective(as_viewer(self.owner), objective.pk)
        self.assertOKRError("forbidden", 403, okrs.approve_objective, as_viewer(self.colleague), objective.pk)
        okrs.approve_objective(as_viewer(self.manager), objective.pk)
        objective.refresh_from_db()
        self.assertEqual(objective.approval_status, ApprovalStatus.APPROVED)
        self.assertEqual(set(objective.key_results.values_list("approval_status", flat=True)), {"approved"})
        self.assertEqual(objective.progress, Decimal("20.00"))
        self.assertEqual(OKRActivity.objects.filter(key_result__objective=objective, action="approved").count(), 2)
        self.assertTrue(
            OKRNotification.objects.filter(recipient=self.owner, notification_type="approved", reference_id=objective.pk).exists()
        )
        self.assertFalse(
            OKRNotification.objects.filter(
                notification_type="approval_requested", reference_id=objective.pk, is_read=False
            ).exists()
        )

    def test_managers_cannot_approve_their_own_okr_but_admins_can(self):
        set_reporting_manager(self.manager, self.admin)
        own = self.draft(owner=self.manager)
        okrs.submit_objective(as_viewer(self.manager), own.pk)
        self.assertOKRError("self_approval", 403, okrs.approve_objective, as_viewer(self.manager), own.pk)
        admin_own = self.draft(owner=self.admin)
        okrs.submit_objective(as_viewer(self.admin), admin_own.pk)
        okrs.approve_objective(as_viewer(self.admin), admin_own.pk)
        admin_own.refresh_from_db()
        self.assertEqual(admin_own.approval_status, ApprovalStatus.APPROVED)

    def test_closed_cycle_blocks_review(self):
        objective = self.draft()
        okrs.submit_objective(as_viewer(self.owner), objective.pk)
        OKRCycle.objects.filter(pk=self.cycle.pk).update(status=CycleStatus.CLOSED)
        self.assertOKRError("cycle_closed", 409, okrs.approve_objective, as_viewer(self.manager), objective.pk)

    def test_key_result_due_outside_cycle_blocks_approval(self):
        objective = self.draft()
        okrs.submit_objective(as_viewer(self.owner), objective.pk)
        objective.key_results.filter(pk=objective.key_results.first().pk).update(due_date=datetime.date(2027, 1, 5))
        self.assertOKRError("invalid_key_result", 400, okrs.approve_objective, as_viewer(self.manager), objective.pk)

    def test_reject_needs_reason_and_reopens_editing(self):
        objective = self.draft()
        owner, manager = as_viewer(self.owner), as_viewer(self.manager)
        okrs.submit_objective(owner, objective.pk)
        self.assertOKRError("objective_locked", 409, okrs.update_objective, owner, objective.pk, {"title": "Doi ten"})
        self.assertOKRError("invalid_rejection", 400, okrs.reject_objective, manager, objective.pk, "   ")
        okrs.reject_objective(manager, objective.pk, "Can ro chi so hon")
        objective.refresh_from_db()
        self.assertEqual(objective.approval_status, ApprovalStatus.REJECTED)
        self.assertEqual(okrs.rejection_reason(objective), "Can ro chi so hon")
        note = OKRNotification.objects.get(recipient=self.owner, notification_type="rejected")
        self.assertEqual(note.message, "Can ro chi so hon")
        okrs.update_objective(owner, objective.pk, {"title": "Ten moi", "parent_id": self.company_objective.pk})
        okrs.submit_objective(owner, objective.pk)
        objective.refresh_from_db()
        self.assertEqual(objective.approval_status, ApprovalStatus.SUBMITTED)

    def test_normalize_weights_splits_cents_like_joyokr(self):
        self.assertEqual(okrs.normalize_weights(3), [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")])
        objective = self.draft(weights=("10", "10", "10"))
        okrs.normalize_key_result_weights(as_viewer(self.owner), objective.pk)
        weights = list(objective.key_results.order_by("id").values_list("weight", flat=True))
        self.assertEqual(weights, [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")])
        self.assertEqual(sum(weights), Decimal("100.00"))
        one = self.draft(weights=("10",))
        self.assertOKRError("validation_error", 400, okrs.normalize_key_result_weights, as_viewer(self.owner), one.pk)

    def test_definition_rules_from_controller(self):
        owner = as_viewer(self.owner)
        objective = self.draft()
        error = self.assertOKRError(
            "validation_error", 400, okrs.create_key_result, owner,
            {"objective_id": objective.pk, "title": "KR", "unit": "", "start_value": "5", "target_value": "5", "weight": "0"},
        )
        self.assertEqual(set(error.fields), {"unit", "target_value", "weight"})


class CheckinTests(ServiceTestBase):
    def checkin(self, employee, kr, **overrides):
        values = {"current_value": "46", "confidence": 4, "update_text": "Da goi 12 khach hang"}
        values.update(overrides)
        return okrs.checkin(as_viewer(employee), kr.pk, **values)

    def test_checkin_updates_key_result_objective_and_history(self):
        objective = self.approved()
        kr = objective.key_results.order_by("id").first()
        OKRKeyResult.objects.filter(pk=kr.pk).update(start_value=Decimal("40"), target_value=Decimal("80"), current_value=Decimal("40"))
        result = self.checkin(self.owner, kr)
        self.assertTrue(result["created"])
        kr.refresh_from_db()
        objective.refresh_from_db()
        self.assertEqual((kr.current_value, kr.progress), (Decimal("46.0000"), Decimal("15.00")))
        self.assertEqual(objective.progress, Decimal("7.50"))
        entry = result["checkin"]
        self.assertEqual((entry.previous_value, entry.previous_progress), (Decimal("40.0000"), Decimal("0.00")))
        log = OKRActivity.objects.get(key_result=kr, action=ActivityAction.CHECKED_IN)
        self.assertEqual(log.metadata["current_progress"], "15.00")
        self.assertTrue(OKRNotification.objects.filter(recipient=self.manager, notification_type="checkin").exists())

    def test_only_the_owner_checks_in_on_approved_okrs_in_active_cycles(self):
        draft = self.draft()
        self.assertOKRError("checkin_not_approved", 409, self.checkin, self.owner, draft.key_results.first())
        objective = self.approved()
        kr = objective.key_results.first()
        self.assertOKRError("forbidden", 403, self.checkin, self.manager, kr)
        self.assertOKRError("not_found", 404, self.checkin, self.outsider, kr)
        OKRCycle.objects.filter(pk=self.cycle.pk).update(status=CycleStatus.CLOSED)
        self.assertOKRError("cycle_not_active", 409, self.checkin, self.owner, kr)

    def test_checkin_input_validation(self):
        kr = self.approved().key_results.first()
        error = self.assertOKRError(
            "validation_error", 400, self.checkin, self.owner, kr, current_value="abc", confidence=6, update_text=" "
        )
        self.assertEqual(set(error.fields), {"current_value", "confidence", "update_text"})
        error = self.assertOKRError("validation_error", 400, self.checkin, self.owner, kr, current_value="1.23456")
        self.assertIn("current_value", error.fields)

    def test_client_request_id_makes_checkin_idempotent(self):
        objective = self.approved()
        first_kr, second_kr = objective.key_results.order_by("id")
        one = self.checkin(self.owner, first_kr, client_request_id="req-1")
        again = self.checkin(self.owner, first_kr, client_request_id="req-1", current_value="70")
        self.assertFalse(again["created"])
        self.assertEqual(again["checkin"].pk, one["checkin"].pk)
        self.assertEqual(OKRCheckin.objects.filter(key_result=first_kr).count(), 1)
        self.assertOKRError("client_request_reused", 409, self.checkin, self.owner, second_kr, client_request_id="req-1")


class CycleTests(ServiceTestBase):
    def test_only_okr_admins_manage_cycles(self):
        data = {"name": "Q1 2027", "cycle_type": "quarter", "start_date": "2027-01-01", "end_date": "2027-03-31"}
        self.assertOKRError("forbidden", 403, cycles.create_cycle, as_viewer(self.manager), data)
        cycle = cycles.create_cycle(as_viewer(self.admin), data)
        self.assertEqual((cycle.status, cycle.company_id), (CycleStatus.DRAFT, self.company.pk))

    def test_same_type_cycles_cannot_overlap_within_a_company(self):
        admin = as_viewer(self.admin)
        overlap = {"name": "Trung", "cycle_type": "quarter", "start_date": "2026-12-01", "end_date": "2027-02-28"}
        error = self.assertOKRError("validation_error", 400, cycles.create_cycle, admin, overlap)
        self.assertIn("start_date", error.fields)
        cycles.create_cycle(admin, {**overlap, "cycle_type": "month"})
        okr_cycle(self.other_company, "Q1 B", start_date=datetime.date(2027, 1, 1), end_date=datetime.date(2027, 3, 31))
        cycles.create_cycle(admin, {"name": "Q1", "cycle_type": "quarter", "start_date": "2027-01-01", "end_date": "2027-03-31"})

    def test_status_transitions(self):
        admin = as_viewer(self.admin)
        draft = cycles.create_cycle(admin, {"name": "Q1", "cycle_type": "quarter", "start_date": "2027-01-01", "end_date": "2027-03-31"})
        self.assertOKRError("validation_error", 400, cycles.change_status, admin, draft.pk, CycleStatus.CLOSED)
        cycles.change_status(admin, draft.pk, CycleStatus.ACTIVE)
        cycles.change_status(admin, draft.pk, CycleStatus.CLOSED)
        cycles.change_status(admin, draft.pk, CycleStatus.ACTIVE)
        self.assertEqual(OKRActivity.objects.filter(cycle_id=draft.pk, action=ActivityAction.CYCLE_STATUS_CHANGED).count(), 3)

    def test_clone_copies_drafts_and_remaps_alignment(self):
        admin = as_viewer(self.admin)
        child = self.approved()
        kr = child.key_results.first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="80", confidence=3, update_text="Tien do tot")
        target = cycles.create_cycle(admin, {"name": "Q1", "cycle_type": "quarter", "start_date": "2027-01-01", "end_date": "2027-03-31"})
        result = cycles.clone_cycle(admin, self.cycle.pk, target.pk)
        self.assertEqual(result["objective_count"], 2)
        copies = {o.title: o for o in OKRObjective.objects.filter(cycle=target)}
        child_copy = copies[child.title]
        self.assertEqual(child_copy.parent_id, copies[self.company_objective.title].pk)
        self.assertEqual((child_copy.approval_status, child_copy.progress), (ApprovalStatus.DRAFT, Decimal("0")))
        copied_kr = child_copy.key_results.order_by("id").first()
        self.assertEqual(copied_kr.current_value, copied_kr.start_value)
        self.assertEqual(copied_kr.due_date, datetime.date(2027, 3, 31))
        self.assertOKRError("clone_target_not_empty", 409, cycles.clone_cycle, admin, self.cycle.pk, target.pk)
        self.assertOKRError("invalid_clone_target", 400, cycles.clone_cycle, admin, self.cycle.pk, self.cycle.pk)
        other = okr_cycle(self.other_company, "Q1 B", status="draft")
        self.assertOKRError("cycle_not_found", 404, cycles.clone_cycle, admin, self.cycle.pk, other.pk)


class DeletionAndNotificationTests(ServiceTestBase):
    def test_objective_with_checkins_cannot_be_deleted(self):
        objective = self.approved()
        kr = objective.key_results.first()
        okrs.checkin(as_viewer(self.owner), kr.pk, current_value="10", confidence=3, update_text="Bat dau")
        self.assertOKRError("objective_locked", 409, okrs.delete_objective, as_viewer(self.owner), objective.pk)
        self.assertOKRError("has_checkins", 409, okrs.delete_objective, as_viewer(self.admin), objective.pk)

    def test_deleted_objective_reports_deleted_and_its_notifications_go_stale(self):
        objective = self.draft()
        okrs.submit_objective(as_viewer(self.owner), objective.pk)
        okrs.reject_objective(as_viewer(self.manager), objective.pk, "Lam lai")
        okrs.delete_objective(as_viewer(self.owner), objective.pk)
        self.assertOKRError("deleted", 404, okrs.get_objective, as_viewer(self.owner), objective.pk)
        manager = as_viewer(self.manager)
        items = list(notifications.for_viewer(manager))
        self.assertTrue(items)
        availability = notifications.reference_availability(manager, items)
        self.assertEqual(set(availability.values()), {False})
        self.assertEqual(notifications.unread_count(manager), 0)

    def test_notifications_belong_to_their_recipient(self):
        objective = self.draft()
        okrs.submit_objective(as_viewer(self.owner), objective.pk)
        note = OKRNotification.objects.filter(recipient=self.manager).first()
        self.assertOKRError("not_found", 404, notifications.mark_read, as_viewer(self.colleague), note.pk)
        self.assertOKRError("not_found", 404, notifications.mark_read, as_viewer(self.outsider), note.pk)
        notifications.mark_read(as_viewer(self.manager), note.pk)
        note.refresh_from_db()
        self.assertTrue(note.is_read)

    def test_overdue_checkin_reminder_once_per_interval(self):
        objective = self.approved()
        stale, fresh = objective.key_results.order_by("id")
        OKRKeyResult.objects.filter(pk=stale.pk).update(created_at=timezone.now() - datetime.timedelta(days=9))
        okrs.checkin(as_viewer(self.owner), fresh.pk, current_value="20", confidence=3, update_text="Cap nhat")
        first = notifications.send_checkin_reminders(self.company.pk)
        again = notifications.send_checkin_reminders(self.company.pk)
        self.assertEqual((first, again), ({"checked": 1, "sent": 1}, {"checked": 1, "sent": 0}))
        reminder = OKRNotification.objects.get(notification_type="checkin_reminder")
        self.assertEqual((reminder.recipient_id, reminder.reference_id), (self.owner.pk, stale.pk))
        self.assertIn("quá hạn check-in 2 ngày", reminder.message)

    def test_dedupe_key_never_stores_twice(self):
        first = notifications.send(
            self.owner, company_id=self.company.pk, notification_type="info", title="A", dedupe_key="k1"
        )
        second = notifications.send(
            self.owner, company_id=self.company.pk, notification_type="info", title="B", dedupe_key="k1"
        )
        self.assertEqual(first.pk, second.pk)


class ReportTests(ServiceTestBase):
    def test_dashboard_is_the_viewers_own_overview(self):
        objective = self.approved()
        first_kr = objective.key_results.order_by("id").first()
        okrs.checkin(as_viewer(self.owner), first_kr.pk, current_value="100", confidence=5, update_text="Xong")
        self.draft(title="Ban nhap")
        data = reports.dashboard(as_viewer(self.owner), self.cycle)
        expected = calculator.expected_progress(self.cycle.start_date, self.cycle.end_date)
        self.assertEqual(data["objective_count"], 2)
        self.assertEqual(data["progress"], Decimal("50.00"))
        self.assertEqual(data["expected_progress"], expected)
        self.assertEqual(data["health"], calculator.status(Decimal("50"), expected, 15))
        self.assertEqual((data["key_results_completed"], data["key_results_total"]), (1, 2))
        self.assertEqual(data["pending_approval_count"], 0)

    def test_stale_key_results_need_attention(self):
        objective = self.approved()
        kr = objective.key_results.order_by("id").first()
        OKRKeyResult.objects.filter(pk=kr.pk).update(created_at=timezone.now() - datetime.timedelta(days=10))
        data = reports.dashboard(as_viewer(self.owner), self.cycle)
        stale = [item for item in data["attention"] if item["key_result_id"] == kr.pk]
        self.assertEqual(len(stale), 1)
        self.assertEqual((stale[0]["reason"], stale[0]["days_overdue"]), ("stale", 3))
        self.assertIsNone(stale[0]["last_checkin_at"])

    def test_approval_queue_lists_what_the_viewer_may_review(self):
        objective = self.draft()
        okrs.submit_objective(as_viewer(self.owner), objective.pk)
        queue = reports.approval_queue(as_viewer(self.manager))
        self.assertEqual([item["objective"].pk for item in queue], [objective.pk])
        item = queue[0]
        self.assertTrue(item["can_approve"])
        self.assertIsNone(item["block_reason"])
        self.assertEqual(item["checks"]["weight_total"], Decimal("100.00"))
        self.assertIsNotNone(item["submitted_at"])
        self.assertEqual(reports.approval_queue(as_viewer(self.colleague)), [])
        self.assertEqual(reports.approval_queue(as_viewer(self.outsider)), [])

    def test_team_report_covers_managed_people(self):
        self.approved()
        data = reports.team_report(as_viewer(self.manager), self.cycle)
        members = {m["employee"].pk: m for m in data["members"]}
        self.assertEqual(set(members), {self.owner.pk})
        self.assertTrue(members[self.owner.pk]["has_approved_okr"])
        self.assertEqual(data["member_count"], 1)
        admin_view = reports.team_report(as_viewer(self.admin), self.cycle)
        self.assertNotIn(self.outsider.pk, {m["employee"].pk for m in admin_view["members"]})
