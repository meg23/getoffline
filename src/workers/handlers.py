from app.queue import publish_job
from models.jobs import create_job
from logger import get_logger
from models.models import Download, Job, SourceConfig


log = get_logger("workers.handlers")


def _publish_created_job(job: Job) -> None:
    log.info("Publishing child job job_id=%s job_type=%s profile_id=%s", job.id, job.job_type, job.profile_id)
    publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    log.info("Published child job job_id=%s job_type=%s profile_id=%s", job.id, job.job_type, job.profile_id)


def check_for_episodes(job: Job) -> None:
    """Serial discovery worker: find enabled sources and enqueue one download job per source."""
    profile_id = job.profile_id
    sources = list(SourceConfig.objects.filter(profile_id=profile_id, enabled=True).order_by("position", "id"))
    log.info("Episode check started job_id=%s profile_id=%s enabled_sources=%s", job.id, profile_id, len(sources))
    enqueued = 0
    for source in sources:
        log.info("Queueing source download job parent_job_id=%s source_id=%s source_type=%s source_name=%s", job.id, source.id, source.source_type, source.name)
        child = create_job(
            profile_id=profile_id,
            job_type="download_episode",
            payload={"source_id": source.id, "source_type": source.source_type, "source_name": source.name},
            idempotency_key=f"download_episode:{profile_id}:source:{source.id}",
        )
        _publish_created_job(child)
        enqueued += 1
    log.info("Episode check finished job_id=%s profile_id=%s enqueued_download_jobs=%s", job.id, profile_id, enqueued)


def update_downloads(job: Job) -> None:
    log.info("update_downloads routed to episode checker job_id=%s", job.id)
    check_for_episodes(job)


def download_episode(job: Job) -> None:
    """Serial downloader worker placeholder.

    The queue is intentionally single-consumer/prefetch=1 so episode downloads happen one at a time.
    After download code writes a Download row, enqueue transcript generation with that download_id.
    """
    log.info("Download worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    download_id = job.payload.get("download_id") if isinstance(job.payload, dict) else None
    if not download_id:
        log.info("Download worker has no download_id yet; actual downloader integration pending job_id=%s", job.id)
        return
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_transcript",
        payload={"download_id": download_id},
        idempotency_key=f"generate_transcript:{job.profile_id}:{download_id}",
    )
    _publish_created_job(child)
    log.info("Download worker queued transcript job parent_job_id=%s download_id=%s child_job_id=%s", job.id, download_id, child.id)


def download_single(job: Job) -> None:
    log.info("download_single routed to downloader job_id=%s", job.id)
    download_episode(job)


def generate_transcript(job: Job) -> None:
    """Parallel transcript worker placeholder; enqueue summary when transcript work is done."""
    log.info("Transcript worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    download_id = job.payload.get("download_id") if isinstance(job.payload, dict) else None
    if not download_id:
        log.warning("Transcript worker skipped job with no download_id job_id=%s", job.id)
        return
    if not Download.objects.filter(pk=download_id, profile_id=job.profile_id).exists():
        log.warning("Transcript worker skipped missing download job_id=%s download_id=%s profile_id=%s", job.id, download_id, job.profile_id)
        return
    child = create_job(
        profile_id=job.profile_id,
        job_type="generate_summary",
        payload={"download_id": download_id},
        idempotency_key=f"generate_summary:{job.profile_id}:{download_id}",
    )
    _publish_created_job(child)
    log.info("Transcript worker queued summary job parent_job_id=%s download_id=%s child_job_id=%s", job.id, download_id, child.id)


def generate_summary(job: Job) -> None:
    log.info("Summary worker started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    log.info("Summary worker finished placeholder job_id=%s", job.id)
    return None


def summarize_missing(job: Job) -> None:
    downloads = list(Download.objects.filter(profile_id=job.profile_id, summary__isnull=True).order_by("-last_seen_at")[:100])
    log.info("Summarize-missing fanout started job_id=%s profile_id=%s candidates=%s", job.id, job.profile_id, len(downloads))
    enqueued = 0
    for download in downloads:
        child = create_job(
            profile_id=job.profile_id,
            job_type="generate_summary",
            payload={"download_id": download.id},
            idempotency_key=f"generate_summary:{job.profile_id}:{download.id}",
        )
        _publish_created_job(child)
        enqueued += 1
    log.info("Summarize-missing fanout finished job_id=%s profile_id=%s enqueued_summary_jobs=%s", job.id, job.profile_id, enqueued)


def sync_media(job: Job) -> None:
    log.info("Sync worker placeholder started job_id=%s profile_id=%s payload=%s", job.id, job.profile_id, job.payload)
    log.info("Sync worker placeholder finished job_id=%s", job.id)
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
