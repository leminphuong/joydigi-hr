"""Phase EMPLOYEE-BULK-DELETE-2 — permanent employee account deletion.

Audit EMPLOYEE-BULK-DELETE-AUDIT-1 found the existing bulk delete both
ineffective and unsafe: seventeen ``PROTECT`` relations meant anyone who had
ever clocked in could not be deleted at all, the loop caught that per employee
and carried on — leaving a partial batch — and the endpoint answered
``{"message": "Success"}`` regardless. It also accepted ``GET``.

These tests pin the behaviour that repair has to keep true. The interesting
ones are not "deleting works" but the three ways deletion can quietly corrupt
data: destroying a colleague's record because the departing employee approved
it (E), destroying a subordinate because their manager left (D), and leaving a
row pointing at an employee who no longer exists (F).
"""

from datetime import date, time

from django.contrib.auth.models import Permission
from django.test import Client, TestCase

from attendance.models import (
    Attendance,
    AttendanceActivity,
    AttendanceLateComeEarlyOut,
    AttendanceOverTime,
    OvertimeRequest,
)
from base.models import (
    CheckInLocation,
    CheckInPolicy,
    Company,
    Department,
    EmployeeShift,
    OfficeWifi,
)
from employee.models import Employee, EmployeeWorkInformation
from employee.services import account_deletion
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_auth.models import JoydigiUser

URL = "/employee/employee-bulk-delete/"


class BulkDeleteBase(TestCase):
    """One admin who may delete, inside one company scope."""

    def setUp(self):
        self.client = Client()
        self.company = make_company("Bulk Delete Co")

        self.admin_user = make_user("bd_admin", password="secret123")
        self.admin = make_employee(
            company=self.company,
            email="bd_admin@test.joydigi",
            user=self.admin_user,
        )
        self.admin_user.user_permissions.add(
            Permission.objects.get(
                codename="delete_employee", content_type__app_label="employee"
            )
        )
        self.client.login(username="bd_admin", password="secret123")
        self._select_company(self.company)

    def _select_company(self, company):
        session = self.client.session
        session["selected_company"] = str(company.id)
        session.save()

    def employee(self, slug, company=None):
        user = make_user(slug, password="secret123")
        return make_employee(
            company=company or self.company,
            email=f"{slug}@test.joydigi",
            user=user,
        )

    def preview(self, employees):
        return self.client.post(
            URL,
            {"action": "preview", "ids": _ids(employees)},
        )

    def delete(self, employees, confirmation=None):
        if confirmation is None:
            confirmation = account_deletion.confirmation_phrase(len(employees))
        return self.client.post(
            URL,
            {
                "action": "delete",
                "ids": _ids(employees),
                "confirmation": confirmation,
            },
        )

    def assert_gone(self, employee, user_pk):
        self.assertFalse(Employee.objects.filter(pk=employee.pk).exists())
        self.assertFalse(
            JoydigiUser.objects.filter(pk=user_pk).exists(),
            msg="a login-capable orphan user was left behind",
        )


def _ids(employees):
    import json

    return json.dumps(
        [e.id if hasattr(e, "id") else int(e) for e in employees]
    )


def _attendance(employee, day, **extra):
    return Attendance.objects.create(
        employee_id=employee,
        attendance_date=day,
        attendance_clock_in_date=day,
        attendance_clock_in=time(9, 0),
        **extra,
    )


class DeleteWithoutHistoryTests(BulkDeleteBase):
    """TEST A"""

    def test_employee_and_user_are_both_gone(self):
        target = self.employee("bd_clean")
        user_pk = target.employee_user_id.pk

        response = self.delete([target])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"success": True, "deleted_count": 1}
        )
        self.assert_gone(target, user_pk)


class DeleteOwnedHistoryTests(BulkDeleteBase):
    """TESTS B, C, F — the PROTECT and DO_NOTHING relations that used to make
    deletion impossible or unclean."""

    def test_attendance_no_longer_blocks_deletion(self):
        """TEST B — this is the case that previously always failed."""
        target = self.employee("bd_worker")
        user_pk = target.employee_user_id.pk
        _attendance(target, date(2026, 1, 5))
        _attendance(target, date(2026, 1, 6))

        response = self.delete([target])

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["success"])
        self.assertFalse(
            Attendance.objects.filter(employee_id_id=target.id).exists()
        )
        self.assert_gone(target, user_pk)

    def test_overtime_activity_and_requests_are_removed(self):
        """TEST C"""
        target = self.employee("bd_ot")
        user_pk = target.employee_user_id.pk
        AttendanceActivity.objects.create(
            employee_id=target, attendance_date=date(2026, 2, 2),
            clock_in_date=date(2026, 2, 2), clock_in=time(8, 30),
        )
        AttendanceOverTime.objects.create(employee_id=target, month="february")
        OvertimeRequest.objects.create(
            employee_id=target,
            request_date=date(2026, 2, 3),
            start_time=time(18, 0),
            end_time=time(20, 0),
        )

        self.assertTrue(self.delete([target]).json()["success"])

        self.assertFalse(
            AttendanceActivity.objects.filter(employee_id_id=target.id).exists()
        )
        self.assertFalse(
            AttendanceOverTime.objects.filter(employee_id_id=target.id).exists()
        )
        self.assertFalse(
            OvertimeRequest.objects.filter(employee_id_id=target.id).exists()
        )
        self.assert_gone(target, user_pk)

    def test_do_nothing_relation_leaves_no_dangling_row(self):
        """TEST F — ``AttendanceLateComeEarlyOut.employee_id`` is
        ``DO_NOTHING``: the database neither blocks the delete nor clears the
        column, so without explicit handling the row survives pointing at an
        employee id that no longer exists."""
        target = self.employee("bd_late")
        attendance = _attendance(target, date(2026, 3, 1))
        # Not ``objects.create()``: this model's ``save()`` calls
        # ``super().save(*args, **kwargs)`` twice, so ``create()``'s
        # ``force_insert=True`` is replayed on the second call and the insert
        # collides with itself. A pre-existing defect in
        # ``attendance.models``, unrelated to this phase and left alone.
        late = AttendanceLateComeEarlyOut(
            employee_id=target, attendance_id=attendance, type="late_come"
        )
        late.save()

        self.assertTrue(self.delete([target]).json()["success"])

        self.assertFalse(
            AttendanceLateComeEarlyOut.objects.filter(
                employee_id_id=target.id
            ).exists(),
            msg="a row is still pointing at the deleted employee",
        )
        # And nothing was left behind pointing at *any* missing employee.
        live_ids = set(Employee.objects.all().values_list("id", flat=True))
        orphans = [
            row
            for row in AttendanceLateComeEarlyOut.objects.all().values_list(
                "employee_id_id", flat=True
            )
            if row is not None and row not in live_ids
        ]
        self.assertEqual(orphans, [])


class CrossEmployeeReferenceTests(BulkDeleteBase):
    """TESTS D, E — the two ways a naive delete destroys somebody else's
    data."""

    def test_a_subordinate_survives_their_manager_being_deleted(self):
        """TEST D — ``reporting_manager_id`` is ``PROTECT``, so deleting a
        manager used to be refused outright. Detaching is right; cascading
        would delete the subordinate, which would be a disaster."""
        manager = self.employee("bd_manager")
        manager_user_pk = manager.employee_user_id.pk
        subordinate = self.employee("bd_report")
        EmployeeWorkInformation.objects.filter(
            employee_id=subordinate
        ).update(reporting_manager_id=manager)

        self.assertTrue(self.delete([manager]).json()["success"])

        self.assertTrue(Employee.objects.filter(pk=subordinate.pk).exists())
        self.assertIsNone(
            EmployeeWorkInformation.objects.get(
                employee_id=subordinate
            ).reporting_manager_id,
            msg="the subordinate must be detached, not deleted",
        )
        self.assert_gone(manager, manager_user_pk)

    def test_another_employees_attendance_survives_its_approver(self):
        """TEST E — the distinction the phase calls mandatory:
        ``Attendance.employee_id`` is ownership, ``Attendance.approved_by`` is
        not."""
        approver = self.employee("bd_approver")
        worker = self.employee("bd_worked")
        record = _attendance(worker, date(2026, 4, 1), approved_by=approver)

        self.assertTrue(self.delete([approver]).json()["success"])

        record.refresh_from_db()
        self.assertTrue(Employee.objects.filter(pk=worker.pk).exists())
        self.assertIsNone(record.approved_by)
        self.assertEqual(record.employee_id_id, worker.id)


class BatchAtomicityTests(BulkDeleteBase):
    """TESTS G, H — all or nothing."""

    def test_a_whole_batch_is_deleted_together(self):
        """TEST G"""
        targets = [self.employee(f"bd_batch{i}") for i in range(3)]
        user_pks = [t.employee_user_id.pk for t in targets]
        _attendance(targets[0], date(2026, 5, 1))

        response = self.delete(targets)

        self.assertEqual(
            response.json(), {"success": True, "deleted_count": 3}
        )
        for target, user_pk in zip(targets, user_pks):
            self.assert_gone(target, user_pk)

    def test_one_protected_member_stops_the_whole_batch(self):
        """TEST H — the failure the old loop produced silently."""
        deletable = self.employee("bd_ok")
        protected_user = make_user("bd_super", password="secret123", is_superuser=True)
        protected = make_employee(
            company=self.company,
            email="bd_super@test.joydigi",
            user=protected_user,
        )

        response = self.delete([deletable, protected])

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["deleted_count"], 0)
        self.assertTrue(Employee.objects.filter(pk=deletable.pk).exists())
        self.assertTrue(Employee.objects.filter(pk=protected.pk).exists())


class ScopeAndProtectionTests(BulkDeleteBase):
    """TESTS I, J, K"""

    def test_an_id_from_another_company_is_rejected(self):
        """TEST I — ids arrive from the browser and can be edited by hand."""
        other_company = make_company(
            "Other Co", address="9 Elsewhere", city="SF", zip="94105"
        )
        outsider = self.employee("bd_outsider", company=other_company)
        insider = self.employee("bd_insider")

        response = self.delete([insider, outsider])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["deleted_count"], 0)
        self.assertEqual(
            {e["code"] for e in response.json()["errors"]}, {"NOT_FOUND"}
        )
        self.assertTrue(Employee.objects.filter(pk=insider.pk).exists())
        self.assertTrue(Employee.objects.filter(pk=outsider.pk).exists())

    def test_deleting_yourself_is_refused(self):
        """TEST J"""
        response = self.delete([self.admin])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            {e["code"] for e in response.json()["errors"]}, {"SELF"}
        )
        self.assertTrue(Employee.objects.filter(pk=self.admin.pk).exists())

    def test_deleting_a_superuser_is_refused(self):
        """TEST K"""
        super_user = make_user("bd_root", password="secret123", is_superuser=True)
        target = make_employee(
            company=self.company, email="bd_root@test.joydigi", user=super_user
        )

        response = self.delete([target])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            {e["code"] for e in response.json()["errors"]}, {"SUPERUSER"}
        )
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())


class RequestContractTests(BulkDeleteBase):
    """TESTS L, M, N — the guards around the destructive call."""

    def test_get_cannot_delete(self):
        """TEST L — this decorator was missing while both sibling views had
        it."""
        target = self.employee("bd_get")

        response = self.client.get(URL, {"ids": _ids([target])})

        self.assertEqual(response.status_code, 405)
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())

    def test_a_wrong_confirmation_phrase_deletes_nothing(self):
        """TEST M"""
        target = self.employee("bd_phrase")

        response = self.delete([target], confirmation="XOA 2 TAI KHOAN")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["errors"][0]["code"], "CONFIRMATION_MISMATCH"
        )
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())

    def test_an_empty_confirmation_deletes_nothing(self):
        target = self.employee("bd_nophrase")

        self.assertEqual(self.delete([target], confirmation="").status_code, 400)
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())

    def test_preview_changes_nothing(self):
        """TEST N"""
        target = self.employee("bd_preview")
        _attendance(target, date(2026, 6, 1))
        before = (
            Employee.objects.count(),
            JoydigiUser.objects.count(),
            Attendance.objects.count(),
        )

        response = self.preview([target])
        body = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["success"])
        self.assertEqual(body["selected_count"], 1)
        self.assertEqual(body["employees"][0]["attendance_count"], 1)
        self.assertEqual(body["confirmation_phrase"], "XOA 1 TAI KHOAN")
        self.assertEqual(
            before,
            (
                Employee.objects.count(),
                JoydigiUser.objects.count(),
                Attendance.objects.count(),
            ),
        )

    def test_an_unknown_action_previews_rather_than_deletes(self):
        """A cached copy of the old script posts no action at all. The safe
        reading of an unrecognised request is "show me", not "destroy"."""
        target = self.employee("bd_legacy")

        response = self.client.post(URL, {"ids": _ids([target])})

        self.assertEqual(response.json()["action"], "preview")
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())

    def test_a_user_without_the_permission_cannot_delete(self):
        target = self.employee("bd_guarded")
        other = make_user("bd_nopriv", password="secret123")
        make_employee(
            company=self.company, email="bd_nopriv@test.joydigi", user=other
        )
        self.client.logout()
        self.client.login(username="bd_nopriv", password="secret123")

        response = self.delete([target])

        self.assertNotEqual(response.status_code, 200)
        self.assertTrue(Employee.objects.filter(pk=target.pk).exists())


class SharedDataTests(BulkDeleteBase):
    """TEST O — the company keeps its configuration."""

    def test_company_configuration_survives(self):
        department = Department.objects.create(department="Engineering")
        shift = EmployeeShift.objects.create(employee_shift="Morning")
        location = CheckInLocation.objects.create(
            company_id=self.company, name="HQ", latitude=10, longitude=106
        )
        policy = CheckInPolicy.objects.create(company_id=self.company)
        wifi = OfficeWifi.objects.create(
            company_id=self.company, name="Office", ssid="joydigi"
        )
        target = self.employee("bd_shared")
        EmployeeWorkInformation.objects.filter(employee_id=target).update(
            department_id=department, shift_id=shift
        )
        _attendance(target, date(2026, 7, 1))

        self.assertTrue(self.delete([target]).json()["success"])

        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())
        self.assertTrue(Department.objects.filter(pk=department.pk).exists())
        self.assertTrue(EmployeeShift.objects.filter(pk=shift.pk).exists())
        self.assertTrue(CheckInLocation.objects.filter(pk=location.pk).exists())
        self.assertTrue(CheckInPolicy.objects.filter(pk=policy.pk).exists())
        self.assertTrue(OfficeWifi.objects.filter(pk=wifi.pk).exists())

    def test_other_employees_are_untouched(self):
        target = self.employee("bd_one")
        bystander = self.employee("bd_two")

        self.assertTrue(self.delete([target]).json()["success"])

        self.assertTrue(Employee.objects.filter(pk=bystander.pk).exists())
        self.assertTrue(Employee.objects.filter(pk=self.admin.pk).exists())


class RelationClassificationTests(TestCase):
    """The service derives its plan from the schema rather than a fixed list,
    so these assert the derivation itself — a new relation added later is
    either handled or loudly refused, never skipped."""

    def test_every_blocking_relation_is_either_owned_or_detached(self):
        handled = {
            (model._meta.label, field)
            for model, field, _b in account_deletion.OWNED_BLOCKING_RELATIONS
        } | {
            (model._meta.label, field)
            for model, field, _b in account_deletion.DETACH_RELATIONS
        }
        blocking = set()
        for relation in Employee._meta.related_objects:
            if relation.field.many_to_many:
                continue
            handler = relation.field.remote_field.on_delete
            if getattr(handler, "__name__", "") in ("PROTECT", "DO_NOTHING"):
                blocking.add(
                    (relation.related_model._meta.label, relation.field.name)
                )
        self.assertEqual(blocking - handled, set())

    def test_approval_fields_are_never_treated_as_ownership(self):
        owned_fields = {
            field
            for _m, field, _b in account_deletion.OWNED_BLOCKING_RELATIONS
        }
        for cross_reference in (
            "approved_by",
            "created_by",
            "acted_by",
            "reporting_manager_id",
            "reallocate_to",
        ):
            self.assertNotIn(cross_reference, owned_fields)

    def test_every_detach_target_can_actually_be_nulled(self):
        """The alternative would be inventing a replacement value, which the
        phase forbids; this proves the refusal branch is never reached."""
        for model, field_name, _b in account_deletion.DETACH_RELATIONS:
            self.assertTrue(
                model._meta.get_field(field_name).null,
                msg=f"{model._meta.label}.{field_name} cannot be detached",
            )

    def test_shared_objects_are_not_reachable_for_deletion(self):
        """Deletion only ever walks *reverse* relations from Employee, so a
        shared object it points *at* — company, department, shift — is never a
        candidate."""
        touched = {
            model._meta.label
            for model, _f, _b in account_deletion.OWNED_RELATIONS
        } | {
            model._meta.label
            for model, _f, _b in account_deletion.DETACH_RELATIONS
        }
        for shared in (
            "base.Company",
            "base.Department",
            "base.JobPosition",
            "base.EmployeeShift",
            "base.CheckInLocation",
            "base.CheckInPolicy",
            "base.OfficeWifi",
            "leave.LeaveType",
            "base.Holidays",
        ):
            self.assertNotIn(shared, touched)
