from datetime import timedelta

from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils import timezone

from base.templatetags.basefilters import notification_text_vi, relative_time_vi


class VietnameseNotificationFilterTests(SimpleTestCase):
    def test_translates_existing_roster_notification(self):
        self.assertEqual(
            notification_text_vi("Your roster has been published."),
            "Lịch làm việc của bạn đã được công bố.",
        )

    def test_translates_existing_dated_roster_notification(self):
        self.assertEqual(
            notification_text_vi(
                "Your roster from 2026-08-17 - 2026-08-23 has been published."
            ),
            "Lịch làm việc của bạn từ 2026-08-17 - 2026-08-23 đã được công bố.",
        )

    def test_relative_time_is_always_vietnamese(self):
        timestamp = timezone.now() - timedelta(seconds=125)
        self.assertEqual(relative_time_vi(timestamp), "2 phút trước")

    def test_notification_template_filters_legacy_database_value(self):
        rendered = Template(
            "{% load basefilters %}"
            "{{ verb|notification_text_vi }}|{{ timestamp|relative_time_vi }}"
        ).render(
            Context(
                {
                    "verb": "Your roster has been published.",
                    "timestamp": timezone.now() - timedelta(hours=3),
                }
            )
        )
        self.assertEqual(
            rendered,
            "Lịch làm việc của bạn đã được công bố.|3 giờ trước",
        )
