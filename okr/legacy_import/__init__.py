"""JOYOKR (WordPress) → Django OKR data migration.

* ``export`` reads the JSON file written by ``joyokr_export.php`` (read-only
  WP-CLI exporter shipped next to this package).
* ``mapping`` proposes WordPress user → HR employee and JOYOKR department →
  HR department matches for a person to verify; nothing is matched on e-mail
  alone and nothing is verified automatically.
* ``importer`` writes the verified data in one transaction, idempotently
  (``legacy_wp_id``), as a dry run unless asked to apply.
* ``rollback`` removes the rows one applied batch created.

See ``okr/docs/wp_migration.md`` for the field mapping and the runbook.
"""
