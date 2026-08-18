"""
Fix: the filter dropdown in stage/filters.html contains a
button.oh-btn--secondary (.filterButton) that appears earlier in the DOM
than the Create button, so Driver.js highlighted the wrong element.

Fix: change the selector from  button.oh-btn--secondary
                         to    button[data-toggle="oh-modal-toggle"].oh-btn--secondary
which uniquely targets the Create button and skips the filter button.
"""
from django.db import migrations


def fix(apps, schema_editor):
    Tour = apps.get_model("joydigi_tour", "Tour")
    TourStep = apps.get_model("joydigi_tour", "TourStep")
    try:
        tour = Tour.objects.get(slug="recruitment-stage-tour")
    except Tour.DoesNotExist:
        return
    TourStep.objects.filter(
        tour=tour, element_selector="button.oh-btn--secondary"
    ).update(element_selector='button[data-toggle="oh-modal-toggle"].oh-btn--secondary')


def revert(apps, schema_editor):
    Tour = apps.get_model("joydigi_tour", "Tour")
    TourStep = apps.get_model("joydigi_tour", "TourStep")
    try:
        tour = Tour.objects.get(slug="recruitment-stage-tour")
    except Tour.DoesNotExist:
        return
    TourStep.objects.filter(
        tour=tour,
        element_selector='button[data-toggle="oh-modal-toggle"].oh-btn--secondary',
    ).update(element_selector="button.oh-btn--secondary")


class Migration(migrations.Migration):

    dependencies = [
        ("joydigi_tour", "0014_seed_stage_tour"),
    ]

    operations = [
        migrations.RunPython(fix, revert),
    ]
