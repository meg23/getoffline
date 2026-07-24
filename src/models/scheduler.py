from __future__ import annotations

from datetime import timedelta
from string import Template
from typing import Any

from django.db import transaction
from django.utils import timezone

from frontend.queue import publish_job
from models.domain import JobType
from models.domain import parse_str_enum
from models.jobs import create_job
from models.models import ScheduledJob


def _render_idempotency_key(schedule: ScheduledJob, due_at) -> str:
    if schedule.idempotency_key_template:
        return Template(schedule.idempotency_key_template).safe_substitute(
            schedule_id=schedule.id,
            profile_id=schedule.profile_id,
            job_type=schedule.job_type,
            due_at=due_at.isoformat(),
            due_date=due_at.date().isoformat(),
            due_hour=due_at.strftime("%Y-%m-%dT%H"),
        )
    return f"scheduled:{schedule.id}:{schedule.job_type}:{due_at.isoformat()}"


def _next_run_after(schedule: ScheduledJob, due_at):
    interval = max(60, int(schedule.interval_seconds or 60))
    next_run = due_at + timedelta(seconds=interval)
    now = timezone.now()
    while next_run <= now:
        next_run += timedelta(seconds=interval)
    return next_run


def enqueue_due_scheduled_jobs(*, now=None, limit: int = 100) -> list[int]:
    now = now or timezone.now()
    enqueued_job_ids: list[int] = []
    due_ids = list(
        ScheduledJob.objects.filter(enabled=True, next_run_at__lte=now)
        .order_by("next_run_at", "id")
        .values_list("id", flat=True)[: max(1, int(limit))]
    )
    for schedule_id in due_ids:
        with transaction.atomic():
            schedule = ScheduledJob.objects.select_for_update().get(pk=schedule_id)
            if not schedule.enabled or schedule.next_run_at > now:
                continue
            if parse_str_enum(JobType, schedule.job_type) is None:
                schedule.enabled = False
                schedule.updated_at = now
                schedule.save(update_fields=["enabled", "updated_at"])
                continue
            due_at = schedule.next_run_at
            idempotency_key = _render_idempotency_key(schedule, due_at)
            payload: dict[str, Any] = dict(schedule.payload or {})
            payload.setdefault("scheduled_job_id", schedule.id)
            payload.setdefault("scheduled_due_at", due_at.isoformat())
            job = create_job(
                profile_id=schedule.profile_id,
                job_type=schedule.job_type,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            schedule.last_run_at = now
            schedule.next_run_at = _next_run_after(schedule, due_at)
            schedule.updated_at = now
            schedule.save(update_fields=["last_run_at", "next_run_at", "updated_at"])
        publish_job(
            {
                "job_id": job.id,
                "job_type": job.job_type,
                "profile_id": job.profile_id,
                "attempt": 1,
            }
        )
        enqueued_job_ids.append(job.id)
    return enqueued_job_ids
