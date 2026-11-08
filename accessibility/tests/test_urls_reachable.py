"""The accessibility settings routes must resolve before the custom-404 catch-all.

They used to be appended after `re_path(r"^.*$", custom404)` in joydigi.urls,
so `/settings/user-accessibility/` (linked from templates/settings.html) and
the two HTMX endpoints answered 404 in production.
"""

from django.test import TestCase
from django.urls import resolve, reverse

from accessibility import views


class AccessibilityRoutesReachableTests(TestCase):
    ROUTES = {
        "user-accessibility": ("/settings/user-accessibility/", views.user_accessibility),
        "load-accessibility-form": ("/settings/load-accessibility-form/", views.load_accessibility_form),
        "get-initial-accessibility-data": ("/get-initial-accessibility-data/", views.get_accessibility_data),
    }

    def test_routes_resolve_to_their_views_not_the_catch_all(self):
        for name, (path, view) in self.ROUTES.items():
            with self.subTest(name=name):
                self.assertEqual(reverse(name), path)
                match = resolve(path)
                self.assertNotEqual(match.url_name, "custom-404")
                self.assertIs(match.func, view)

    def test_anonymous_user_is_sent_to_login_not_404(self):
        response = self.client.get("/settings/user-accessibility/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
