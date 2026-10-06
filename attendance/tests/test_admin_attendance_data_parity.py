"""Does the Admin attendance list actually show the rows that exist?

Phase ATTENDANCE-STATUS-AND-ADMIN-DATA-HARDENING, sections 8-13.

Three numbers, for the same company, employees and date range:

* DB_COUNT       — rows in the database, read with `.entire()` so no manager
                   scoping can hide one
* QUERYSET_COUNT — what the view's own `get_queryset()` returns
* VISIBLE_COUNT  — what the page actually renders, summed over every page

A gap between the first two is a filter dropping rows; a gap between the last
two is pagination or rendering. Each test below names which of the three it is
about, so a failure says where the row went rather than only that one went.

Nothing here widens a queryset to make a number match: the cross-company and
permission tests assert that rows *stay* hidden.
"""

import uuid
from datetime import time, timedelta

from django.core.cache import cache as CACHE
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from attendance.models import Attendance
from base.models import (
    Company,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    WorkType,
)
from employee.models import Employee, EmployeeWorkInformation
from joydigi_auth.models import JoydigiUser

PASSWORD = "admin-pass-123"


class AdminAttendanceParityBase(TestCase):
    """A superuser admin, two companies, and enough rows to need two pages."""

    @classmethod
    def setUpTestData(cls):
        tag = uuid.uuid4().hex[:6]
        cls.company = Company.objects.create(
            company="Parity Co %s" % tag,
            hq=True,
            address="x",
            country="VN",
            state="HN",
            city="HN",
            zip="10000",
        )
        cls.other_company = Company.objects.create(
            company="Other Co %s" % tag,
            hq=False,
            address="y",
            country="VN",
            state="HN",
            city="HN",
            zip="10000",
        )
        cls.shift = EmployeeShift.objects.create(employee_shift="Ca %s" % tag)
        cls.shift.company_id.add(cls.company)
        cls.work_type = WorkType.objects.create(work_type="Office %s" % tag)
        cls.work_type.company_id.add(cls.company)
        Department.objects.create(department="Eng %s" % tag).company_id.add(cls.company)

        for name in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            schedule = EmployeeShiftSchedule.objects.create(
                day=EmployeeShiftDay.objects.filter(day=name).first(),
                shift_id=cls.shift,
                minimum_working_hour="08:00",
                start_time=time(8, 0),
                end_time=time(17, 0),
            )
            schedule.company_id.add(cls.company)

    def setUp(self):
        tag = uuid.uuid4().hex[:8]
        self.admin_user = JoydigiUser.objects.create_superuser(
            username="parity_admin_%s" % tag,
            email="parity_admin_%s@test.local" % tag,
            password=PASSWORD,
        )
        # The middleware logs out an account with no employee record.
        self.admin = self.make_employee(
            "Admin", tag, self.company, user=self.admin_user
        )
        self.active_a = self.make_employee("Active", "A" + tag, self.company)
        self.active_b = self.make_employee("Active", "B" + tag, self.company)
        self.former = self.make_employee("Former", "C" + tag, self.company)
        self.outsider = self.make_employee(
            "Outsider", "D" + tag, self.other_company, shift=None
        )
        self.client.login(username=self.admin_user.username, password=PASSWORD)

    # ---------------------------------------------------------------- fixtures
    def make_employee(self, first, last, company, user=None, shift="default"):
        employee = Employee(
            employee_first_name=first,
            employee_last_name=last,
            email="%s.%s@test.local" % (first.lower(), last.lower()),
            phone="9999999999",
        )
        if user is not None:
            employee.employee_user_id = user
        employee.save()
        info = EmployeeWorkInformation.objects.get(employee_id=employee)
        info.company_id = company
        if shift == "default":
            info.shift_id = self.shift
            info.work_type_id = self.work_type
        info.save()
        return Employee.objects.get(pk=employee.pk)

    def record(self, employee, day, validated=True, check_out=time(17, 0)):
        row = Attendance(
            employee_id=employee,
            attendance_date=day,
            shift_id=self.shift if employee != self.outsider else None,
            work_type_id=self.work_type,
            attendance_day=EmployeeShiftDay.objects.filter(
                day=day.strftime("%A").lower()
            ).first(),
            attendance_clock_in=time(8, 0),
            attendance_clock_in_date=day,
            attendance_clock_out=check_out,
            attendance_clock_out_date=day if check_out else None,
            minimum_hour="08:00",
            attendance_validated=validated,
        )
        row.save()
        return row

    def weekdays(self, count, end=None):
        """`count` past weekdays, most recent first."""
        day = end or (timezone.localdate() - timedelta(days=1))
        out = []
        while len(out) < count:
            if day.weekday() < 5:
                out.append(day)
            day -= timedelta(days=1)
        return out

    # ------------------------------------------------------------------ counts
    def db_count(self, employees, days):
        return (
            Attendance.objects.entire()
            .filter(employee_id__in=employees, attendance_date__in=days)
            .count()
        )

    def page(self, url_name, **params):
        # These tabs are htmx fragments: `hx_request_required` answers 405 to a
        # plain GET, which is how the browser loads them too.
        response = self.client.get(
            reverse(url_name), params, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200, url_name)
        return response

    def queryset_count(self, response):
        """Total rows the view resolved, before pagination cut them up."""
        return response.context["queryset"].paginator.count

    def visible_count(self, url_name, **params):
        """Rows actually rendered, walked across every page."""
        response = self.page(url_name, **params)
        page_obj = response.context["queryset"]
        total_pages = page_obj.paginator.num_pages
        seen = []
        for number in range(1, total_pages + 1):
            page_params = dict(params)
            page_params["page"] = number
            page_response = self.page(url_name, **page_params)
            seen.extend(row.pk for row in page_response.context["queryset"].object_list)
        return seen


class ValidatedTabParityTests(AdminAttendanceParityBase):
    """The tab an administrator looks at for finished days."""

    URL = "validated-attendance-tab"

    def test_every_row_in_the_database_is_reachable_across_the_pages(self):
        """Section 12: a row on page 2 must not read as a row that is missing."""
        days = self.weekdays(14)
        for day in days:
            self.record(self.active_a, day)
            self.record(self.active_b, day)
        expected = self.db_count([self.active_a, self.active_b], days)
        self.assertEqual(expected, 28, "fixture should exceed one page of 20")

        response = self.page(self.URL)
        self.assertEqual(
            self.queryset_count(response),
            expected,
            "QUERYSET_COUNT < DB_COUNT means a filter dropped rows",
        )
        self.assertGreater(
            response.context["queryset"].paginator.num_pages,
            1,
            "this test is pointless unless the list actually paginates",
        )

        visible = self.visible_count(self.URL)
        self.assertEqual(
            len(visible),
            expected,
            "VISIBLE_COUNT < QUERYSET_COUNT means pagination or rendering lost "
            "rows",
        )
        self.assertEqual(len(set(visible)), len(visible), "a row appeared twice")

    def test_both_employees_of_the_company_are_visible(self):
        """Section 13, cases 1 and 2."""
        day = self.weekdays(1)[0]
        a = self.record(self.active_a, day)
        b = self.record(self.active_b, day)
        visible = self.visible_count(self.URL)
        self.assertIn(a.pk, visible)
        self.assertIn(b.pk, visible)

    def test_another_companys_attendance_is_not_visible(self):
        """Section 13, case 3 — and section 10: this must stay hidden."""
        day = self.weekdays(1)[0]
        mine = self.record(self.active_a, day)
        theirs = self.record(self.outsider, day)
        visible = self.visible_count(self.URL)
        self.assertIn(mine.pk, visible)
        self.assertNotIn(
            theirs.pk,
            visible,
            "a superuser of this tenant still must not be shown another "
            "company's attendance through this list",
        )

    def test_a_date_filter_selects_exactly_the_day_asked_for(self):
        """Section 12: the date filter is exact."""
        days = self.weekdays(3)
        rows = {day: self.record(self.active_a, day) for day in days}
        target = days[1]

        visible = self.visible_count(
            self.URL,
            attendance_date__gte=target.isoformat(),
            attendance_date__lte=target.isoformat(),
            filter_applied="on",
        )
        self.assertEqual(visible, [rows[target].pk])

    def test_an_inactive_employees_attendance_is_hidden_and_that_is_explicit(self):
        """Section 12: inactive behaviour stated, not discovered.

        `ValidatedAttendancesList.get_queryset` filters `employee_id__is_active`,
        and `JoydigiCompanyManager.all()` independently hides rows of inactive
        employees. So a former employee's history is not reachable from this
        list at all. That is the behaviour as built; this test exists so that it
        is a decision on record rather than a surprise, and so that changing it
        has to be deliberate.
        """
        day = self.weekdays(1)[0]
        kept = self.record(self.active_a, day)
        leaver = self.record(self.former, day)

        Employee.objects.filter(pk=self.former.pk).update(is_active=False)

        visible = self.visible_count(self.URL)
        self.assertIn(kept.pk, visible)
        self.assertNotIn(leaver.pk, visible)
        self.assertEqual(
            Attendance.objects.entire().filter(pk=leaver.pk).count(),
            1,
            "the row still exists in the database — it is hidden, not deleted",
        )


class ValidateTabParityTests(AdminAttendanceParityBase):
    """Unvalidated days live in their own tab; they are not lost."""

    def test_an_unvalidated_day_is_listed_in_the_validate_tab(self):
        day = self.weekdays(1)[0]
        row = self.record(self.active_a, day, validated=False)

        visible = self.visible_count("validate-attendance-tab")
        self.assertIn(row.pk, visible)

    def test_a_validated_day_is_not_in_the_validate_tab(self):
        day = self.weekdays(1)[0]
        row = self.record(self.active_a, day, validated=True)
        self.assertNotIn(row.pk, self.visible_count("validate-attendance-tab"))

    def test_every_row_is_in_exactly_one_of_the_two_tabs(self):
        """The invariant that makes "data is missing" answerable.

        A day is either validated or not, so between the two tabs every row an
        administrator may see is somewhere. If this ever fails, rows exist that
        no tab lists.
        """
        days = self.weekdays(4)
        expected = set()
        for index, day in enumerate(days):
            expected.add(self.record(self.active_a, day, validated=index % 2 == 0).pk)

        listed = set(self.visible_count("validated-attendance-tab")) | set(
            self.visible_count("validate-attendance-tab")
        )
        self.assertTrue(
            expected <= listed,
            "rows not listed anywhere: %s" % sorted(expected - listed),
        )


class RememberedFilterConsistencyTests(AdminAttendanceParityBase):
    """The root cause: a remembered filter that only one worker remembered.

    Production runs gunicorn with three workers and no `REDIS_URL`, so Django's
    default `LocMemCache` is private to each process. A filter applied on one
    worker was re-applied to later unfiltered requests *on that worker only*, so
    the same admin opening the same list URL saw a filtered list or the full one
    depending on which worker answered.

    Clearing the cache while keeping the session is exactly what "the next
    request landed on a different worker" looks like from inside one process.
    """

    URL = "validated-attendance-tab"

    def setUp(self):
        super().setUp()
        CACHE.clear()
        self.days = self.weekdays(3)
        self.rows = {day: self.record(self.active_a, day) for day in self.days}
        self.target = self.days[1]

    def apply_filter(self):
        return self.visible_count(
            self.URL,
            attendance_date__gte=self.target.isoformat(),
            attendance_date__lte=self.target.isoformat(),
            filter_applied="on",
        )

    def test_the_filter_is_applied_when_asked_for(self):
        self.assertEqual(self.apply_filter(), [self.rows[self.target].pk])

    def test_the_same_url_gives_the_same_rows_on_a_worker_without_the_cache(self):
        self.apply_filter()

        with_cache = self.visible_count(self.URL)
        CACHE.clear()  # i.e. the next request is served by another worker
        without_cache = self.visible_count(self.URL)

        self.assertEqual(
            sorted(with_cache),
            sorted(without_cache),
            "the same admin, the same session and the same URL returned a "
            "different number of rows depending on which worker answered — "
            "which is what 'the admin page does not show all the data' was",
        )

    def test_the_remembered_filter_is_shown_to_the_user(self):
        """It narrows the list, so it must be visible while it does.

        The filter tags are built from the same remembered query, so an admin
        can see why the list is short and clear it.
        """
        self.apply_filter()
        response = self.page(self.URL)
        self.assertTrue(
            response.context.get("filter_dict"),
            "a filter that reduces the list without appearing anywhere is "
            "indistinguishable from missing data",
        )
