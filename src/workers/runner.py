import argparse
import json
import os
import signal
import time

import django
import pika
from django.conf import settings
from django.db import close_old_connections
from pika import exceptions as pika_exceptions

from workers.logger import get_logger

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
django.setup()

from app.queue import job_priority  # noqa: E402
from app.routing import CLEANUP_QUEUE  # noqa: E402
from app.routing import FFMPEG_QUEUE
from app.routing import PODCAST_DOWNLOAD_QUEUE
from app.routing import SERIAL_EPISODE_CHECK_QUEUE
from app.routing import TRANSCRIPT_QUEUE
from app.routing import TRANSFER_QUEUE
from app.routing import YOUTUBE_DOWNLOAD_QUEUE
from app.routing import queue_arguments
from app.routing import queue_name
from models.jobs import claim_job  # noqa: E402
from models.jobs import finish_job
from models.models import Download  # noqa: E402
from models.models import Job
from models.models import SourceConfig
from workers.handlers import HANDLERS  # noqa: E402
from workers.scheduler import HEAVY_JOB_TYPES  # noqa: E402
from workers.scheduler import scheduler_from_settings

log = get_logger("workers.runner")

_STOP = False
_SCHEDULER = None

QUEUE_BY_WORKER = {
    "updates": SERIAL_EPISODE_CHECK_QUEUE,
    "downloader-youtube": YOUTUBE_DOWNLOAD_QUEUE,
    "downloader-podcast": PODCAST_DOWNLOAD_QUEUE,
    "transcripts": TRANSCRIPT_QUEUE,
    "ffmpeg": FFMPEG_QUEUE,
    "transfer": TRANSFER_QUEUE,
    "cleanup": CLEANUP_QUEUE,
}

JOB_TYPES_BY_WORKER = {
    "updates": {"check_for_episodes", "update_downloads"},
    "downloader-youtube": {"download_episode", "download_single"},
    "downloader-podcast": {"download_episode", "download_single"},
    "ffmpeg": {"transcode_media"},
    "transcripts": {"generate_transcript"},
    "transfer": {"transfer_media"},
    "cleanup": {"retention_cleanup"},
}

SERIAL_WORKERS = {"updates", "downloader-youtube"}


def worker_rabbitmq_parameters():
    """Return RabbitMQ connection parameters safe for long-running jobs.

    BlockingConnection only services RabbitMQ heartbeats while control returns to
    pika. Transcript generation can legitimately run longer than RabbitMQ's
    default heartbeat interval, so worker consumer connections disable heartbeat
    checks unless the URL explicitly opts into a heartbeat query parameter.
    """
    params = pika.URLParameters(settings.RABBITMQ_URL)
    if "heartbeat=" not in settings.RABBITMQ_URL.lower():
        params.heartbeat = 0
    return params


def close_connection_if_open(connection) -> None:
    if getattr(connection, "is_closed", False):
        return
    try:
        connection.close()
    except pika_exceptions.ConnectionWrongStateError:
        log.warning("RabbitMQ connection already closed before worker shutdown")


def _handle_signal(signum, _frame) -> None:
    global _STOP
    log.info("Shutdown signal received signal=%s", signum)
    _STOP = True


def _worker_accepts_job(worker_type: str, job_id: int) -> bool:
    if worker_type not in {"downloader-youtube", "downloader-podcast"}:
        return True
    job = Job.objects.filter(pk=job_id).only("payload", "job_type").first()
    if job is None:
        return True
    payload = job.payload if isinstance(job.payload, dict) else {}
    source_type = (
        str(
            payload.get("source_type")
            or ("podcast" if payload.get("media_type") == "audio" else "youtube")
        )
        .strip()
        .lower()
    )
    allowed = "youtube" if worker_type == "downloader-youtube" else "podcast"
    return source_type == allowed


def _emit_update_finished_message(
    job: Job, *, status: str, error_message: str = ""
) -> None:
    if job.job_type != "update_downloads":
        return
    payload = job.payload if isinstance(job.payload, dict) else {}
    completion_token = str(payload.get("completion_token") or "").strip()
    if not completion_token:
        return
    idempotency_key = f"worker_message:update_downloads:{job.id}:{completion_token}"
    if Job.objects.filter(idempotency_key=idempotency_key).exists():
        return
    Job.objects.create(
        profile_id=job.profile_id,
        job_type="worker_message",
        status=Job.STATUS_SUCCEEDED,
        payload={
            "event_type": "update_downloads_finished",
            "completion_token": completion_token,
            "source_job_id": job.id,
            "source_status": status,
            "error_message": error_message,
        },
        idempotency_key=idempotency_key,
    )


def _scheduler():
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = scheduler_from_settings()
    return _SCHEDULER


def process_message(message: dict) -> None:
    close_old_connections()
    job_id = int(message["job_id"])
    log.info(
        "Message received job_id=%s job_type=%s profile_id=%s attempt=%s",
        job_id,
        message.get("job_type"),
        message.get("profile_id"),
        message.get("attempt"),
    )
    job = claim_job(job_id)
    if job is None:
        log.info(
            "Job skipped because it was already claimed or no longer queued job_id=%s",
            job_id,
        )
        return
    log.info(
        "Job claimed job_id=%s job_type=%s profile_id=%s",
        job.id,
        job.job_type,
        job.profile_id,
    )
    try:
        handler = HANDLERS[job.job_type]
        scheduler_job_type = HEAVY_JOB_TYPES.get(job.job_type)
        log.info(
            "Job handler starting job_id=%s job_type=%s scheduler_job_type=%s",
            job.id,
            job.job_type,
            scheduler_job_type,
        )
        if scheduler_job_type:
            _scheduler().run(scheduler_job_type, handler, job)
        else:
            handler(job)
        finish_job(job, status=Job.STATUS_SUCCEEDED)
        _emit_update_finished_message(job, status=Job.STATUS_SUCCEEDED)
        log.info("Job succeeded job_id=%s job_type=%s", job.id, job.job_type)
    except Exception as exc:
        finish_job(job, status=Job.STATUS_FAILED, error_message=str(exc))
        _emit_update_finished_message(
            job, status=Job.STATUS_FAILED, error_message=str(exc)
        )
        log.exception(
            "Job failed job_id=%s job_type=%s error=%s", job.id, job.job_type, exc
        )
        raise
    finally:
        close_old_connections()


def requeue_existing_jobs(channel, worker_type: str) -> int:
    """Publish queue messages for queued DB jobs that do not have a live broker message.

    The database is the durable source of truth for job state. If a worker queue is
    introduced after jobs were created, RabbitMQ data is reset, or a publish is
    interrupted after the DB row is committed, this startup pass makes the queue
    self-healing. Duplicate messages are safe because ``claim_job`` only allows
    one consumer to transition a queued job to running.
    """
    job_types = JOB_TYPES_BY_WORKER[worker_type]
    queue = QUEUE_BY_WORKER[worker_type]
    rows = list(
        Job.objects.filter(status=Job.STATUS_QUEUED, job_type__in=job_types).order_by(
            "created_at", "id"
        )[:500]
    )
    published = 0
    for job in rows:
        message = {
            "job_id": job.id,
            "job_type": job.job_type,
            "profile_id": job.profile_id,
            "attempt": 1,
            "payload": job.payload,
        }
        target_queue = queue_name(
            job.job_type, job.payload if isinstance(job.payload, dict) else None
        )
        if (
            worker_type in {"downloader-youtube", "downloader-podcast"}
            and target_queue != queue
        ):
            continue
        channel.basic_publish(
            exchange=settings.RABBITMQ_EXCHANGE,
            routing_key=target_queue,
            body=json.dumps(
                {k: v for k, v in message.items() if k != "payload"}, sort_keys=True
            ).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                priority=job_priority(message),
            ),
            mandatory=True,
        )
        published += 1
    return published


def _source_subtitle_settings(download: Download) -> tuple[bool, float | None]:
    source = (
        SourceConfig.objects.filter(
            profile_id=download.profile_id,
            source_type=download.source_type,
            name=download.source_name,
        )
        .only("subtitles", "subtitle_offset_seconds")
        .first()
    )
    if source is None:
        return True, None
    return bool(source.subtitles), source.subtitle_offset_seconds


def requeue_existing_jobs_enabled() -> bool:
    return str(os.getenv("GETOFFLINE_REQUEUE_EXISTING_JOBS", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def run_worker(
    worker_type: str,
    *,
    prefetch_count: int | None = None,
    max_messages: int | None = None,
) -> None:
    queue = QUEUE_BY_WORKER[worker_type]
    safe_prefetch = (
        1 if worker_type in SERIAL_WORKERS else max(1, int(prefetch_count or 4))
    )
    safe_max_messages = max(
        1,
        int(
            max_messages
            if max_messages is not None
            else os.getenv("GETOFFLINE_WORKER_MAX_MESSAGES", "1")
        ),
    )
    processed_messages = 0
    log.info(
        "Worker starting worker_type=%s queue=%s prefetch=%s serial=%s max_messages=%s",
        worker_type,
        queue,
        safe_prefetch,
        worker_type in SERIAL_WORKERS,
        safe_max_messages,
    )
    connection = pika.BlockingConnection(worker_rabbitmq_parameters())
    log.info("RabbitMQ connected worker_type=%s queue=%s", worker_type, queue)
    try:
        channel = connection.channel()
        channel.exchange_declare(
            exchange=settings.RABBITMQ_EXCHANGE, exchange_type="direct", durable=True
        )
        channel.queue_declare(
            queue=queue, durable=True, arguments=queue_arguments(queue) or None
        )
        channel.queue_bind(
            queue=queue, exchange=settings.RABBITMQ_EXCHANGE, routing_key=queue
        )
        channel.basic_qos(prefetch_count=safe_prefetch)
        if requeue_existing_jobs_enabled():
            requeued = requeue_existing_jobs(channel, worker_type)
            log.info(
                "Worker requeued existing jobs worker_type=%s queue=%s count=%s",
                worker_type,
                queue,
                requeued,
            )
        log.info(
            "Worker consuming worker_type=%s queue=%s exchange=%s prefetch=%s",
            worker_type,
            queue,
            settings.RABBITMQ_EXCHANGE,
            safe_prefetch,
        )
        for method_frame, _properties, body in channel.consume(
            queue, inactivity_timeout=1
        ):
            if _STOP:
                log.info(
                    "Worker stop requested worker_type=%s queue=%s", worker_type, queue
                )
                break
            if method_frame is None:
                continue
            message = json.loads(body.decode("utf-8"))
            job_id = int(message.get("job_id", 0))
            if not _worker_accepts_job(worker_type, job_id):
                channel.basic_nack(method_frame.delivery_tag, requeue=True)
                log.info(
                    "Message requeued for matching downloader worker worker_type=%s queue=%s job_id=%s",
                    worker_type,
                    queue,
                    job_id,
                )
                time.sleep(0.25)
                continue
            try:
                process_message(message)
            except Exception:
                channel.basic_nack(method_frame.delivery_tag, requeue=False)
                processed_messages += 1
                log.warning(
                    "Message nacked worker_type=%s queue=%s delivery_tag=%s processed_messages=%s",
                    worker_type,
                    queue,
                    method_frame.delivery_tag,
                    processed_messages,
                )
            else:
                channel.basic_ack(method_frame.delivery_tag)
                processed_messages += 1
                log.info(
                    "Message acked worker_type=%s queue=%s delivery_tag=%s processed_messages=%s",
                    worker_type,
                    queue,
                    method_frame.delivery_tag,
                    processed_messages,
                )
            if processed_messages >= safe_max_messages:
                log.info(
                    "Worker processed max messages; exiting for container restart worker_type=%s queue=%s processed_messages=%s max_messages=%s",
                    worker_type,
                    queue,
                    processed_messages,
                    safe_max_messages,
                )
                break
        channel.cancel()
        log.info(
            "Worker consumer cancelled worker_type=%s queue=%s", worker_type, queue
        )
    finally:
        close_connection_if_open(connection)
        log.info(
            "RabbitMQ connection closed worker_type=%s queue=%s", worker_type, queue
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a GetOffline worker")
    parser.add_argument(
        "worker_type", choices=sorted(QUEUE_BY_WORKER), help="Which queue to consume"
    )
    parser.add_argument(
        "--prefetch",
        type=int,
        default=None,
        help="Prefetch for parallel workers; serial workers always use 1",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=None,
        help="Exit after processing this many messages; defaults to GETOFFLINE_WORKER_MAX_MESSAGES or 1",
    )
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    run_worker(
        args.worker_type, prefetch_count=args.prefetch, max_messages=args.max_messages
    )


if __name__ == "__main__":
    main()
