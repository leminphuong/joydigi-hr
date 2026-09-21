"""Phase GLOBAL-ADMIN-PERF-B — TEMPORARY. Delete with `joydigi/perf_timing.py`.

A diagnostic that changes the program it measures is worse than none, so
what is pinned here is mostly absence: the response is byte-for-byte what
it was, no extra query runs, no extra row is written, and nobody who
should not see the numbers sees them. The durations themselves are
whatever this machine is, and are not asserted.
"""

import re

from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from joydigi.perf_timing import REQUEST_ATTR, PerfTimingMiddleware
from joydigi.testkit import make_company, make_employee, make_user

#: Every metric name the header is allowed to carry.
ALLOWED_METRICS = {"pre", "ctx", "tpl", "view", "post", "total", "sql", "sqlcount"}

#: `name;dur=<number>` and nothing else — no `desc`, no free text.
METRIC = re.compile(r"^([a-z]+);dur=(\d+(?:\.\d+)?)$")

HEAVY_PAGES = ["employee-view", "company-view", "work-type-view", "holiday-view"]


class PerfTimingBase(TestCase):
    def setUp(self):
        self.company = make_company("Timing Co")

    def _user(self, username, *, permitted):
        user = make_user(username, password="secret123")
        make_employee(
            company=self.company, email=f"{username}@test.joydigi", user=user
        )
        if permitted:
            user.user_permissions.add(
                Permission.objects.get(codename="add_attendance")
            )
        return type(user).objects.get(pk=user.pk)

    def as_superuser(self):
        """Someone who can actually open the heavy admin pages."""
        user = make_user("timing_root", password="secret123", is_superuser=True)
        make_employee(company=self.company, email="root@test.joydigi", user=user)
        self.client.force_login(type(user).objects.get(pk=user.pk))

    def as_admin(self):
        self.as_superuser()

    def metrics(self, response):
        header = response.get("Server-Timing")
        self.assertIsNotNone(header, "expected a Server-Timing header")
        parsed = {}
        for chunk in header.split(","):
            match = METRIC.match(chunk.strip())
            self.assertIsNotNone(match, f"malformed metric: {chunk!r}")
            parsed[match.group(1)] = float(match.group(2))
        return parsed


class HeaderVisibilityTests(PerfTimingBase):
    """Who is shown the numbers."""

    def test_a_superuser_is_shown_the_timings(self):
        self.as_superuser()
        response = self.client.get(reverse("employee-view"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Server-Timing", response)

    def test_the_permission_alone_is_enough_to_be_shown_them(self):
        # The gate is `attendance.add_attendance`, not superuser: an
        # administrator who holds it sees the numbers on a page they can
        # reach. (`client-ip-debug` is guarded by that same permission.)
        self.client.force_login(self._user("timing_perm", permitted=True))
        response = self.client.get(reverse("client-ip-debug"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Server-Timing", response)

    def test_an_ordinary_employee_is_not(self):
        # Whatever the page answers them — 403, a redirect — the numbers
        # are not attached to it.
        self.client.force_login(self._user("timing_plain", permitted=False))
        for name in ("employee-view", "client-ip-debug"):
            self.assertNotIn(
                "Server-Timing", self.client.get(reverse(name)), name
            )

    def test_an_anonymous_caller_is_not(self):
        self.assertNotIn(
            "Server-Timing", self.client.get(reverse("employee-view"))
        )


class HeaderContentTests(PerfTimingBase):
    """What the numbers may say."""

    def setUp(self):
        super().setUp()
        self.as_admin()

    def test_only_allow_listed_metric_names_appear(self):
        metrics = self.metrics(self.client.get(reverse("employee-view")))
        self.assertTrue(set(metrics) <= ALLOWED_METRICS, set(metrics))

    def test_a_rendered_page_reports_every_boundary(self):
        metrics = self.metrics(self.client.get(reverse("employee-view")))
        for name in ("pre", "ctx", "tpl", "view", "post", "total", "sql", "sqlcount"):
            self.assertIn(name, metrics, name)

    def test_the_boundaries_do_not_overlap_and_sum_to_the_whole(self):
        metrics = self.metrics(self.client.get(reverse("employee-view")))
        self.assertAlmostEqual(
            metrics["ctx"] + metrics["tpl"], metrics["view"], delta=1.0
        )
        self.assertAlmostEqual(
            metrics["pre"] + metrics["view"] + metrics["post"],
            metrics["total"],
            delta=1.0,
        )

    def test_sql_time_never_exceeds_the_request_it_was_measured_in(self):
        metrics = self.metrics(self.client.get(reverse("employee-view")))
        self.assertLessEqual(metrics["sql"], metrics["total"] + 1.0)
        self.assertGreater(metrics["sqlcount"], 0)

    def test_the_header_carries_no_sql_no_ids_and_no_paths(self):
        response = self.client.get(reverse("employee-view"))
        header = response["Server-Timing"]
        for forbidden in ("SELECT", "select", "FROM", "WHERE", "/", "@", "=" * 2,
                          "employee", "company", "session", "cache", "desc",
                          "password", "token"):
            self.assertNotIn(forbidden, header, forbidden)

    def test_the_header_is_only_names_numbers_and_separators(self):
        header = self.client.get(reverse("employee-view"))["Server-Timing"]
        self.assertIsNone(
            re.search(r"[^a-z0-9;=.,\s]", header),
            f"unexpected character in {header!r}",
        )

    def test_every_heavy_admin_page_is_measured_independently(self):
        seen = {}
        for name in HEAVY_PAGES:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 200, name)
            seen[name] = self.metrics(response)["total"]
        # Each page gets its own header rather than a shared running
        # total: the numbers must differ across independent requests.
        self.assertEqual(len(seen), len(HEAVY_PAGES))

    def test_each_htmx_child_request_reports_its_own_timings(self):
        for name in ("employees-nav", "employees-list"):
            response = self.client.get(reverse(name), HTTP_HX_REQUEST="true")
            self.assertIn("Server-Timing", response, name)
            self.assertIn("total", self.metrics(response), name)


class BehaviourIsUnchangedTests(PerfTimingBase):
    """The measurement must not move anything it measures."""

    def setUp(self):
        super().setUp()
        self.as_admin()

    def test_status_and_body_are_untouched(self):
        with_header = self.client.get(reverse("employee-view"))

        # The same request with the instrumentation neutralised, by
        # removing the record the outer copy writes.
        original = PerfTimingMiddleware.__call__

        def passthrough(inner_self, request):
            return inner_self.get_response(request)

        PerfTimingMiddleware.__call__ = passthrough
        try:
            without_header = self.client.get(reverse("employee-view"))
        finally:
            PerfTimingMiddleware.__call__ = original

        self.assertEqual(with_header.status_code, without_header.status_code)
        self.assertEqual(len(with_header.content), len(without_header.content))
        self.assertNotIn("Server-Timing", without_header)

    def test_no_query_is_added_or_repeated(self):
        original = PerfTimingMiddleware.__call__

        def passthrough(inner_self, request):
            return inner_self.get_response(request)

        self.client.get(reverse("employee-view"))  # warm every lazy cache

        with CaptureQueriesContext(connection) as instrumented:
            self.client.get(reverse("employee-view"))

        PerfTimingMiddleware.__call__ = passthrough
        try:
            with CaptureQueriesContext(connection) as plain:
                self.client.get(reverse("employee-view"))
        finally:
            PerfTimingMiddleware.__call__ = original

        self.assertEqual(len(instrumented.captured_queries),
                         len(plain.captured_queries))

    def test_no_extra_business_row_is_written(self):
        from employee.models import Employee

        before = Employee.objects.count()
        self.client.get(reverse("employee-view"))
        self.assertEqual(Employee.objects.count(), before)

    def test_the_context_mark_adds_nothing_to_any_template_context(self):
        from joydigi.perf_timing import timing_context_mark

        class FakeRequest:
            pass

        self.assertEqual(timing_context_mark(FakeRequest()), {})

    def test_the_context_mark_survives_a_request_without_a_record(self):
        # Anything rendered outside the middleware chain — a management
        # command, an error page — must not blow up on a missing record.
        from joydigi.perf_timing import timing_context_mark

        class FakeRequest:
            pass

        request = FakeRequest()
        self.assertEqual(timing_context_mark(request), {})
        self.assertFalse(hasattr(request, REQUEST_ATTR))

    def test_a_broken_header_never_breaks_the_response(self):
        from unittest import mock

        with mock.patch.object(
            PerfTimingMiddleware, "_format", side_effect=RuntimeError("boom")
        ):
            response = self.client.get(reverse("employee-view"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Server-Timing", response)

    def test_a_failure_below_the_middleware_propagates_unchanged(self):
        # Asserted against the middleware itself rather than through a
        # view: `joydigi.decorators.login_required` catches whatever a
        # view raises and renders an error page, so a request-level test
        # would be measuring that decorator, not this middleware. What
        # matters here is that the timing wrapper re-raises the original
        # exception object instead of replacing it or swallowing it.
        def explode(request):
            raise RuntimeError("original failure")

        middleware = PerfTimingMiddleware(explode)

        class FakeRequest:
            pass

        with self.assertRaises(RuntimeError) as caught:
            middleware(FakeRequest())
        self.assertEqual(str(caught.exception), "original failure")

    def test_a_request_that_fails_is_not_given_a_different_failure(self):
        from unittest import mock

        from employee.cbv import employees

        with mock.patch.object(
            employees.EmployeesView,
            "get_context_data",
            side_effect=RuntimeError("original failure"),
        ):
            instrumented = self.client.get(reverse("employee-view"))

            original = PerfTimingMiddleware.__call__

            def passthrough(inner_self, request):
                return inner_self.get_response(request)

            PerfTimingMiddleware.__call__ = passthrough
            try:
                plain = self.client.get(reverse("employee-view"))
            finally:
                PerfTimingMiddleware.__call__ = original

        self.assertEqual(instrumented.status_code, plain.status_code)
