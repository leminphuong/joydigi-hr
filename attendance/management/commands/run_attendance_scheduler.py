"""Phase NOTIFY-2 — the one process that owns the background jobs.

Until now every web process owned a scheduler, because importing
`attendance.scheduler` started one and `attendance/apps.py` imports it
from `AppConfig.ready()`. Under `gunicorn --workers 3` that is three
schedulers on the same one-minute tick, each with its own in-memory
jobstore and nothing between them. The four attendance reminders survived
that only because every reminder checks for a stored notification before
sending — a second line of defence doing the work of the first.

This command is the first line: run it as a single process, set
`ATTENDANCE_SCHEDULER_MODE=dedicated` so the web workers start none, and
the jobs have exactly one owner.

    python manage.py run_attendance_scheduler

It blocks, which is what a process manager wants, and shuts the scheduler
down cleanly on SIGINT/SIGTERM so a restart does not leave a job
half-run. The jobs themselves are not defined here — `register_jobs` is
shared with the embedded path, so there is one description of what runs
and how often, and the two modes cannot drift apart.
"""

import signal

from apscheduler.schedulers.blocking import BlockingScheduler
from django.core.management.base import BaseCommand, CommandError

from attendance.scheduler import (
    MODE_DEDICATED,
    MODE_DISABLED,
    register_jobs,
    scheduler_mode,
)


class Command(BaseCommand):
    help = "Run the attendance background scheduler in its own process."

    def handle(self, *args, **options):
        mode = scheduler_mode()
        if mode == MODE_DISABLED:
            raise CommandError(
                "ATTENDANCE_SCHEDULER_MODE is 'disabled', so refusing to start. "
                "Set it to 'dedicated' to run the scheduler in this process."
            )
        if mode != MODE_DEDICATED:
            # Importing `attendance.scheduler` in embedded mode has
            # already started one in this very process; starting a second
            # here would double every job rather than own it.
            raise CommandError(
                f"ATTENDANCE_SCHEDULER_MODE is '{mode}', which means the web "
                "processes already own a scheduler. Set it to "
                f"'{MODE_DEDICATED}' before running this command, or the jobs "
                "would run twice."
            )

        scheduler = register_jobs(
            BlockingScheduler(timezone=self._timezone())
        )

        def shutdown(signum, _frame):
            # `wait=False`: a job already running is allowed to finish its
            # own transaction, but the process must not linger waiting for
            # a ten-minute interval job that has not started.
            self.stdout.write(
                self.style.WARNING(f"signal {signum} received, shutting down")
            )
            scheduler.shutdown(wait=False)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, shutdown)
            except (ValueError, AttributeError, OSError):
                # Not the main thread, or a platform without this signal.
                # Losing the graceful path is not a reason to refuse to run.
                pass

        self.stdout.write(
            self.style.SUCCESS(
                f"attendance scheduler running ({len(scheduler.get_jobs())} jobs)"
            )
        )
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown(wait=False)

    @staticmethod
    def _timezone():
        import pytz
        from django.conf import settings

        return pytz.timezone(settings.TIME_ZONE)
