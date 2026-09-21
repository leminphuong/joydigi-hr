"""Phase 3B.1 — TEMPORARY. Delete with `attendance/views/client_ip_debug.py`.

The diagnostic reports four request values and nothing else. What is
worth testing is not that it can read a header — Django does that — but
that it cannot be reached by the wrong person and cannot be talked into
reporting anything beyond its four fields.
"""

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from joydigi.testkit import make_company, make_employee, make_user

# What production is expected to look like: Cloudflare in front, nginx
# behind it, gunicorn behind that. The values are documentation examples
# (RFC 5737), never real addresses.
PROXY_HEADERS = {
    "REMOTE_ADDR": "198.51.100.10",
    "HTTP_X_FORWARDED_FOR": "203.0.113.20, 198.51.100.10",
    "HTTP_X_REAL_IP": "203.0.113.20",
    "HTTP_CF_CONNECTING_IP": "203.0.113.20",
}

ALLOWED_KEYS = {
    "remote_addr", "remote_addr_present",
    "x_forwarded_for", "x_forwarded_for_present",
    "x_real_ip", "x_real_ip_present",
    "cf_connecting_ip", "cf_connecting_ip_present",
    "performance", "diagnostic_total_ms",
}

PERFORMANCE_KEYS = {"cache", "database", "tables", "tables_ms", "runtime"}
CACHE_KEYS = {"configured", "backend", "probe_safe", "set_ms", "get_ms",
              "delete_ms", "ok", "error_type"}
DATABASE_KEYS = {"vendor", "select_1_ms", "ok", "error_type"}
TABLE_KEYS = {"attendance_count", "leave_request_count",
              "notification_count", "activity_log_count"}
RUNTIME_KEYS = {"debug", "gunicorn_workers"}

# Shapes that must never appear anywhere in the response, whatever the
# environment happens to be configured with.
FORBIDDEN_SUBSTRINGS = (
    "redis://", "rediss://", "postgres://", "postgresql://", "sqlite://",
    "REDIS_URL", "DATABASE_URL", "SECRET_KEY", "PASSWORD", "password",
    "LOCATION", "OPTIONS", "BACKEND", "HTTP_", "sessionid", "csrftoken",
    "Authorization", "os.environ", "Traceback",
)


class ClientIpDebugBase(TestCase):
    def setUp(self):
        self.url = reverse("client-ip-debug")
        self.company = make_company("IP Debug Co")

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

    def _login(self, user):
        self.client.force_login(user)


class PermissionTests(ClientIpDebugBase):
    def test_a_an_unauthenticated_caller_is_turned_away(self):
        response = self.client.get(self.url, **PROXY_HEADERS)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.url)

    def test_b_an_ordinary_employee_is_turned_away(self):
        self._login(self._user("ipdebug_staffless", permitted=False))
        response = self.client.get(self.url, **PROXY_HEADERS)
        # `handle_no_permission` redirects; what matters is that no
        # diagnostic payload is produced.
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("application/json", response.get("Content-Type", ""))

    def test_c_an_administrator_of_the_allowed_ip_screen_may_look(self):
        self._login(self._user("ipdebug_admin", permitted=True))
        response = self.client.get(self.url, **PROXY_HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_c2_a_superuser_may_look(self):
        user = make_user("ipdebug_root", password="secret123", is_superuser=True)
        make_employee(company=self.company, email="root@test.joydigi", user=user)
        self._login(type(user).objects.get(pk=user.pk))
        self.assertEqual(self.client.get(self.url, **PROXY_HEADERS).status_code, 200)


class PayloadTests(ClientIpDebugBase):
    def setUp(self):
        super().setUp()
        self._login(self._user("ipdebug_admin", permitted=True))

    def get(self, **extra):
        response = self.client.get(self.url, **extra)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_d_it_reports_exactly_what_django_received(self):
        body = self.get(**PROXY_HEADERS)
        self.assertEqual(body["remote_addr"], "198.51.100.10")
        self.assertEqual(body["x_forwarded_for"], "203.0.113.20, 198.51.100.10")
        self.assertEqual(body["x_real_ip"], "203.0.113.20")
        self.assertEqual(body["cf_connecting_ip"], "203.0.113.20")
        self.assertTrue(all(body[f"{k}_present"] for k in
                            ("remote_addr", "x_forwarded_for", "x_real_ip",
                             "cf_connecting_ip")))

    def test_d2_a_missing_header_is_reported_as_absent_not_guessed(self):
        body = self.get(REMOTE_ADDR="198.51.100.10")
        self.assertFalse(body["cf_connecting_ip_present"])
        self.assertIsNone(body["cf_connecting_ip"])
        self.assertFalse(body["x_forwarded_for_present"])

    def test_d3_the_response_has_no_key_beyond_the_four_fields(self):
        self.assertEqual(set(self.get(**PROXY_HEADERS)), ALLOWED_KEYS)

    def test_e_nothing_sensitive_can_appear_in_the_response(self):
        body = self.client.get(
            self.url,
            HTTP_AUTHORIZATION="Bearer supersecrettoken",
            HTTP_COOKIE="sessionid=supersecretsession",
            HTTP_X_CSRFTOKEN="supersecretcsrf",
            HTTP_USER_AGENT="supersecretagent",
            **PROXY_HEADERS,
        ).content.decode()
        for secret in ("supersecrettoken", "supersecretsession",
                       "supersecretcsrf", "supersecretagent",
                       "sessionid", "Authorization", "HTTP_"):
            self.assertNotIn(secret, body)

    def test_e2_a_client_supplied_forwarded_header_is_shown_never_trusted(self):
        # Visibility only. Phase 3B.2 decides what may be believed; this
        # page has no opinion and grants nothing.
        body = self.get(REMOTE_ADDR="198.51.100.10",
                        HTTP_X_FORWARDED_FOR="1.2.3.4")
        self.assertEqual(body["x_forwarded_for"], "1.2.3.4")
        self.assertEqual(body["remote_addr"], "198.51.100.10")

    def test_f_only_get_is_answered(self):
        for method in ("post", "put", "patch", "delete"):
            response = getattr(self.client, method)(self.url, **PROXY_HEADERS)
            self.assertEqual(response.status_code, 405, method)


class PerformanceSectionTests(ClientIpDebugBase):
    """Phase GLOBAL-ADMIN-PERF-A — the shared-path measurements.

    The numbers themselves are whatever this machine happens to be; what
    is pinned here is the shape, the safety and the absence of side
    effects.
    """

    def setUp(self):
        super().setUp()
        self._login(self._user("ipdebug_admin", permitted=True))

    def body(self):
        response = self.client.get(self.url, **PROXY_HEADERS)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_performance_block_has_exactly_its_allow_listed_shape(self):
        perf = self.body()["performance"]
        self.assertEqual(set(perf), PERFORMANCE_KEYS)
        self.assertEqual(set(perf["cache"]), CACHE_KEYS)
        self.assertEqual(set(perf["database"]), DATABASE_KEYS)
        self.assertEqual(set(perf["tables"]), TABLE_KEYS)
        self.assertEqual(set(perf["runtime"]), RUNTIME_KEYS)
        self.assertEqual(set(perf["tables_ms"]),
                         {f"{name}_ms" for name in TABLE_KEYS})

    def test_the_cache_backend_is_named_but_never_located(self):
        cache_info = self.body()["performance"]["cache"]
        self.assertTrue(cache_info["configured"])
        # A bare class name, never a dotted path and never a URL.
        self.assertNotIn(".", cache_info["backend"])
        self.assertNotIn(":", cache_info["backend"])

    def test_a_locally_bounded_backend_is_probed_and_timed(self):
        # The test settings use an in-process cache, which cannot hang.
        cache_info = self.body()["performance"]["cache"]
        self.assertTrue(cache_info["probe_safe"])
        self.assertTrue(cache_info["ok"], cache_info["error_type"])
        for field in ("set_ms", "get_ms", "delete_ms"):
            self.assertIsInstance(cache_info[field], (int, float), field)

    def test_a_network_backend_without_timeouts_is_refused_not_probed(self):
        # The production shape this gate exists for: django-redis with no
        # socket timeout. It must be reported, never contacted.
        #
        # Asserted against `_probe_cache` directly rather than through a
        # request: `override_settings(CACHES=...)` would also redirect the
        # accessibility middleware and the database template loader onto
        # that Redis, so the request would fail for reasons that have
        # nothing to do with what is being tested here. The gate is a
        # pure function of configuration, which is the point of it.
        from django.test import override_settings

        from attendance.views.client_ip_debug import _probe_cache

        unbounded = {
            "default": {
                "BACKEND": "django_redis.cache.RedisCache",
                "LOCATION": "redis://127.0.0.1:6379/0",
                "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
            }
        }
        with override_settings(CACHES=unbounded):
            cache_info = _probe_cache()
        self.assertEqual(cache_info["backend"], "RedisCache")
        self.assertFalse(cache_info["probe_safe"])
        self.assertIsNone(cache_info["set_ms"])
        self.assertIsNone(cache_info["get_ms"])
        self.assertIsNone(cache_info["delete_ms"])
        self.assertFalse(cache_info["ok"])
        self.assertIsNone(cache_info["error_type"])

    def test_a_network_backend_with_timeouts_is_allowed_to_be_probed(self):
        from django.test import override_settings

        from attendance.views import client_ip_debug

        bounded = {
            "default": {
                "BACKEND": "django_redis.cache.RedisCache",
                "LOCATION": "redis://127.0.0.1:6379/0",
                "OPTIONS": {
                    "SOCKET_CONNECT_TIMEOUT": 2,
                    "SOCKET_TIMEOUT": 2,
                },
            }
        }
        with override_settings(CACHES=bounded):
            config, name = client_ip_debug._cache_config()
            self.assertEqual(name, "RedisCache")
            self.assertTrue(client_ip_debug._cache_probe_is_bounded(config, name))

    def test_an_unconfigured_cache_is_never_probed(self):
        from django.test import override_settings

        from attendance.views.client_ip_debug import _probe_cache

        with override_settings(CACHES={}):
            cache_info = _probe_cache()
        self.assertFalse(cache_info["configured"])
        self.assertIsNone(cache_info["backend"])
        self.assertFalse(cache_info["probe_safe"])

    def test_the_probe_key_does_not_outlive_the_probe(self):
        from django.core.cache import cache

        from attendance.views.client_ip_debug import _PROBE_KEY

        self.body()
        self.assertIsNone(cache.get(_PROBE_KEY))

    def test_the_probe_leaves_application_cache_values_alone(self):
        from django.core.cache import cache

        cache.set("some_application_key", "untouched", 60)
        self.body()
        self.assertEqual(cache.get("some_application_key"), "untouched")

    def test_the_database_probe_reports_a_vendor_and_a_round_trip(self):
        database = self.body()["performance"]["database"]
        self.assertIn(database["vendor"], {"sqlite", "postgresql", "mysql", "oracle"})
        self.assertTrue(database["ok"], database["error_type"])
        self.assertIsInstance(database["select_1_ms"], (int, float))

    def test_the_database_probe_writes_nothing(self):
        from attendance.models import Attendance
        from joydigi_audit.models import UserActivityLog

        before = (Attendance.objects.count(), UserActivityLog.objects.count())
        self.body()
        after = (Attendance.objects.count(), UserActivityLog.objects.count())
        # The activity-log middleware writes one row per request; that is
        # the application's behaviour, not this page's, so only the
        # business table is asserted unchanged.
        self.assertEqual(before[0], after[0])
        self.assertGreaterEqual(after[1], before[1])

    def test_tables_report_counts_only(self):
        tables = self.body()["performance"]["tables"]
        for name, value in tables.items():
            self.assertIsInstance(value, int, name)

    def test_runtime_reports_debug_and_declines_to_guess_worker_count(self):
        runtime = self.body()["performance"]["runtime"]
        self.assertIsInstance(runtime["debug"], bool)
        self.assertIsNone(runtime["gunicorn_workers"])

    def test_the_view_times_itself(self):
        total = self.body()["diagnostic_total_ms"]
        self.assertIsInstance(total, (int, float))
        self.assertGreaterEqual(total, 0)

    def test_no_connection_string_or_secret_can_appear_in_the_response(self):
        raw = self.client.get(
            self.url,
            HTTP_AUTHORIZATION="Bearer supersecrettoken",
            HTTP_COOKIE="sessionid=supersecretsession",
            **PROXY_HEADERS,
        ).content.decode()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(forbidden, raw, forbidden)

    def test_a_failing_probe_reveals_only_the_exception_class(self):
        from unittest import mock

        class ExplodingCache:
            """Fails the way a misconfigured Redis client does — with the
            connection string in the message. Bound over the module's own
            name, not over the shared cache object, so the middleware and
            the template loader keep the real cache."""

            def set(self, *a, **kw):
                raise RuntimeError("redis://user:hunter2@10.0.0.5:6379 refused")

            def get(self, *a, **kw):
                raise RuntimeError("redis://user:hunter2@10.0.0.5:6379 refused")

            def delete(self, *a, **kw):
                raise RuntimeError("redis://user:hunter2@10.0.0.5:6379 refused")

        with mock.patch(
            "attendance.views.client_ip_debug.cache", ExplodingCache()
        ):
            raw = self.client.get(self.url, **PROXY_HEADERS)
        cache_info = raw.json()["performance"]["cache"]
        self.assertEqual(cache_info["error_type"], "RuntimeError")
        self.assertFalse(cache_info["ok"])
        body = raw.content.decode()
        self.assertNotIn("hunter2", body)
        self.assertNotIn("10.0.0.5", body)
        self.assertNotIn("refused", body)
