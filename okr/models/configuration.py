from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from okr.models.base import TimeStampedModel
from okr.models.choices import ReminderFrequency


def default_rating_ranges():
    """JOYOKR's built-in rating bands (report controller ``default_ranges``)."""
    return [
        {"min": "0", "max": "0.3", "label": "Failed"},
        {"min": "0.3", "max": "0.6", "label": "Below Expectation"},
        {"min": "0.6", "max": "0.8", "label": "Good"},
        {"min": "0.8", "max": "1", "label": "Excellent"},
    ]


def validate_rating_ranges(ranges):
    """Same rules as JOYOKR: exactly four bands, contiguous, covering 0..1."""
    if not isinstance(ranges, list) or len(ranges) != 4:
        raise ValidationError(_("Exactly four rating ranges are required."))
    clean = []
    for item in ranges:
        try:
            low, high = Decimal(str(item["min"])), Decimal(str(item["max"]))
            label = str(item["label"]).strip()
        except (KeyError, TypeError, ArithmeticError) as exc:
            raise ValidationError(_("Invalid rating range.")) from exc
        clean.append((low, high, label))
    clean.sort(key=lambda band: band[0])
    if clean[0][0] != 0 or clean[-1][1] != 1:
        raise ValidationError(_("Rating ranges must cover 0 to 1."))
    for index, (low, high, label) in enumerate(clean):
        if low < 0 or high > 1 or low >= high or not label:
            raise ValidationError(_("Invalid rating range."))
        if index and abs(low - clean[index - 1][1]) > Decimal("0.0001"):
            raise ValidationError(_("Rating ranges must be contiguous and must not overlap."))


class OKRSettings(TimeStampedModel):
    """Per-company OKR configuration (JOYOKR kept these in ``wp_options``).

    Secrets that JOYOKR stored next to them (Telegram bot token, Workspace SSO
    secret, chat keys) are deliberately not modelled here.
    """

    company = models.OneToOneField(
        "base.Company",
        on_delete=models.PROTECT,
        related_name="okr_settings",
        verbose_name=_("Company"),
    )
    warning_threshold = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("15"),
        verbose_name=_("At-risk threshold (percentage points)"),
    )
    reminder_frequency = models.CharField(
        max_length=20,
        choices=ReminderFrequency.choices,
        default=ReminderFrequency.WEEKLY,
        verbose_name=_("Check-in reminder frequency"),
    )
    email_reminders = models.BooleanField(default=True, verbose_name=_("Email reminders"))
    telegram_enabled = models.BooleanField(default=False, verbose_name=_("Telegram reminders"))
    rating_ranges = models.JSONField(
        default=default_rating_ranges,
        validators=[validate_rating_ranges],
        verbose_name=_("Rating ranges"),
    )

    class Meta:
        db_table = "okr_settings"
        verbose_name = _("OKR settings")
        verbose_name_plural = _("OKR settings")
        constraints = [
            models.CheckConstraint(
                condition=Q(warning_threshold__gte=0) & Q(warning_threshold__lte=100),
                name="okr_settings_threshold_0_100",
            ),
        ]

    def __str__(self):
        return f"OKR settings · {self.company}"
