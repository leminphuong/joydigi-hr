"""Phase EMPLOYEE-FORM-CLEANUP-1 — fields removed from the employee forms.

Removing a field from a template alone is not enough: the form still validates
it, so a field that was required simply stops being fillable and the page can
never be saved. These tests therefore check both halves — the field is gone
from the form (so no validation remains) and the add/edit screens still save.

``EmployeeBankDetailsForm`` is the case that would have broken: it declared
``address = forms.CharField(...)``, required by default, with no input left on
the page to satisfy it.
"""

from django.contrib.auth.models import Group, Permission
from django.test import Client, TestCase

from base.models import CompanyGroupAssignment, Department, JobPosition
from base.roles import ADMIN_ROLE, ensure_standard_roles
from employee.forms import (
    EmployeeBankDetailsForm,
    EmployeeBankDetailsUpdateForm,
    EmployeeWorkInformationForm,
    EmployeeWorkInformationUpdateForm,
)
from employee.models import Employee, EmployeeBankDetails, EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user

REMOVED_WORK_FIELDS = ["job_role_id"]
REMOVED_BANK_FIELDS = [
    "any_other_code1",
    "any_other_code2",
    "address",
    "country",
    "state",
    "city",
]
KEPT_WORK_FIELDS = ["department_id", "job_position_id"]
KEPT_BANK_FIELDS = ["bank_name", "account_number", "branch"]


class FormFieldTests(TestCase):
    """The forms themselves — where the validation lives."""

    def test_work_forms_no_longer_carry_job_role(self):
        for form_cls in (
            EmployeeWorkInformationForm,
            EmployeeWorkInformationUpdateForm,
        ):
            fields = form_cls().fields
            for name in REMOVED_WORK_FIELDS:
                self.assertNotIn(name, fields, msg=form_cls.__name__)

    def test_work_forms_keep_department_and_job_position(self):
        for form_cls in (
            EmployeeWorkInformationForm,
            EmployeeWorkInformationUpdateForm,
        ):
            fields = form_cls().fields
            for name in KEPT_WORK_FIELDS:
                self.assertIn(name, fields, msg=form_cls.__name__)

    def test_bank_forms_stop_at_branch(self):
        for form_cls in (
            EmployeeBankDetailsForm,
            EmployeeBankDetailsUpdateForm,
        ):
            fields = form_cls().fields
            for name in REMOVED_BANK_FIELDS:
                self.assertNotIn(name, fields, msg=form_cls.__name__)
            for name in KEPT_BANK_FIELDS:
                self.assertIn(name, fields, msg=form_cls.__name__)

    def test_no_removed_field_can_still_demand_a_value(self):
        """The specific failure mode: a required field with no input left."""
        for form_cls in (
            EmployeeWorkInformationForm,
            EmployeeWorkInformationUpdateForm,
            EmployeeBankDetailsForm,
            EmployeeBankDetailsUpdateForm,
        ):
            required = {
                name
                for name, field in form_cls().fields.items()
                if field.required
            }
            for name in REMOVED_WORK_FIELDS + REMOVED_BANK_FIELDS:
                self.assertNotIn(name, required, msg=form_cls.__name__)

    def test_the_salary_field_is_labelled_as_a_daily_amount(self):
        for form_cls in (
            EmployeeWorkInformationForm,
            EmployeeWorkInformationUpdateForm,
        ):
            self.assertEqual(
                str(form_cls().fields["salary_hour"].label),
                "Lương ngày",
                msg=form_cls.__name__,
            )

    def test_the_bank_form_declares_no_extra_required_address(self):
        """``address`` used to be declared on the form as a required
        ``CharField``, independent of the model. Leaving that behind would
        have made every bank save fail with "Trường này là bắt buộc"."""
        self.assertNotIn("address", EmployeeBankDetailsForm.declared_fields)


class SaveWithoutRemovedFieldsTests(TestCase):
    """Bound forms with only the surviving inputs must validate."""

    def setUp(self):
        self.company = make_company("Form Cleanup Co")
        self.employee = make_employee(
            company=self.company,
            email="fc_target@test.joydigi",
            user=make_user("fc_target", password="secret123"),
        )

    def test_bank_details_save_with_only_the_three_remaining_fields(self):
        form = EmployeeBankDetailsForm(
            {
                "bank_name": "Vietcombank",
                "account_number": "0123456789",
                "branch": "Ha Noi",
            }
        )
        self.assertTrue(form.is_valid(), msg=form.errors.as_json())

        instance = form.save(commit=False)
        instance.employee_id = self.employee
        instance.save()

        stored = EmployeeBankDetails.objects.get(employee_id=self.employee)
        self.assertEqual(stored.bank_name, "Vietcombank")
        self.assertIsNone(stored.any_other_code1)
        self.assertIsNone(stored.address)

    def test_bank_update_form_saves_without_the_removed_fields(self):
        form = EmployeeBankDetailsUpdateForm(
            {
                "bank_name": "BIDV",
                "account_number": "999888777",
                "branch": "Da Nang",
            }
        )
        self.assertTrue(form.is_valid(), msg=form.errors.as_json())

    def test_work_information_saves_without_job_role(self):
        department = Department.objects.create(department="FC Dept")
        position = JobPosition.objects.create(
            job_position="FC Position", department_id=department
        )
        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)

        form = EmployeeWorkInformationUpdateForm(
            {
                "department_id": department.id,
                "job_position_id": position.id,
                "company_id": self.company.id,
                "salary_hour": 500000,
                "basic_salary": 0,
            },
            instance=info,
        )
        self.assertTrue(form.is_valid(), msg=form.errors.as_json())

        saved = form.save()
        self.assertEqual(saved.salary_hour, 500000)
        self.assertEqual(saved.department_id_id, department.id)
        self.assertEqual(saved.job_position_id_id, position.id)

    def test_an_existing_job_role_value_is_left_alone(self):
        """The column still exists; excluding the field must not blank it."""
        from base.models import JobRole

        department = Department.objects.create(department="FC Dept 2")
        position = JobPosition.objects.create(
            job_position="FC Position 2", department_id=department
        )
        role = JobRole.objects.create(job_role="FC Role", job_position_id=position)
        info = EmployeeWorkInformation.objects.get(employee_id=self.employee)
        EmployeeWorkInformation.objects.filter(pk=info.pk).update(
            job_role_id=role
        )

        info.refresh_from_db()
        form = EmployeeWorkInformationUpdateForm(
            {
                "department_id": department.id,
                "job_position_id": position.id,
                "company_id": self.company.id,
                "salary_hour": 700000,
                "basic_salary": 0,
            },
            instance=info,
        )
        self.assertTrue(form.is_valid(), msg=form.errors.as_json())
        saved = form.save()

        self.assertEqual(
            saved.job_role_id_id,
            role.id,
            msg="excluding the field must not clear the stored value",
        )


class ScreenTests(TestCase):
    """The add and edit screens, rendered and posted through the real URLs."""

    def setUp(self):
        self.client = Client()
        self.company = make_company("Screen Co")
        self.admin_user = make_user("fc_admin", password="secret123")
        self.admin = make_employee(
            company=self.company,
            email="fc_admin@test.joydigi",
            user=self.admin_user,
        )
        for codename in (
            "add_employee",
            "change_employee",
            "view_employee",
            "change_employeeworkinformation",
            "change_employeebankdetails",
        ):
            try:
                self.admin_user.user_permissions.add(
                    Permission.objects.get(
                        codename=codename, content_type__app_label="employee"
                    )
                )
            except Permission.DoesNotExist:
                pass
        ensure_standard_roles()
        group = Group.objects.get(name=ADMIN_ROLE)
        self.admin_user.groups.add(group)
        CompanyGroupAssignment.objects.get_or_create(
            user=self.admin_user, company=self.company, group=group
        )
        self.client.login(username="fc_admin", password="secret123")
        session = self.client.session
        session["selected_company"] = str(self.company.id)
        session.save()

        self.target = make_employee(
            company=self.company,
            email="fc_edit@test.joydigi",
            user=make_user("fc_edit", password="secret123"),
        )

    def test_creating_an_employee_succeeds(self):
        before = Employee.objects.count()

        response = self.client.post(
            "/employee/employee-create-personal-info/",
            {
                "employee_first_name": "Nguyen",
                "employee_last_name": "Van A",
                "email": "fc_new@test.joydigi",
                "phone": "0900000001",
                # Required by EmployeeForm before this phase and untouched by
                # it — included so the test exercises the removal, not an
                # unrelated missing value.
                "gender": "male",
            },
        )

        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(Employee.objects.count(), before + 1)
        created = Employee.objects.get(email="fc_new@test.joydigi")
        self.assertEqual(created.employee_first_name, "Nguyen")

    def test_the_edit_screen_renders_without_the_removed_fields(self):
        response = self.client.get(
            f"/employee/employee-view-update/{self.target.id}/"
        )
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        # Deliberately not asserting on ``id_address`` or ``id_city``: the
        # personal-information tab has its own address and city inputs with
        # the same ids, and those must stay. The bank block is identified by
        # markers unique to it instead.
        for marker in (
            'id="id_job_role_id"',
            'id="id_any_other_code1"',
            'id="id_any_other_code2"',
            'id="bank_country"',
            'id="bank_state"',
            "Bank Address",
            "Bank Code",
        ):
            self.assertNotIn(marker, body, msg=marker)

        # Kept, per the phase brief.
        self.assertIn("id_department_id", body)
        self.assertIn("id_job_position_id", body)
        self.assertIn('id="id_bank_name"', body)
        self.assertIn('id="id_account_number"', body)
        self.assertIn('id="id_branch"', body)
        self.assertIn("Lương ngày", body)
        self.assertNotIn("Salary Per Hour", body)

    def test_saving_the_work_tab_succeeds_without_job_role(self):
        department = Department.objects.create(department="Screen Dept")
        position = JobPosition.objects.create(
            job_position="Screen Position", department_id=department
        )

        response = self.client.post(
            f"/employee/employee-view-update/{self.target.id}/",
            {
                "form": "work",
                "department_id": department.id,
                "job_position_id": position.id,
                "company_id": self.company.id,
                "basic_salary": 0,
                "salary_hour": 450000,
            },
        )

        self.assertIn(response.status_code, (200, 302))
        info = EmployeeWorkInformation.objects.get(employee_id=self.target)
        self.assertEqual(info.salary_hour, 450000)

    def test_saving_the_bank_tab_succeeds_with_only_three_fields(self):
        response = self.client.post(
            f"/employee/employee-view-update/{self.target.id}/",
            {
                "form": "bank",
                "bank_name": "Techcombank",
                "account_number": "555444333",
                "branch": "Sai Gon",
            },
        )

        self.assertIn(response.status_code, (200, 302))
        self.assertNotIn(
            "Trường này là bắt buộc", response.content.decode()
        )
        stored = EmployeeBankDetails.objects.filter(
            employee_id=self.target
        ).first()
        self.assertIsNotNone(stored, msg="bank details were not saved")
        self.assertEqual(stored.bank_name, "Techcombank")
        self.assertEqual(stored.branch, "Sai Gon")
