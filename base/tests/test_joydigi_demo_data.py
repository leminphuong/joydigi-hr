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
