from datetime import date
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from attendance.models import Attendance
from base.demo_data.modules.checkin import seed_joydigi_checkin_demo
from base.models import CheckInLocation, Company, OfficeWifi
from employee.models import Employee
from joydigi_auth.models import JoydigiUser


@override_settings(DB_INIT_PASSWORD="demo-secret")
class JoydigiDemoDataTests(TestCase):
    @patch("base.demo_data.run_enterprise_demo_seeder")
    def test_web_loader_requires_db_init_password_and_runs_seeder(self, seeder):
        response = self.client.post(
            reverse("load-demo-database"),
            {"load_data_password": "demo-secret"},
        )

        self.assertEqual(response.status_code, 302)
        seeder.assert_called_once()

    @patch("base.views.initialize_database_condition", return_value=False)
    @patch("base.views.is_checkin_admin", return_value=True)
    @patch("base.demo_data.modules.checkin.seed_joydigi_checkin_demo")
    def test_admin_can_open_and_run_loader_after_initialization(
        self, seeder, _admin_access, _initialization_condition
    ):
        admin_user = JoydigiUser.objects.create_superuser(
            username="demo_loader_admin",
            email="demo-loader-admin@test.joydigi",
            password="test-password",
        )
        admin_user.is_staff = True
        admin_user.is_superuser = True
        admin_user.save(update_fields=["is_staff", "is_superuser"])
        seeder.return_value = {
            "employees": 15,
            "attendance_from": "2026-07-01",
            "attendance_to": "2026-08-20",
        }
        self.client.force_login(admin_user)

        response = self.client.get(reverse("load-demo-database"))
        self.assertEqual(response.status_code, 200, response.get("Location"))
        self.assertContains(response, "Xác nhận tạo dữ liệu JOYDIGI")

        response = self.client.post(
            reverse("load-demo-database"),
            {"load_data_password": "demo-secret"},
        )
        self.assertEqual(response.status_code, 302)
        seeder.assert_called_once()

    @patch("base.views.initialize_database_condition", return_value=False)
    @patch("base.views.is_checkin_admin", return_value=False)
    def test_non_admin_cannot_open_loader_after_initialization(
        self, _admin_access, _initialization_condition
    ):
        regular_user = JoydigiUser.objects.create_user(
            username="regular_demo_loader_user",
            password="test-password",
        )
        self.client.force_login(regular_user)

        response = self.client.get(reverse("load-demo-database"))
        self.assertEqual(response.status_code, 302)

    def test_seeder_creates_15_people_and_two_months_of_checkin_data(self):
        result = seed_joydigi_checkin_demo(today=date(2026, 8, 20))

        company = Company.objects.get(company="JOYDIGI")
        employees = Employee.objects.entire().filter(
            employee_work_info__company_id=company
        )
        attendances = Attendance.objects.entire().filter(
            employee_id__in=employees
        )

        self.assertEqual(result["employees"], 15)
        self.assertEqual(
            employees.count(),
            15,
            (
                Employee.objects.entire().count(),
                list(
                    Employee.objects.entire().values_list(
                        "email", "employee_work_info__company_id__company"
                    )
                ),
            ),
        )
        self.assertEqual(attendances.earliest("attendance_date").attendance_date, date(2026, 7, 1))
        self.assertEqual(attendances.latest("attendance_date").attendance_date, date(2026, 8, 20))
        self.assertTrue(JoydigiUser.objects.get(username="admin").check_password("demo-secret"))
        # DEMO_EMPLOYEE_PASSWORD is unset in this class: demo employees get an
        # unusable password instead of the old hardcoded "123456".
        demo_employee = JoydigiUser.objects.get(username="lan.phuong")
        self.assertFalse(demo_employee.has_usable_password())
        self.assertFalse(demo_employee.check_password("123456"))
        self.assertEqual(CheckInLocation.objects.filter(company_id=company).count(), 1)
        self.assertEqual(OfficeWifi.objects.filter(company_id=company).count(), 1)

        seed_joydigi_checkin_demo(today=date(2026, 8, 20))
        self.assertEqual(
            Employee.objects.entire().filter(
                employee_work_info__company_id=company
            ).count(),
            15,
        )
        self.assertEqual(
            Attendance.objects.entire().filter(employee_id__in=employees).count(),
            attendances.count(),
        )


@override_settings(DB_INIT_PASSWORD="demo-secret", DEMO_EMPLOYEE_PASSWORD="demo-employee-secret")
class DemoEmployeePasswordFromEnvironmentTests(TestCase):
    def test_demo_employees_use_the_configured_password(self):
        seed_joydigi_checkin_demo(today=date(2026, 8, 20))
        demo_employee = JoydigiUser.objects.get(username="lan.phuong")
        self.assertTrue(demo_employee.check_password("demo-employee-secret"))
        self.assertFalse(demo_employee.check_password("123456"))


class DbInitPasswordTests(TestCase):
    """DB_INIT_PASSWORD has no default; an unset value unlocks nothing."""

    def test_unset_password_never_matches(self):
        from base.views import db_init_password_matches

        with override_settings(DB_INIT_PASSWORD=""):
            self.assertFalse(db_init_password_matches(""))
            self.assertFalse(db_init_password_matches(None))
            self.assertFalse(db_init_password_matches("anything"))

    def test_configured_password_matches_only_itself(self):
        from base.views import db_init_password_matches

        with override_settings(DB_INIT_PASSWORD="s3cret-value"):
            self.assertTrue(db_init_password_matches("s3cret-value"))
            self.assertFalse(db_init_password_matches("s3cret-valuE"))
            self.assertFalse(db_init_password_matches(""))
            self.assertFalse(db_init_password_matches(None))

    @patch("base.views.initialize_database_condition", return_value=True)
    def test_demo_loader_refuses_an_empty_password_when_unset(self, _initializing):
        with override_settings(DB_INIT_PASSWORD=""), patch("base.demo_data.run_enterprise_demo_seeder") as seeder:
            self.client.post(reverse("load-demo-database"), {"load_data_password": ""})
        seeder.assert_not_called()

    def test_settings_default_is_not_the_old_public_value(self):
        from django.conf import settings

        from joydigi.settings.security import INSECURE_DB_INIT_PASSWORDS

        self.assertNotIn(settings.DB_INIT_PASSWORD, INSECURE_DB_INIT_PASSWORDS)
