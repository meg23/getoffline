from app.queue import publish_job
from models.jobs import create_job
from models.models import Download, Job, SourceConfig


def _publish_created_job(job: Job) -> None:
    publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})


def check_for_episodes(job: Job) -> None:
    """Serial discovery worker: find enabled sources and enqueue one download job per source."""
    profile_id = job.profile_id
    sources = SourceConfig.objects.filter(profile_id=profile_id, enabled=True).order_by("position", "id")
    for source in sources:
        child = create_job(
            profile_id=profile_id,
            job_type="download_episode",
            payload={"source_id": source.id, "source_type": source.source_type, "source_name": source.name},
            idempotency_key=f"download_episode:{profile_id}:source:{source.id}",
        )
        _publish_created_job(child)


def update_downloads(job: Job) -> None:
    check_for_episodes(job)


def download_episode(job: Job) -> None:
    """Serial downloader worker placeholder.

    The queue is intentionally single-consumer/prefetch=1 so episode downloads happen one at a time.
    After download code writes a Download row, enqueue transcript generation with that download_id.
    """
    download_id = job.payload.get("download_id") if isinstance(job.payload, dict) else None
    if not download_id:
        return
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_transcript",
        payload={"download_id": download_id},
        idempotency_key=f"generate_transcript:{job.profile_id}:{download_id}",
    )
    _publish_created_job(child)


def download_single(job: Job) -> None:
    download_episode(job)


def generate_transcript(job: Job) -> None:
    """Parallel transcript worker placeholder; enqueue summary when transcript work is done."""
    download_id = job.payload.get("download_id") if isinstance(job.payload, dict) else None
    if not download_id or not Download.objects.filter(pk=download_id, profile_id=job.profile_id).exists():
        return
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_summary",
        payload={"download_id": download_id},
        idempotency_key=f"generate_summary:{job.profile_id}:{download_id}",
    )
    _publish_created_job(child)


def generate_summary(job: Job) -> None:
    return None


def summarize_missing(job: Job) -> None:
    downloads = Download.objects.filter(profile_id=job.profile_id, summary__isnull=True).order_by("-last_seen_at")[:100]
    for download in downloads:
        child = create_job(
            profile_id=job.profile_id,
            job_type="generate_summary",
            payload={"download_id": download.id},
            idempotency_key=f"generate_summary:{job.profile_id}:{download.id}",
        )
        _publish_created_job(child)


def sync_media(job: Job) -> None:
    return None


HANDLERS = {
    "check_for_episodes": check_for_episodes,
    "update_downloads": update_downloads,
    "download_episode": download_episode,
    "download_single": download_single,
    "generate_transcript": generate_transcript,
    "generate_summary": generate_summary,
    "summarize_missing": summarize_missing,
    "sync_media": sync_media,
}
