"""Phase ADMIN-P0-PERMISSION-HARDENING — the nine P0 holes, and the flows beside them.

Every test here is paired: the negative half proves the hole is shut, and a
positive half beside it proves the operator who was always allowed to do the
thing still can. That pairing is the point of the phase — these were guard bugs,
not design bugs, so a fix that also narrowed a legitimate flow would be a
regression dressed up as a security improvement.

Each negative test would have passed its own assertion backwards before the fix:
C-1/C-2 created a superuser for an unauthenticated POST, C-3 flipped an
accessibility row for one, C-8 created an Attendance row for a colleague, C-9 and
C-12 let "a manager of somebody" decide a stranger's request, C-10 let any
employee rewrite a colleague's shift, C-11 made every `is_reportingmanger(...) or
...` test pass for an employee with no work information, C-13 showed a
non-superuser admin every company in the database, and C-14 let any employee
rewrite the working calendar.
"""

from datetime import date, timedelta

from django.contrib.auth.models import Group, Permission
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accessibility.models import DefaultAccessibility
from attendance.models import Attendance
from base.checkin_portal import _visible_employees
from base.models import (
    CompanyGroupAssignment,
    CompanyLeaves,
    EmployeeShift,
    Holidays,
    ShiftRequest,
    WorkType,
    WorkTypeRequest,
)
from base.roles import ADMIN_ROLE, LEADER_ROLE
from base.views import is_reportingmanger
from employee.models import Employee, EmployeeWorkInformation
from joydigi.testkit import make_company, make_employee, make_user
from joydigi_auth.models import JoydigiUser


def grant(user, *codenames):
    """Give a user model permissions and drop the cached permission set."""
    for codename in codenames:
        user.user_permissions.add(Permission.objects.get(codename=codename))
    return JoydigiUser.objects.get(pk=user.pk)


def set_manager(employee, manager):
    employee.employee_work_info.reporting_manager_id = manager
    employee.employee_work_info.save(update_fields=["reporting_manager_id"])


class P0TestCase(TestCase):
    """Two companies, and in company A a leader, their report, and a stranger."""

    def setUp(self):
        self.company = make_company("P0 Co")
        self.other_company = make_company("P0 Other Co")

        self.leader_user = make_user("p0_leader")
        self.leader = make_employee(
            company=self.company, email="p0-leader@test.joydigi", user=self.leader_user
        )
        self.worker_user = make_user("p0_worker")
        self.worker = make_employee(
            company=self.company, email="p0-worker@test.joydigi", user=self.worker_user
        )
        set_manager(self.worker, self.leader)

        # An ordinary employee of the same company who manages nobody.
        self.stranger_user = make_user("p0_stranger")
        self.stranger = make_employee(
            company=self.company,
            email="p0-stranger@test.joydigi",
            user=self.stranger_user,
        )

        # A leader in the OTHER company, with a report of their own, so they are
        # a reporting manager somewhere and pass every global manager test.
        self.outsider_user = make_user("p0_outsider")
        self.outsider = make_employee(
            company=self.other_company,
            email="p0-outsider@test.joydigi",
            user=self.outsider_user,
        )
        self.outsider_report = make_employee(
            company=self.other_company, email="p0-outsider-report@test.joydigi"
        )
        set_manager(self.outsider_report, self.outsider)

        # A leader in the SAME company who manages somebody else. This is the
        # actor that makes an object-level test meaningful: a global
        # "is this user a manager?" decorator lets them straight through and the
        # row is inside their company, so only a per-row check can refuse them.
        self.other_leader_user = make_user("p0_other_leader")
        self.other_leader = make_employee(
            company=self.company,
            email="p0-other-leader@test.joydigi",
            user=self.other_leader_user,
        )
        self.other_leader_report = make_employee(
            company=self.company, email="p0-other-leader-report@test.joydigi"
        )
        set_manager(self.other_leader_report, self.other_leader)

    def select_company(self, company):
        """Put a company in the session, as the switcher middleware would."""
        session = self.client.session
        session["selected_company"] = company.id
        session.save()


# ======================================================================
# C-1 / C-2 — the initialisation wizard
# ======================================================================


class InitializationEndpointTests(P0TestCase):
    USER_PAYLOAD = {
        "username": "smuggled_superuser",
        "password": "pw-12345",
        "confirm_password": "pw-12345",
        "firstname": "Smuggled",
        "lastname": "Superuser",
        "badge_id": "SMUG1",
        "email": "smuggled@test.joydigi",
        "phone": "0900000000",
    }

    @override_settings(DEBUG=False)
    def test_production_refuses_the_user_step_and_creates_nobody(self):
        """C-1. The entry page was guarded; this URL was not."""
        before = JoydigiUser.objects.count()

        response = self.client.post(
            reverse("initialize-database-user"),
            self.USER_PAYLOAD,
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(JoydigiUser.objects.count(), before)
        self.assertFalse(
            JoydigiUser.objects.filter(username="smuggled_superuser").exists()
        )

    @override_settings(DEBUG=False)
    def test_production_refuses_the_company_step_and_creates_nothing(self):
        """C-2."""
        from base.models import Company

        before = Company.objects.count()

        response = self.client.post(
            reverse("initialize-database-company"),
            {"company": "Smuggled Co", "hq": True},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Company.objects.count(), before)

    @override_settings(DEBUG=True)
    def test_the_user_step_is_refused_once_the_database_is_initialised(self):
        """Even in DEBUG, step one is only for a database with no usable superuser.

        The condition is about a superuser that HAS an employee, not about users
        in general — so this test has to create one before the bootstrap counts
        as finished. (Discovering that is why the first version of this test
        failed: the fixture had users but no superuser, so the wizard still
        considered the database uninitialised, which is correct.)
        """
        installed = make_user("p0_installed_superuser", is_superuser=True)
        make_employee(
            company=self.company,
            email="p0-installed-superuser@test.joydigi",
            user=installed,
        )

        response = self.client.post(
            reverse("initialize-database-user"),
            self.USER_PAYLOAD,
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            JoydigiUser.objects.filter(username="smuggled_superuser").exists()
        )

    @override_settings(DEBUG=True)
    def test_a_later_step_is_refused_for_anonymous_and_allowed_for_a_superuser(self):
        """C-2, both directions.

        The later steps cannot re-check `initialize_database_condition()` — step
        one creates a superuser WITH an employee, which makes it false — so the
        gate is "DEBUG and an authenticated superuser", which is the state step
        one leaves behind. Anonymous is refused; the operator mid-wizard is not.
        """
        url = reverse("initialize-database-company")

        anonymous = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertEqual(anonymous.status_code, 404)

        superuser = make_user("p0_init_superuser", is_superuser=True)
        # An account with no Employee row is logged straight back out by the
        # session middleware, so it would 404 here for the wrong reason.
        make_employee(
            company=self.company,
            email="p0-init-superuser@test.joydigi",
            user=superuser,
        )
        self.client.force_login(superuser)
        allowed = self.client.get(url, HTTP_HX_REQUEST="true")
        self.assertNotEqual(
            allowed.status_code,
            404,
            msg="the dev wizard must still open for the superuser running it",
        )


class InitializationOnAnEmptyDatabaseTests(TestCase):
    """No fixtures at all, so the bootstrap condition is genuinely true."""

    @override_settings(DEBUG=True)
    def test_the_dev_flow_still_opens_when_there_is_no_superuser_yet(self):
        self.assertFalse(JoydigiUser.objects.exists())

        response = self.client.get(
            reverse("initialize-database-user"), HTTP_HX_REQUEST="true"
        )

        self.assertNotEqual(
            response.status_code,
            404,
            msg="C-1 must not break the only situation this wizard exists for",
        )


# ======================================================================
# C-3 — the per-employee accessibility toggle
# ======================================================================


class ProfileEditAccessTests(P0TestCase):
    def setUp(self):
        super().setUp()
        self.accessibility = DefaultAccessibility.objects.create(
            feature="profile_edit", filter={}
        )
        self.url = reverse("profile-edit-access", kwargs={"emp_id": self.worker.pk})

    def granted(self):
        return list(
            self.accessibility.employees.values_list("pk", flat=True)
        )

    def test_an_anonymous_caller_changes_nothing(self):
        """C-3. This view used to carry no decorators at all."""
        response = self.client.post(self.url + "?feature=profile_edit")

        self.assertNotEqual(response.status_code, 200)
        self.assertEqual(self.granted(), [])

    def test_an_ordinary_employee_changes_nothing(self):
        self.client.force_login(self.stranger_user)

        self.client.post(self.url + "?feature=profile_edit")

        self.assertEqual(self.granted(), [])

    def test_a_get_cannot_mutate(self):
        admin_user = grant(make_user("p0_access_admin"), "change_permission")
        make_employee(
            company=self.company, email="p0-access-admin@test.joydigi", user=admin_user
        )
        self.client.force_login(admin_user)
        self.select_company(self.company)

        response = self.client.get(self.url + "?feature=profile_edit")

        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.granted(), [])

    def test_an_authorized_admin_in_the_same_company_can_still_toggle(self):
        admin_user = grant(make_user("p0_access_admin2"), "change_permission")
        make_employee(
            company=self.company, email="p0-access-admin2@test.joydigi", user=admin_user
        )
        self.client.force_login(admin_user)
        self.select_company(self.company)

        self.client.post(self.url + "?feature=profile_edit")
        self.assertEqual(self.granted(), [self.worker.pk])

        # ...and off again, so the toggle is still a toggle.
        self.client.post(self.url + "?feature=profile_edit")
        self.assertEqual(self.granted(), [])

    def test_another_companys_employee_is_out_of_reach(self):
        admin_user = grant(make_user("p0_access_admin3"), "change_permission")
        make_employee(
            company=self.company, email="p0-access-admin3@test.joydigi", user=admin_user
        )
        self.client.force_login(admin_user)
        self.select_company(self.company)

        response = self.client.post(
            reverse("profile-edit-access", kwargs={"emp_id": self.outsider.pk})
            + "?feature=profile_edit"
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.granted(), [])

    def test_a_feature_outside_the_allowlist_is_refused(self):
        admin_user = grant(make_user("p0_access_admin4"), "change_permission")
        make_employee(
            company=self.company, email="p0-access-admin4@test.joydigi", user=admin_user
        )
        DefaultAccessibility.objects.create(feature="invented_feature", filter={})
        self.client.force_login(admin_user)
        self.select_company(self.company)

        response = self.client.post(self.url + "?feature=invented_feature")

        self.assertEqual(response.status_code, 404)


# ======================================================================
# C-11 — is_reportingmanger must answer a boolean
# ======================================================================


class ReportingManagerHelperTests(P0TestCase):
    def test_a_missing_work_information_row_is_False_and_not_a_response(self):
        """C-11. This used to return HttpResponse, which is always truthy."""
        orphan = make_employee(company=self.company, email="p0-orphan@test.joydigi")
        EmployeeWorkInformation.objects.filter(employee_id=orphan).delete()
        orphan = Employee.objects.get(pk=orphan.pk)
        request = RequestFactory().get("/")
        request.user = self.stranger_user
        holder = ShiftRequest(employee_id=orphan)

        result = is_reportingmanger(request, holder)

        self.assertIs(result, False)

    def test_the_real_manager_is_True_and_everybody_else_is_False(self):
        request = RequestFactory().get("/")
        holder = ShiftRequest(employee_id=self.worker)

        request.user = self.leader_user
        self.assertIs(is_reportingmanger(request, holder), True)

        request.user = self.stranger_user
        self.assertIs(is_reportingmanger(request, holder), False)


# ======================================================================
# C-12 — work-type approve / reject / bulk
# ======================================================================


class WorkTypeRequestDecisionTests(P0TestCase):
    def setUp(self):
        super().setUp()
        self.work_type = WorkType.objects.create(work_type="Tại nhà")
        self.request_row = WorkTypeRequest.objects.create(
            employee_id=self.worker,
            work_type_id=self.work_type,
            requested_date=date.today() + timedelta(days=1),
            requested_till=date.today() + timedelta(days=2),
            description="Xin làm tại nhà.",
        )

    def approve(self, row=None):
        row = row or self.request_row
        return self.client.post(
            reverse("work-type-request-approve", kwargs={"id": row.pk})
        )

    def test_an_ordinary_employee_cannot_approve(self):
        """C-12: these three views had no manager decorator at all."""
        self.client.force_login(self.stranger_user)
        self.select_company(self.company)

        self.approve()

        self.request_row.refresh_from_db()
        self.assertFalse(self.request_row.approved)

    def test_the_reporting_manager_can_still_approve(self):
        self.client.force_login(self.leader_user)
        self.select_company(self.company)

        self.approve()

        self.request_row.refresh_from_db()
        self.assertTrue(
            self.request_row.approved,
            msg="the manager flow this screen exists for must be untouched",
        )

    def test_a_manager_of_another_company_cannot_approve(self):
        self.client.force_login(self.outsider_user)
        self.select_company(self.other_company)

        self.approve()

        self.request_row.refresh_from_db()
        self.assertFalse(self.request_row.approved)

    def test_nobody_approves_their_own_request(self):
        own = WorkTypeRequest.objects.create(
            employee_id=self.leader,
            work_type_id=self.work_type,
            requested_date=date.today() + timedelta(days=3),
            requested_till=date.today() + timedelta(days=4),
            description="Đơn của chính tôi.",
        )
        # The owner even holds the approval permission, which used to be enough.
        self.leader_user = grant(self.leader_user, "change_worktyperequest")
        self.client.force_login(self.leader_user)
        self.select_company(self.company)

        self.approve(own)

        own.refresh_from_db()
        self.assertFalse(own.approved)

    def test_the_owner_can_still_withdraw_their_own_pending_request(self):
        """The flow a manager decorator would have broken.

        `work_type_request_cancel` is also how an ordinary employee withdraws
        their own request. `@manager_can_enter` admits only a permission holder
        or a reporting manager, so putting it on this view — as its approve twin
        correctly has — would have locked the owner out of their own withdrawal.
        This test is why that decorator is deliberately absent there.
        """
        self.client.force_login(self.worker_user)
        self.select_company(self.company)

        self.client.post(
            reverse("work-type-request-cancel", kwargs={"id": self.request_row.pk})
        )

        self.request_row.refresh_from_db()
        self.assertTrue(
            self.request_row.canceled,
            msg="an employee must still be able to withdraw their own request",
        )

    def test_bulk_approve_touches_only_the_rows_the_actor_may_decide(self):
        """C-12: mixed ids, and an id that does not exist at all."""
        import json

        other_company_row = WorkTypeRequest.objects.create(
            employee_id=self.outsider_report,
            work_type_id=self.work_type,
            requested_date=date.today() + timedelta(days=1),
            requested_till=date.today() + timedelta(days=2),
            description="Đơn công ty khác.",
        )
        self.client.force_login(self.leader_user)
        self.select_company(self.company)

        response = self.client.post(
            reverse("work-type-request-bulk-approve"),
            {"ids": json.dumps([self.request_row.pk, other_company_row.pk, 10**6])},
        )

        self.assertEqual(
            response.status_code,
            200,
            msg="a crafted id used to raise DoesNotExist and 500 the whole action",
        )
        self.request_row.refresh_from_db()
        other_company_row.refresh_from_db()
        self.assertTrue(self.request_row.approved)
        self.assertFalse(other_company_row.approved)


# ======================================================================
# C-10 — shift reallocation
# ======================================================================


class ShiftReallocationTests(P0TestCase):
    def setUp(self):
        super().setUp()
        self.shift = EmployeeShift.objects.create(employee_shift="Ca chiều")
        self.shift.company_id.add(self.company)
        self.previous_shift = EmployeeShift.objects.create(employee_shift="Ca sáng")
        self.previous_shift.company_id.add(self.company)
        self.worker.employee_work_info.shift_id = self.previous_shift
        self.worker.employee_work_info.save(update_fields=["shift_id"])
        # The swap is offered TO the stranger: they are the legitimate decider.
        self.request_row = ShiftRequest.objects.create(
            employee_id=self.worker,
            shift_id=self.shift,
            previous_shift_id=self.previous_shift,
            requested_date=date.today() + timedelta(days=1),
            requested_till=date.today() + timedelta(days=2),
            reallocate_to=self.stranger,
            description="Đổi ca.",
        )

    def test_an_uninvolved_employee_cannot_accept_a_swap(self):
        """C-10: these two views carried only @login_required."""
        bystander_user = make_user("p0_bystander")
        make_employee(
            company=self.company,
            email="p0-bystander@test.joydigi",
            user=bystander_user,
        )
        self.client.force_login(bystander_user)
        self.select_company(self.company)

        self.client.post(
            reverse(
                "shift-allocation-request-approve", kwargs={"id": self.request_row.pk}
            )
        )

        self.request_row.refresh_from_db()
        self.assertFalse(self.request_row.reallocate_approved)

    def test_an_uninvolved_employee_cannot_rewrite_a_colleagues_shift(self):
        """The cancel path writes employee_work_info.shift_id, so it matters more."""
        bystander_user = make_user("p0_bystander2")
        make_employee(
            company=self.company,
            email="p0-bystander2@test.joydigi",
            user=bystander_user,
        )
        self.client.force_login(bystander_user)
        self.select_company(self.company)

        self.client.post(
            reverse(
                "shift-allocation-request-cancel", kwargs={"id": self.request_row.pk}
            )
        )

        self.request_row.refresh_from_db()
        self.assertFalse(self.request_row.reallocate_canceled)

    def test_the_employee_the_swap_is_offered_to_can_still_accept(self):
        """The one legitimate actor who holds no permission and manages nobody."""
        self.client.force_login(self.stranger_user)
        self.select_company(self.company)

        self.client.post(
            reverse(
                "shift-allocation-request-approve", kwargs={"id": self.request_row.pk}
            )
        )

        self.request_row.refresh_from_db()
        self.assertTrue(
            self.request_row.reallocate_approved,
            msg="the swap-acceptance flow must survive the fix",
        )

    def test_a_permission_holder_in_another_company_cannot_decide(self):
        self.outsider_user = grant(self.outsider_user, "change_shiftrequest")
        self.client.force_login(self.outsider_user)
        self.select_company(self.other_company)

        self.client.post(
            reverse(
                "shift-allocation-request-approve", kwargs={"id": self.request_row.pk}
            )
        )

        self.request_row.refresh_from_db()
        self.assertFalse(self.request_row.reallocate_approved)


# ======================================================================
# C-13 — what "All my companies" means
# ======================================================================


class VisibleEmployeeScopeTests(P0TestCase):
    def build_request(self, user, selected="all", all_my=None):
        request = RequestFactory().get("/duyet-don/")
        request.user = user
        request.session = {"selected_company": selected}
        request.all_my_company_ids = all_my
        request.allowed_company_ids = all_my
        return request

    def make_admin(self, username):
        """An ADMIN_ROLE holder who is not a superuser.

        The group is attached directly as well as through
        `CompanyGroupAssignment`, so `user_has_role` resolves it whether or not a
        company is in the thread-local context.
        """
        admin_group, _ = Group.objects.get_or_create(name=ADMIN_ROLE)
        user = make_user(username)
        make_employee(
            company=self.company, email=f"{username}@test.joydigi", user=user
        )
        CompanyGroupAssignment.objects.create(
            user=user, company=self.company, group=admin_group
        )
        user.groups.add(admin_group)
        return JoydigiUser.objects.get(pk=user.pk)

    def test_an_admin_on_all_companies_sees_only_their_assignments(self):
        """C-13. A checkin admin used to skip the company filter entirely."""
        admin_user = self.make_admin("p0_scoped_admin")

        visible = _visible_employees(
            self.build_request(admin_user, all_my=[self.company.id])
        )
        companies = set(
            visible.values_list("employee_work_info__company_id_id", flat=True)
        )

        self.assertEqual(companies, {self.company.id})
        self.assertNotIn(self.outsider.pk, set(visible.values_list("pk", flat=True)))

    def test_a_superuser_on_all_companies_is_still_tenant_wide(self):
        superuser = make_user("p0_scope_superuser", is_superuser=True)
        make_employee(
            company=self.company,
            email="p0-scope-superuser@test.joydigi",
            user=superuser,
        )

        visible = _visible_employees(self.build_request(superuser, all_my=None))
        ids = set(visible.values_list("pk", flat=True))

        self.assertIn(self.worker.pk, ids)
        self.assertIn(
            self.outsider.pk, ids, msg="a superuser's reach must not be narrowed"
        )

    def test_an_admin_with_two_assignments_sees_both_and_no_more(self):
        admin_user = self.make_admin("p0_two_company_admin")
        third_company = make_company("P0 Third Co")
        third_employee = make_employee(
            company=third_company, email="p0-third@test.joydigi"
        )

        visible = _visible_employees(
            self.build_request(
                admin_user, all_my=[self.company.id, self.other_company.id]
            )
        )
        ids = set(visible.values_list("pk", flat=True))

        self.assertIn(self.worker.pk, ids)
        self.assertIn(self.outsider.pk, ids)
        self.assertNotIn(third_employee.pk, ids)

    def test_a_specific_company_selection_is_unchanged(self):
        admin_user = self.make_admin("p0_specific_admin")

        visible = _visible_employees(
            self.build_request(admin_user, selected=self.company.id, all_my=None)
        )
        companies = set(
            visible.values_list("employee_work_info__company_id_id", flat=True)
        )

        self.assertEqual(companies, {self.company.id})


# ======================================================================
# C-14 — the working calendar
# ======================================================================


class WorkingCalendarPermissionTests(P0TestCase):
    def holiday_payload(self):
        """A payload `HolidayForm` accepts.

        `company_id` matters: without it the form fails validation for everybody,
        and the negative test below would pass whether or not the endpoint is
        guarded — which is exactly how the first version of it was vacuous.
        """
        when = date.today() + timedelta(days=5)
        return {
            "name": "Ngày nghỉ kiểm tra",
            "start_date": when,
            "end_date": when,
            "company_id": self.company.id,
        }

    def test_an_employee_cannot_create_a_public_holiday(self):
        """C-14. The list page was gated; this form endpoint was not."""
        self.client.force_login(self.stranger_user)
        self.select_company(self.company)
        before = Holidays.objects.count()

        self.client.post(
            reverse("holiday-creation"), self.holiday_payload(), HTTP_HX_REQUEST="true"
        )

        self.assertEqual(Holidays.objects.count(), before)

    def test_an_operator_with_the_permission_can_still_create_one(self):
        admin_user = grant(make_user("p0_holiday_admin"), "add_holidays")
        make_employee(
            company=self.company, email="p0-holiday-admin@test.joydigi", user=admin_user
        )
        self.client.force_login(admin_user)
        self.select_company(self.company)
        before = Holidays.objects.count()

        self.client.post(
            reverse("holiday-creation"), self.holiday_payload(), HTTP_HX_REQUEST="true"
        )

        self.assertEqual(
            Holidays.objects.count(),
            before + 1,
            msg="the operator this screen is for must still be able to use it",
        )

    def test_an_employee_cannot_declare_a_weekly_off_day(self):
        self.client.force_login(self.stranger_user)
        self.select_company(self.company)
        before = CompanyLeaves.objects.count()

        self.client.post(
            reverse("company-leave-creation"),
            {"based_on_week": "", "based_on_week_day": "2"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(CompanyLeaves.objects.count(), before)

    def test_an_operator_with_the_permission_can_still_open_the_weekly_off_form(self):
        admin_user = grant(make_user("p0_weekoff_admin"), "add_companyleaves")
        make_employee(
            company=self.company, email="p0-weekoff-admin@test.joydigi", user=admin_user
        )
        self.client.force_login(admin_user)
        self.select_company(self.company)

        response = self.client.get(
            reverse("company-leave-creation"), HTTP_HX_REQUEST="true"
        )

        self.assertEqual(response.status_code, 200)


# ======================================================================
# C-8 — whose attendance request is this?
# ======================================================================


class NewAttendanceRequestScopeTests(P0TestCase):
    URL_NAME = "request-new-attendance"

    def setUp(self):
        super().setUp()
        self.shift = EmployeeShift.objects.create(employee_shift="Ca hành chính")
        self.shift.company_id.add(self.company)

    def payload(self, employee):
        """A payload the form actually accepts.

        `shift_id`, `attendance_worked_hour` and `minimum_hour` are required by
        `NewRequestForm`; without them the form fails validation for everybody
        and the negative tests below would pass for the wrong reason — they would
        be asserting that an invalid form creates nothing, not that an
        unauthorized employee cannot be chosen.
        """
        yesterday = date.today() - timedelta(days=1)
        return {
            "employee_id": employee.pk,
            "attendance_date": yesterday,
            "attendance_clock_in_date": yesterday,
            "attendance_clock_in": "08:00",
            "attendance_clock_out_date": yesterday,
            "attendance_clock_out": "17:00",
            "shift_id": self.shift.pk,
            "attendance_worked_hour": "09:00",
            "minimum_hour": "08:00",
            "request_description": "Quên chấm công.",
        }

    def test_an_employee_cannot_file_for_a_colleague(self):
        """C-8. The subordinate narrowing used to run only on the GET render."""
        self.client.force_login(self.stranger_user)
        self.select_company(self.company)

        self.client.post(
            reverse(self.URL_NAME), self.payload(self.worker), HTTP_HX_REQUEST="true"
        )

        self.assertFalse(
            Attendance.objects.filter(employee_id=self.worker).exists(),
            msg="a colleague's attendance row must not be creatable",
        )

    def test_a_manager_cannot_file_for_another_companys_employee(self):
        self.client.force_login(self.leader_user)
        self.select_company(self.company)

        self.client.post(
            reverse(self.URL_NAME),
            self.payload(self.outsider_report),
            HTTP_HX_REQUEST="true",
        )

        self.assertFalse(
            Attendance.objects.filter(employee_id=self.outsider_report).exists()
        )

    def test_a_manager_can_still_file_for_their_own_report(self):
        self.client.force_login(self.leader_user)
        self.select_company(self.company)

        self.client.post(
            reverse(self.URL_NAME), self.payload(self.worker), HTTP_HX_REQUEST="true"
        )

        self.assertTrue(
            Attendance.objects.filter(employee_id=self.worker).exists(),
            msg="the manager flow this form exists for must be untouched",
        )

    def test_a_crafted_emp_id_query_string_does_not_widen_the_choices(self):
        """The `?emp_id=` prefill now intersects the allowed set instead of replacing it."""
        self.client.force_login(self.stranger_user)
        self.select_company(self.company)

        self.client.post(
            reverse(self.URL_NAME) + f"?emp_id={self.worker.pk}",
            self.payload(self.worker),
            HTTP_HX_REQUEST="true",
        )

        self.assertFalse(Attendance.objects.filter(employee_id=self.worker).exists())


# ======================================================================
# C-9 — out-of-radius approval
# ======================================================================


class OutsideRadiusApprovalTests(P0TestCase):
    def setUp(self):
        super().setUp()
        self.attendance = Attendance.objects.create(
            employee_id=self.worker,
            attendance_date=date.today(),
            attendance_clock_in_date=date.today(),
            attendance_clock_in="08:00",
            is_validate_request=True,
            request_description="Chấm công ngoài bán kính.",
            minimum_hour="08:00",
        )

    def approve_url(self):
        return reverse(
            "approve-validate-attendance-request",
            kwargs={"attendance_id": self.attendance.pk},
        )

    def test_a_manager_of_another_team_cannot_approve(self):
        """C-9. Approve was gated globally while reject already checked the row.

        The actor is a leader in the SAME company who manages somebody else, so
        `@manager_can_enter` admits them and the company-scoped lookup finds the
        row. Only the per-row check can refuse them — which is exactly the gap
        this finding was about, and why a cross-company actor would not prove it
        (they would be stopped earlier, by the company scope).
        """
        self.client.force_login(self.other_leader_user)
        self.select_company(self.company)

        response = self.client.post(self.approve_url())

        self.assertEqual(response.status_code, 403)
        self.attendance.refresh_from_db()
        self.assertFalse(self.attendance.attendance_validated)

    def test_the_employees_own_reporting_manager_can_still_approve(self):
        self.client.force_login(self.leader_user)
        self.select_company(self.company)

        self.client.post(self.approve_url())

        self.attendance.refresh_from_db()
        self.assertTrue(
            self.attendance.attendance_validated,
            msg="the reviewer this screen is built for must keep working",
        )

    def test_bulk_approve_survives_a_crafted_id_and_skips_what_it_may_not_touch(self):
        import json

        foreign = Attendance.objects.create(
            employee_id=self.outsider_report,
            attendance_date=date.today(),
            attendance_clock_in_date=date.today(),
            attendance_clock_in="08:00",
            is_validate_request=True,
            request_description="Của công ty khác.",
            minimum_hour="08:00",
        )
        self.client.force_login(self.leader_user)
        self.select_company(self.company)

        response = self.client.post(
            reverse("bulk-approve-attendance-request"),
            {"ids": json.dumps([self.attendance.pk, foreign.pk, 10**6])},
        )

        self.assertLess(
            response.status_code,
            500,
            msg="a crafted id used to raise DoesNotExist",
        )
        self.attendance.refresh_from_db()
        foreign.refresh_from_db()
        self.assertTrue(self.attendance.attendance_validated)
        self.assertFalse(foreign.attendance_validated)
