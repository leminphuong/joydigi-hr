"""The Director role group for tests (2026-11-17).

Migration ``base 0017`` creates it. A ``TransactionTestCase`` flushes every
table, and a ``--keepdb`` test database keeps that flushed state for the next
run, so tests get the group through the migration's own function instead of
assuming it is still there."""

from __future__ import annotations

import importlib

from django.apps import apps as global_apps


def director_group():
    from base.roles import director_groups

    group = director_groups().first()
    if group is None:
        migration = importlib.import_module("base.migrations.0017_director_role")
        migration.create_director_role(global_apps, None)
        group = director_groups().get()
    return group
