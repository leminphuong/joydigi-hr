"""Gate 1: the OKR module must not change HR behaviour or HR schema."""

from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.models import ProtectedError
from django.test import TestCase

from employee.models import Employee
from employee.services import account_deletion
from okr.models import OKRCheckin, OKRComment, OKRKeyResult, OKRObjective, OKRTeam, OKRTeamMember
from okr.models.choices import Scope
from okr.tests.factories import (
    okr_company,
    okr_cycle,
    okr_department,
    okr_employee,
    okr_key_result,
    okr_objective,
)


class EmployeeDeletionContractTests(TestCase):
    """HR's account deletion derives its plan from Employee's relations;
    every OKR relation has to fit that contract."""

    def okr_relations(self):
        for relation in Employee._meta.related_objects:
            if relation.related_model._meta.app_label == "okr":
                yield relation

    def test_okr_relations_to_employee_are_visible_to_hr(self):
        names = {(r.related_model.__name__, r.field.name) for r in self.okr_relations()}
        self.assertIn(("OKRObjective", "owner"), names)
        self.assertIn(("OKRCheckin", "author"), names)
        self.assertIn(("OKRTeamMember", "employee"), names)

    def test_protected_okr_relations_are_nullable_and_detached(self):
        detach = {(m._meta.label, f) for m, f, _b in account_deletion.DETACH_RELATIONS}
        for relation in self.okr_relations():
            behaviour = account_deletion._on_delete_name(relation)
            if behaviour in ("PROTECT", "DO_NOTHING"):
                self.assertTrue(relation.field.null, relation.field)
                self.assertIn((relation.related_model._meta.label, relation.field.name), detach)

    def test_team_membership_is_owned_by_the_employee(self):
        owned = {(m._meta.label, f) for m, f, _b in account_deletion.OWNED_CASCADE_RELATIONS}
        self.assertIn(("okr.OKRTeamMember", "employee"), owned)


class EmployeeDeletionBehaviourTests(TestCase):
    def test_deleting_an_employee_keeps_okr_history_and_drops_membership(self):
        company = okr_company("Del Co")
        leaver = okr_employee(company, "okr_leaver")
        stayer = okr_employee(company, "okr_stayer")
        cycle = okr_cycle(company)
        objective = okr_objective(cycle, leaver)
        kr = okr_key_result(objective, leaver)
        checkin = OKRCheckin.objects.create(
            key_result=kr, author=leaver, previous_value=40, current_value=50,
            previous_progress=0, current_progress=25, confidence=3, update_text="x",
        )
        comment = OKRComment.objects.create(objective=objective, author=stayer, content="giu lai")
        team = OKRTeam.objects.create(company=company, name="T", manager=leaver)
        OKRTeamMember.objects.create(team=team, employee=leaver)

        result = account_deletion.delete_employees(Employee.objects.filter(pk=leaver.pk))

        self.assertEqual(result["deleted_count"], 1)
        self.assertFalse(Employee.objects.filter(pk=leaver.pk).exists())
        self.assertIsNone(OKRObjective.all_objects.get(pk=objective.pk).owner_id)
        self.assertIsNone(OKRKeyResult.all_objects.get(pk=kr.pk).owner_id)
        self.assertIsNone(OKRCheckin._base_manager.get(pk=checkin.pk).author_id)
        self.assertEqual(OKRComment.objects.get(pk=comment.pk).author_id, stayer.pk)
        self.assertIsNone(OKRTeam.objects.get(pk=team.pk).manager_id)
        self.assertFalse(OKRTeamMember.objects.filter(team=team).exists())

    def test_deleting_an_employee_removes_their_okr_roles_and_inbox(self):
        from okr.models import OKRNotification, OKRRole
        from okr.tests.factories import okr_role

        company = okr_company("Del Role Co")
        leaver = okr_employee(company, "okr_role_leaver")
        stayer = okr_employee(company, "okr_role_stayer")
        okr_role(leaver, "admin")
        OKRNotification.objects.create(company=company, recipient=leaver, notification_type="info", title="x")
        kept = OKRNotification.objects.create(company=company, recipient=stayer, notification_type="info", title="y")
        account_deletion.delete_employees(Employee.objects.filter(pk=leaver.pk))
        self.assertFalse(OKRRole.objects.filter(employee_id=leaver.pk).exists())
        self.assertEqual(list(OKRNotification.objects.values_list("pk", flat=True)), [kept.pk])

    def test_hr_department_in_use_by_okr_cannot_be_deleted(self):
        company = okr_company("Dept Co")
        department = okr_department(company)
        owner = okr_employee(company, "okr_dept_owner")
        okr_objective(okr_cycle(company), owner, scope=Scope.DEPARTMENT, department=department)
        with self.assertRaises(ProtectedError):
            department.delete()


class SchemaBoundaryTests(TestCase):
    def test_okr_migrations_only_touch_okr_tables(self):
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        okr_nodes = [key for key in loader.disk_migrations if key[0] == "okr"]
        self.assertTrue(okr_nodes)
        for key in okr_nodes:
            migration = loader.disk_migrations[key]
            for operation in migration.operations:
                self.assertIn(
                    type(operation).__name__,
                    {"CreateModel", "AddField", "AddIndex", "AddConstraint", "AlterField",
                     "RemoveIndex", "RemoveConstraint", "AlterModelOptions"},
                    f"{key}: {type(operation).__name__} needs a manual review",
                )
        for (app_label, _name), migration in loader.disk_migrations.items():
            if app_label != "okr":
                self.assertFalse(
                    any(dep[0] == "okr" for dep in migration.dependencies),
                    f"{app_label} must not depend on okr",
                )

    def test_every_okr_table_is_prefixed(self):
        for model in apps.get_app_config("okr").get_models():
            self.assertTrue(model._meta.db_table.startswith("okr_"), model._meta.db_table)

    def test_no_pending_okr_migrations(self):
        call_command("makemigrations", "okr", "--check", "--dry-run", verbosity=0)
