"""
okr_chat_cleanup_uploads — delete chat files uploaded but never sent.

A file is uploaded before its message is sent; one never attached to a message
(the sender gave up, the page closed) only takes space and counts toward the
uploader's 20 pending files. This deletes those older than ``--older-than-hours``
(default 24, never under 1). A file attached to a message is never touched;
each deletion re-checks that, row locked, inside a transaction.

Dry run by default: it lists what it would delete (ids, sizes — never names).
Deleting needs ``--apply`` and ``--expect-count`` (the number the dry run
showed), so a surprise never deletes more than was reviewed.

    python manage.py okr_chat_cleanup_uploads
    python manage.py okr_chat_cleanup_uploads --apply --expect-count 3

Run it through ``manage.py shell -c`` on the server to keep schedulers off,
like the other maintenance commands.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from okr.services.chat_files import MIN_CLEANUP_AGE, cleanup_stale_uploads


class Command(BaseCommand):
    help = "Delete OKR chat attachments that were uploaded but never sent."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-hours", type=float, default=24.0)
        parser.add_argument("--company", action="append", type=int, help="Only these company ids.")
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--apply", action="store_true", help="Really delete (default: dry run).")
        parser.add_argument("--expect-count", type=int, help="Required with --apply: rows the dry run listed.")

    def handle(self, *args, **options):
        age = datetime.timedelta(hours=options["older_than_hours"])
        if age < MIN_CLEANUP_AGE:
            raise CommandError(f"--older-than-hours must be at least {MIN_CLEANUP_AGE.total_seconds() / 3600:g}")
        if not 1 <= options["limit"] <= 5000:
            raise CommandError("--limit must be between 1 and 5000")
        companies = options.get("company") or None
        preview = cleanup_stale_uploads(age, dry_run=True, company_ids=companies, limit=options["limit"])
        out = self.stdout.write
        out(f"OKR chat uploads never sent, older than {options['older_than_hours']:g} h: {preview['found']} ({preview['bytes']} bytes)")
        for attachment_id, company_id, uploader_id, size, created_at in preview["rows"]:
            out(f"    UNSENT  attachment={attachment_id} company={company_id} uploader={uploader_id} size={size} uploaded={created_at:%Y-%m-%d %H:%M}")
        if not options["apply"]:
            out(self.style.WARNING("DRY RUN — nothing was deleted."))
            return
        if options.get("expect_count") is None:
            raise CommandError("--apply needs --expect-count (the number listed by the dry run)")
        if options["expect_count"] != preview["found"]:
            raise CommandError(f"--expect-count {options['expect_count']} but {preview['found']} found — nothing deleted")
        result = cleanup_stale_uploads(age, dry_run=False, company_ids=companies, limit=options["limit"])
        out(f"  deleted: {result['deleted']} ({result['bytes']} bytes) | kept (sent meanwhile): {result['skipped']}")
        out(self.style.SUCCESS("Cleanup complete."))
