from .domain import JobStatus
from typing import Any

from django.db import transaction
from django.utils import timezone

from .models import Job


def create_job(
    *,
    profile_id: str,
    job_type: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> Job:
    if idempotency_key:
        existing = Job.objects.filter(
            idempotency_key=idempotency_key,
            status__in=[JobStatus.QUEUED, JobStatus.RUNNING],
        ).first()
        if existing is not None:
            return existing
    return Job.objects.create(
        profile_id=profile_id,
        job_type=job_type,
        payload=payload or {},
        idempotency_key=idempotency_key,
        status=JobStatus.QUEUED,
    )


@transaction.atomic
def claim_job(job_id: int) -> Job | None:
    job = (
        Job.objects.select_for_update()
        .filter(pk=int(job_id), status=JobStatus.QUEUED)
        .first()
    )
    if job is None:
        return None
    now = timezone.now()
    job.status = JobStatus.RUNNING
    job.started_at = now
    job.updated_at = now
    job.save(update_fields=["status", "started_at", "updated_at"])
    return job


def finish_job(job: Job, *, status: str, error_message: str | None = None) -> None:
    now = timezone.now()
    job.status = status
    job.error_message = error_message
    job.finished_at = now
    job.updated_at = now
    job.save(update_fields=["status", "error_message", "finished_at", "updated_at"])
