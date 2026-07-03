from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from models.models import ScheduledJob
from models.scheduler import enqueue_due_scheduled_jobs

DEFAULT_SCHEDULES = [
    {
        "profile_id": "default",
        "job_type": "update_downloads",
        "interval_seconds": 3600,
        "payload": {"source": "scheduler"},
        "idempotency_key_template": "scheduled:update_downloads:${profile_id}:${due_hour}",
    },
    {
        "profile_id": "default",
        "job_type": "transfer_media",
        "interval_seconds": 3600,
        "payload": {"source": "scheduler"},
        "idempotency_key_template": "scheduled:transfer_media:${profile_id}:${due_hour}",
    },
    {
        "profile_id": "default",
        "job_type": "retention_cleanup",
        "interval_seconds": 86400,
        "payload": {"source": "scheduler"},
        "idempotency_key_template": "scheduled:retention_cleanup:${profile_id}:${due_date}",
    },
]


class Command(BaseCommand):
    help = "Enqueue due ScheduledJob rows, optionally running continuously."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop", action="store_true", help="Keep polling for due scheduled jobs."
        )
        parser.add_argument(
            "--poll-seconds",
            type=int,
            default=60,
            help="Polling interval when --loop is used.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum due schedules to enqueue per pass.",
        )
        parser.add_argument(
            "--install-defaults",
            action="store_true",
            help="Create default scheduler rows if missing.",
        )

    def handle(self, *args, **options):
        if options["install_defaults"]:
            self._install_defaults()
        if options["loop"]:
            poll_seconds = max(5, int(options["poll_seconds"]))
            while True:
                count = self._run_once(limit=options["limit"])
                self.stdout.write(
                    f"Scheduler pass enqueued {count} job(s); sleeping {poll_seconds}s"
                )
                time.sleep(poll_seconds)
        else:
            count = self._run_once(limit=options["limit"])
            self.stdout.write(self.style.SUCCESS(f"Scheduler enqueued {count} job(s)"))

    def _install_defaults(self) -> None:
        now = timezone.now()
        for spec in DEFAULT_SCHEDULES:
            schedule, created = ScheduledJob.objects.get_or_create(
                profile_id=spec["profile_id"],
                job_type=spec["job_type"],
                defaults={
                    "enabled": True,
                    "interval_seconds": spec["interval_seconds"],
                    "payload": spec["payload"],
                    "idempotency_key_template": spec["idempotency_key_template"],
                    "next_run_at": now,
                },
            )
            if created:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created scheduled job {schedule.job_type} for {schedule.profile_id}"
                    )
                )

    def _run_once(self, *, limit: int) -> int:
        return len(enqueue_due_scheduled_jobs(limit=limit))
