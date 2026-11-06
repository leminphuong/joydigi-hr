"""Gate 1: OKR models, constraints, company isolation, soft delete, history."""

import shutil
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from okr.models import (
    AppendOnlyError,
    OKRActivity,
    OKRCheckin,
    OKRComment,
    OKRCycle,
    OKRDepartmentRole,
    OKREvidence,
    OKRIdentityMap,
    OKRKeyResult,
    OKRMigrationBatch,
    OKRObjective,
    OKRSettings,
    OKRTeam,
    OKRTeamMember,
    default_rating_ranges,
)
from okr.models.choices import ApprovalStatus, Scope
from okr.tests.factories import (
    okr_company,
    okr_cycle,
    okr_department,
    okr_employee,
    okr_key_result,
    okr_objective,
)


class OKRTestBase(TestCase):
    def setUp(self):
        self.company = okr_company("OKR Co A")
        self.other_company = okr_company("OKR Co B")
        self.department = okr_department(self.company)
        self.owner = okr_employee(self.company, "okr_owner", self.department)
        self.manager = okr_employee(self.company, "okr_manager", self.department)
        self.outsider = okr_employee(self.other_company, "okr_outsider")
        self.cycle = okr_cycle(self.company)
        self.other_cycle = okr_cycle(self.other_company, "Q4 B")

    def checkin(self, kr, **overrides):
        values = {
            "key_result": kr,
            "author": self.owner,
            "previous_value": Decimal("40"),
            "current_value": Decimal("60"),
            "previous_progress": Decimal("0"),
            "current_progress": Decimal("50"),
            "confidence": 4,
            "update_text": "Tien do tuan nay",
        }
        values.update(overrides)
        return OKRCheckin.objects.create(**values)


class ModelGraphTests(OKRTestBase):
    def test_full_graph_in_one_company(self):
        team = OKRTeam.objects.create(company=self.company, name="Team A", department=self.department, manager=self.manager)
        member = OKRTeamMember.objects.create(team=team, employee=self.owner)
        role = OKRDepartmentRole.objects.create(company=self.company, department=self.department, manager=self.manager)
        company_obj = okr_objective(self.cycle, self.manager, scope=Scope.COMPANY, title="Muc tieu cong ty")
        dept_obj = okr_objective(
            self.cycle, self.manager, scope=Scope.DEPARTMENT, department=self.department, parent=company_obj
        )
        team_obj = okr_objective(self.cycle, self.manager, scope=Scope.TEAM, team=team, parent=dept_obj)
        kr = okr_key_result(team_obj, self.owner)
        checkin = self.checkin(kr)
        comment = OKRComment.objects.create(objective=team_obj, author=self.manager, content="Tot")
        reply = OKRComment.objects.create(key_result=kr, author=self.owner, content="Cam on", parent=None)
        evidence = OKREvidence.objects.create(
            key_result=kr, checkin=checkin, author=self.owner, evidence_type="url",
            url="https://example.com/report", label="Bao cao",
        )
        activity = OKRActivity.objects.create(
            actor=self.owner, action="checked_in", key_result=kr, metadata={"checkin_id": checkin.pk}
        )
        settings_row = OKRSettings.objects.create(company=self.company)

        for row in (member, kr, checkin, comment, reply, evidence, activity):
            row.refresh_from_db()
            self.assertEqual(row.company_id, self.company.pk, type(row).__name__)
        self.assertEqual(role.company_id, self.company.pk)
        self.assertTrue(timezone.is_aware(checkin.created_at))
        self.assertTrue(timezone.is_aware(kr.updated_at))
        self.assertEqual(settings_row.warning_threshold, Decimal("15"))
        self.assertEqual(settings_row.reminder_frequency, "weekly")
        self.assertEqual(settings_row.rating_ranges, default_rating_ranges())
        self.assertEqual(list(dept_obj.children.all()), [team_obj])

    def test_decimal_values_round_trip_exactly(self):
        objective = okr_objective(self.cycle, self.owner, progress=Decimal("33.33"))
        kr = okr_key_result(
            objective, self.owner,
            start_value=Decimal("0.1234"), current_value=Decimal("98765432.1234"),
            target_value=Decimal("-5.5"), weight=Decimal("33.34"), progress=Decimal("99.99"),
        )
        objective.refresh_from_db()
        kr.refresh_from_db()
        self.assertEqual(objective.progress, Decimal("33.33"))
        self.assertEqual(kr.start_value, Decimal("0.1234"))
        self.assertEqual(kr.current_value, Decimal("98765432.1234"))
        self.assertEqual(kr.target_value, Decimal("-5.5"))
        self.assertEqual(kr.weight, Decimal("33.34"))
        self.assertIsInstance(kr.progress, Decimal)

    @skipUnless(connection.vendor == "postgresql", "SQLite stores decimals as REAL (~15 digits)")
    def test_full_18_digit_metric_precision_on_postgresql(self):
        """DECIMAL(18,4) like JOYOKR; only PostgreSQL's numeric keeps all 18 digits."""
        objective = okr_objective(self.cycle, self.owner)
        kr = okr_key_result(objective, self.owner, current_value=Decimal("12345678901234.5678"))
        kr.refresh_from_db()
        self.assertEqual(kr.current_value, Decimal("12345678901234.5678"))

    def test_legacy_timestamps_can_be_kept(self):
        legacy = timezone.make_aware(timezone.datetime(2026, 1, 2, 3, 4, 5))
        objective = okr_objective(self.cycle, self.owner, created_at=legacy, legacy_wp_id=101)
        objective.refresh_from_db()
        self.assertEqual(objective.created_at, legacy)


class CompanyIsolationTests(OKRTestBase):
    def assert_rejected(self, field, build):
        with self.assertRaises(ValidationError) as caught:
            build()
        self.assertIn(field, caught.exception.message_dict)

    def test_objective_cannot_use_another_companys_cycle(self):
        self.assert_rejected("cycle", lambda: okr_objective(self.other_cycle, self.owner, company=self.company))
        self.assertFalse(OKRObjective.all_objects.exists())

    def test_objective_owner_must_work_for_the_company(self):
        self.assert_rejected("owner", lambda: okr_objective(self.cycle, self.outsider))

    def test_parent_objective_must_be_in_the_same_company(self):
        foreign_parent = okr_objective(self.other_cycle, self.outsider)
        self.assert_rejected("parent", lambda: okr_objective(self.cycle, self.owner, parent=foreign_parent))

    def test_key_result_company_must_match_its_objective(self):
        objective = okr_objective(self.cycle, self.owner)
        self.assert_rejected(
            "objective", lambda: okr_key_result(objective, self.owner, company=self.other_company)
        )

    def test_key_result_owner_must_work_for_the_company(self):
        objective = okr_objective(self.cycle, self.owner)
        self.assert_rejected("owner", lambda: okr_key_result(objective, self.outsider))

    def test_department_must_be_linked_to_the_company(self):
        foreign_department = okr_department(self.other_company, "Phong B")
        self.assert_rejected(
            "department",
            lambda: okr_objective(self.cycle, self.owner, scope=Scope.DEPARTMENT, department=foreign_department),
        )

    def test_department_linked_to_no_company_is_shared(self):
        from base.models import Department

        shared = Department.objects.create(department="Phong dung chung")
        objective = okr_objective(self.cycle, self.owner, scope=Scope.DEPARTMENT, department=shared)
        self.assertEqual(objective.company_id, self.company.pk)

    def test_team_member_must_work_for_the_teams_company(self):
        team = OKRTeam.objects.create(company=self.company, name="Team A")
        self.assert_rejected("employee", lambda: OKRTeamMember.objects.create(team=team, employee=self.outsider))

    def test_checkin_author_must_work_for_the_company(self):
        kr = okr_key_result(okr_objective(self.cycle, self.owner), self.owner)
        self.assert_rejected("author", lambda: self.checkin(kr, author=self.outsider))

    def test_evidence_checkin_must_belong_to_the_same_key_result(self):
        objective = okr_objective(self.cycle, self.owner)
        kr_one = okr_key_result(objective, self.owner)
        kr_two = okr_key_result(objective, self.owner, title="KR hai")
        checkin = self.checkin(kr_one)
        evidence = OKREvidence(
            key_result=kr_two, checkin=checkin, author=self.owner, evidence_type="url",
            url="https://example.com/x", label="x",
        )
        with self.assertRaises(ValidationError) as caught:
            evidence.full_clean()
        self.assertIn("checkin", caught.exception.message_dict)

    def test_queries_must_name_a_company(self):
        mine = okr_objective(self.cycle, self.owner)
        okr_objective(self.other_cycle, self.outsider)
        self.assertEqual(list(OKRObjective.objects.for_company(self.company)), [mine])
        with self.assertRaises(ValueError):
            OKRObjective.objects.for_company(None)

    def test_identity_map_company_must_match_employee(self):
        mapping = OKRIdentityMap(wp_user_id=7, employee=self.owner, company=self.other_company)
        with self.assertRaises(ValidationError) as caught:
            mapping.full_clean()
        self.assertIn("company", caught.exception.message_dict)


class DatabaseConstraintTests(OKRTestBase):
    def assert_integrity_error(self, build):
        with self.assertRaises(IntegrityError), transaction.atomic():
            build()

    def test_cycle_cannot_end_before_it_starts(self):
        self.assert_integrity_error(
            lambda: okr_cycle(self.company, "Sai", start_date=date(2026, 12, 1), end_date=date(2026, 11, 1))
        )

    def test_cycle_and_objective_cannot_be_their_own_parent(self):
        cycle = okr_cycle(self.company, "Q1 2027", start_date=date(2027, 1, 1), end_date=date(2027, 3, 31))
        objective = okr_objective(self.cycle, self.owner)
        self.assert_integrity_error(lambda: OKRCycle.objects.filter(pk=cycle.pk).update(parent=cycle))
        self.assert_integrity_error(lambda: OKRObjective.objects.filter(pk=objective.pk).update(parent=objective))

    def test_scope_must_match_department_and_team(self):
        self.assert_integrity_error(lambda: okr_objective(self.cycle, self.owner, scope=Scope.DEPARTMENT))
        self.assert_integrity_error(lambda: okr_objective(self.cycle, self.owner, scope=Scope.TEAM))
        self.assert_integrity_error(
            lambda: okr_objective(self.cycle, self.owner, scope=Scope.PERSONAL, department=self.department)
        )

    def test_weights_and_progress_stay_within_0_and_100(self):
        self.assert_integrity_error(lambda: okr_objective(self.cycle, self.owner, weight=Decimal("100.01")))
        objective = okr_objective(self.cycle, self.owner)
        self.assert_integrity_error(lambda: okr_key_result(objective, self.owner, progress=Decimal("-0.01")))
        self.assert_integrity_error(lambda: okr_key_result(objective, self.owner, weight=Decimal("101")))

    def test_checkin_confidence_is_1_to_5(self):
        kr = okr_key_result(okr_objective(self.cycle, self.owner), self.owner)
        self.assert_integrity_error(lambda: self.checkin(kr, confidence=6))
        self.assert_integrity_error(lambda: self.checkin(kr, confidence=0))

    def test_comment_targets_exactly_one_object(self):
        objective = okr_objective(self.cycle, self.owner)
        kr = okr_key_result(objective, self.owner)
        self.assert_integrity_error(
            lambda: OKRComment.objects.create(objective=objective, key_result=kr, author=self.owner, content="x")
        )
        with self.assertRaises(ValidationError):
            OKRComment.objects.create(author=self.owner, content="khong co doi tuong")

    def test_evidence_needs_a_file_or_a_link(self):
        kr = okr_key_result(okr_objective(self.cycle, self.owner), self.owner)
        self.assert_integrity_error(
            lambda: OKREvidence.objects.create(key_result=kr, author=self.owner, evidence_type="file", label="x")
        )
        self.assert_integrity_error(
            lambda: OKREvidence.objects.create(key_result=kr, author=self.owner, evidence_type="url", label="x")
        )

    def test_activity_needs_a_target(self):
        self.assert_integrity_error(
            lambda: OKRActivity.objects.create(company=self.company, actor=self.owner, action="created")
        )

    def test_legacy_ids_are_unique(self):
        okr_objective(self.cycle, self.owner, legacy_wp_id=55)
        self.assert_integrity_error(lambda: okr_objective(self.cycle, self.owner, legacy_wp_id=55))

    def test_team_membership_is_unique(self):
        team = OKRTeam.objects.create(company=self.company, name="Team A")
        OKRTeamMember.objects.create(team=team, employee=self.owner)
        self.assert_integrity_error(lambda: OKRTeamMember.objects.create(team=team, employee=self.owner))

    def test_settings_threshold_and_rating_ranges(self):
        self.assert_integrity_error(
            lambda: OKRSettings.objects.create(company=self.company, warning_threshold=Decimal("150"))
        )
        settings_row = OKRSettings(company=self.company, rating_ranges=default_rating_ranges()[:3])
        with self.assertRaises(ValidationError):
            settings_row.full_clean()
        gap = default_rating_ranges()
        gap[1]["min"] = "0.35"
        settings_row.rating_ranges = gap
        with self.assertRaises(ValidationError):
            settings_row.full_clean()


class OwnershipRuleTests(OKRTestBase):
    def test_new_objective_and_key_result_need_an_owner(self):
        objective = OKRObjective(company=self.company, cycle=self.cycle, title="Khong chu")
        with self.assertRaises(ValidationError) as caught:
            objective.full_clean()
        self.assertIn("owner", caught.exception.message_dict)
        owned = okr_objective(self.cycle, self.owner)
        kr = OKRKeyResult(objective=owned, company=self.company, title="Khong chu")
        with self.assertRaises(ValidationError) as caught:
            kr.full_clean()
        self.assertIn("owner", caught.exception.message_dict)


class SoftDeleteTests(OKRTestBase):
    def test_soft_deleted_rows_are_hidden_but_kept(self):
        objective = okr_objective(self.cycle, self.owner)
        kr = okr_key_result(objective, self.owner)
        kr.soft_delete()
        objective.soft_delete()

        self.assertFalse(OKRObjective.objects.filter(pk=objective.pk).exists())
        self.assertTrue(OKRObjective.all_objects.filter(pk=objective.pk).exists())
        reloaded = OKRKeyResult.all_objects.get(pk=kr.pk)
        self.assertIsNotNone(reloaded.deleted_at)
        self.assertEqual(reloaded.approval_status, ApprovalStatus.DELETED)
        self.assertEqual(reloaded.objective.pk, objective.pk)  # forward FK still resolves
        self.assertEqual(objective.key_results.count(), 0)

    def test_soft_delete_is_idempotent(self):
        objective = okr_objective(self.cycle, self.owner)
        objective.soft_delete()
        first = OKRObjective.all_objects.get(pk=objective.pk).deleted_at
        objective.soft_delete()
        self.assertEqual(OKRObjective.all_objects.get(pk=objective.pk).deleted_at, first)


class AppendOnlyHistoryTests(OKRTestBase):
    def test_checkins_cannot_be_edited_or_deleted(self):
        kr = okr_key_result(okr_objective(self.cycle, self.owner), self.owner)
        checkin = self.checkin(kr)
        checkin.update_text = "sua lich su"
        with self.assertRaises(AppendOnlyError):
            checkin.save()
        with self.assertRaises(AppendOnlyError):
            checkin.delete()
        with self.assertRaises(AppendOnlyError):
            OKRCheckin.objects.filter(pk=checkin.pk).update(update_text="x")
        with self.assertRaises(AppendOnlyError):
            OKRCheckin.objects.filter(pk=checkin.pk).delete()
        self.assertEqual(OKRCheckin.objects.get(pk=checkin.pk).update_text, "Tien do tuan nay")

    def test_activity_cannot_be_edited_or_deleted(self):
        objective = okr_objective(self.cycle, self.owner)
        activity = OKRActivity.objects.create(actor=self.owner, action="created", objective=objective)
        with self.assertRaises(AppendOnlyError):
            activity.save()
        with self.assertRaises(AppendOnlyError):
            OKRActivity.objects.all().delete()

    def test_protected_history_blocks_hard_delete_of_key_result(self):
        kr = okr_key_result(okr_objective(self.cycle, self.owner), self.owner)
        self.checkin(kr)
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            OKRKeyResult.all_objects.filter(pk=kr.pk).delete()


class EvidenceStorageTests(OKRTestBase):
    def setUp(self):
        super().setUp()
        self.private_root = tempfile.mkdtemp(prefix="okr_private_")
        self.media_root = tempfile.mkdtemp(prefix="okr_media_")
        self.addCleanup(shutil.rmtree, self.private_root, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)

    def test_files_are_stored_outside_public_media_without_a_url(self):
        kr = okr_key_result(okr_objective(self.cycle, self.owner), self.owner)
        with override_settings(OKR_PRIVATE_MEDIA_ROOT=self.private_root, MEDIA_ROOT=self.media_root):
            evidence = OKREvidence(
                key_result=kr, author=self.owner, evidence_type="file",
                label="Bien ban", original_name="bien-ban-hop.pdf",
            )
            evidence.file.save("bien-ban-hop.pdf", ContentFile(b"%PDF-1.4 fake"), save=True)
            stored = Path(evidence.file.path)

            self.assertTrue(stored.is_file())
            self.assertTrue(str(stored).startswith(str(Path(self.private_root).resolve())))
            self.assertFalse(any(Path(self.media_root).rglob("*")))
            self.assertNotIn("bien-ban-hop", evidence.file.name)
            self.assertTrue(evidence.file.name.startswith(f"evidence/{self.company.pk}/"))
            with self.assertRaises(ValueError):
                evidence.file.url


class MigrationBatchTests(TestCase):
    def test_batch_defaults(self):
        batch = OKRMigrationBatch.objects.create(source_version="1.6.45")
        self.assertEqual(batch.mode, "dry_run")
        self.assertEqual(batch.status, "pending")
        self.assertEqual(batch.stats, {})
