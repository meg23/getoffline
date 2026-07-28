import argparse
import json
import os
import signal
import time
from dataclasses import dataclass
from typing import Any

import django
import pika
from django.conf import settings
from django.db import close_old_connections
from pika import exceptions as pika_exceptions

from models.domain import JobStatus, JobType, MediaType, SourceType, parse_str_enum
from workers.logger import get_logger

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
django.setup()

from frontend.queue import job_priority
from frontend.routing import (
    CLEANUP_QUEUE,
    FFMPEG_QUEUE,
    PODCAST_DOWNLOAD_QUEUE,
    SERIAL_EPISODE_CHECK_QUEUE,
    TRANSCRIPT_QUEUE,
    YOUTUBE_DOWNLOAD_QUEUE,
    queue_arguments,
    queue_name,
)
from models.jobs import (
    claim_job,
    finish_job,
)
from models.models import Job
from workers.handlers import HANDLERS
from workers.scheduler import (
    HEAVY_JOB_TYPES,
    scheduler_from_settings,
)

log = get_logger("workers.runner")

_STOP = False
_SCHEDULER = None

QUEUE_BY_WORKER = {
    "updates": SERIAL_EPISODE_CHECK_QUEUE,
    "downloader-youtube": YOUTUBE_DOWNLOAD_QUEUE,
    "downloader-podcast": PODCAST_DOWNLOAD_QUEUE,
    "transcripts": TRANSCRIPT_QUEUE,
    "ffmpeg": FFMPEG_QUEUE,
    "cleanup": CLEANUP_QUEUE,
}

JOB_TYPES_BY_WORKER = {
    "updates": {JobType.CHECK_FOR_EPISODES, JobType.UPDATE_DOWNLOADS},
    "downloader-youtube": {JobType.DOWNLOAD_EPISODE, JobType.DOWNLOAD_SINGLE},
    "downloader-podcast": {JobType.DOWNLOAD_EPISODE, JobType.DOWNLOAD_SINGLE},
    "ffmpeg": {JobType.TRANSCODE_MEDIA, JobType.CENSOR_PROFANITY},
    "transcripts": {JobType.GENERATE_TRANSCRIPT},
    "cleanup": {JobType.RETENTION_CLEANUP},
}

SERIAL_WORKERS = {"updates", "downloader-youtube"}
DOWNLOADER_WORKERS = {"downloader-youtube", "downloader-podcast"}


@dataclass(frozen=True)
class WorkerConfig:
    worker_type: str
    queue: str
    prefetch_count: int
    max_messages: int


@dataclass(frozen=True)
class QueuedJobMessage:
    job_id: int
    job_type: str | None = None
    profile_id: str | None = None
    attempt: int | None = None

    @classmethod
    def from_body(cls, body: bytes) -> "QueuedJobMessage":
        payload = json.loads(body.decode("utf-8"))
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "QueuedJobMessage":
        return cls(
            job_id=int(payload["job_id"]),
            job_type=payload.get("job_type"),
            profile_id=payload.get("profile_id"),
            attempt=payload.get("attempt"),
        )


def worker_rabbitmq_parameters():
    """Return RabbitMQ connection parameters safe for long-running jobs."""
    params = pika.URLParameters(settings.RABBITMQ_URL)
    if "heartbeat=" not in settings.RABBITMQ_URL.lower():
        params.heartbeat = 0
    return params


def build_worker_config(
    worker_type: str,
    *,
    prefetch_count: int | None = None,
    max_messages: int | None = None,
) -> WorkerConfig:
    return WorkerConfig(
        worker_type=worker_type,
        queue=QUEUE_BY_WORKER[worker_type],
        prefetch_count=safe_prefetch_count(worker_type, prefetch_count),
        max_messages=safe_max_messages(max_messages),
    )


def safe_prefetch_count(worker_type: str, requested_prefetch: int | None) -> int:
    if worker_type in SERIAL_WORKERS:
        return 1
    return max(1, int(requested_prefetch or 4))


def safe_max_messages(requested_max_messages: int | None) -> int:
    configured_max_messages = os.getenv("GETOFFLINE_WORKER_MAX_MESSAGES", "1")
    return max(
        1,
        int(
            requested_max_messages
            if requested_max_messages is not None
            else configured_max_messages
        ),
    )


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


def _scheduler():
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = scheduler_from_settings()
    return _SCHEDULER


def process_message(message: dict) -> None:
    process_queued_job_message(QueuedJobMessage.from_payload(message))


def process_queued_job_message(message: QueuedJobMessage) -> None:
    close_old_connections()
    log_received_message(message)
    job = claim_queued_job(message.job_id)
    if job is None:
        return
    try:
        run_claimed_job(job)
        mark_job_succeeded(job)
    except Exception as exc:
        mark_job_failed(job, exc)
        raise
    finally:
        close_old_connections()


def log_received_message(message: QueuedJobMessage) -> None:
    log.info(
        "Message received job_id=%s job_type=%s profile_id=%s attempt=%s",
        message.job_id,
        message.job_type,
        message.profile_id,
        message.attempt,
    )


def claim_queued_job(job_id: int) -> Job | None:
    job = claim_job(job_id)
    if job is None:
        log.info(
            "Job skipped because it was already claimed or no longer queued job_id=%s",
            job_id,
        )
        return None
    log.info(
        "Job claimed job_id=%s job_type=%s profile_id=%s",
        job.id,
        job.job_type,
        job.profile_id,
    )
    return job


def run_claimed_job(job: Job) -> None:
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
        return
    handler(job)


def mark_job_succeeded(job: Job) -> None:
    finish_job(job, status=JobStatus.SUCCEEDED)
    emit_update_finished_message(job, status=JobStatus.SUCCEEDED)
    log.info("Job succeeded job_id=%s job_type=%s", job.id, job.job_type)


def mark_job_failed(job: Job, exc: Exception) -> None:
    error_message = str(exc)
    finish_job(job, status=JobStatus.FAILED, error_message=error_message)
    emit_update_finished_message(
        job, status=JobStatus.FAILED, error_message=error_message
    )
    log.exception(
        "Job failed job_id=%s job_type=%s error=%s", job.id, job.job_type, exc
    )


def emit_update_finished_message(
    job: Job, *, status: str, error_message: str = ""
) -> None:
    if parse_str_enum(JobType, job.job_type) is not JobType.UPDATE_DOWNLOADS:
        return
    completion_token = update_completion_token(job)
    if not completion_token:
        return
    create_update_finished_message_once(job, completion_token, status, error_message)


def update_completion_token(job: Job) -> str:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return str(payload.get("completion_token") or "").strip()


def create_update_finished_message_once(
    job: Job, completion_token: str, status: str, error_message: str
) -> None:
    idempotency_key = f"worker_message:update_downloads:{job.id}:{completion_token}"
    if Job.objects.filter(idempotency_key=idempotency_key).exists():
        return
    Job.objects.create(
        profile_id=job.profile_id,
        job_type="worker_message",
        status=JobStatus.SUCCEEDED,
        payload={
            "event_type": "update_downloads_finished",
            "completion_token": completion_token,
            "source_job_id": job.id,
            "source_status": status,
            "error_message": error_message,
        },
        idempotency_key=idempotency_key,
    )


# Backwards-compatible name for tests or external callers.
_emit_update_finished_message = emit_update_finished_message


def requeue_existing_jobs(channel, worker_type: str) -> int:
    """Publish queue messages for queued DB jobs that do not have a live broker message."""
    jobs = queued_jobs_for_worker(worker_type)
    return publish_requeued_jobs(channel, worker_type, jobs)


def queued_jobs_for_worker(worker_type: str) -> list[Job]:
    job_types = JOB_TYPES_BY_WORKER[worker_type]
    return list(
        Job.objects.filter(status=JobStatus.QUEUED, job_type__in=job_types).order_by(
            "created_at", "id"
        )[:500]
    )


def publish_requeued_jobs(channel, worker_type: str, jobs: list[Job]) -> int:
    published = 0
    for job in jobs:
        target_queue = queue_for_job(job)
        if worker_type_is_wrong_downloader(worker_type, target_queue):
            continue
        publish_requeued_job(channel, job, target_queue)
        published += 1
    return published


def queue_for_job(job: Job) -> str:
    payload = job.payload if isinstance(job.payload, dict) else None
    return queue_name(job.job_type, payload)


def worker_type_is_wrong_downloader(worker_type: str, target_queue: str) -> bool:
    return (
        worker_type in DOWNLOADER_WORKERS
        and target_queue != QUEUE_BY_WORKER[worker_type]
    )


def publish_requeued_job(channel, job: Job, target_queue: str) -> None:
    message = {
        "job_id": job.id,
        "job_type": job.job_type,
        "profile_id": job.profile_id,
        "attempt": 1,
        "payload": job.payload,
    }
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
    config = build_worker_config(
        worker_type, prefetch_count=prefetch_count, max_messages=max_messages
    )
    log_worker_starting(config)
    connection = pika.BlockingConnection(worker_rabbitmq_parameters())
    log.info(
        "RabbitMQ connected worker_type=%s queue=%s", config.worker_type, config.queue
    )
    try:
        channel = open_worker_channel(connection, config)
        requeue_jobs_if_enabled(channel, config)
        consume_worker_messages(channel, config)
        cancel_worker_channel(channel, config)
    finally:
        close_connection_if_open(connection)
        log.info(
            "RabbitMQ connection closed worker_type=%s queue=%s",
            config.worker_type,
            config.queue,
        )


def log_worker_starting(config: WorkerConfig) -> None:
    log.info(
        "Worker starting worker_type=%s queue=%s prefetch=%s serial=%s max_messages=%s",
        config.worker_type,
        config.queue,
        config.prefetch_count,
        config.worker_type in SERIAL_WORKERS,
        config.max_messages,
    )


def open_worker_channel(connection, config: WorkerConfig):
    channel = connection.channel()
    declare_worker_exchange(channel)
    declare_worker_queue(channel, config)
    channel.basic_qos(prefetch_count=config.prefetch_count)
    return channel


def declare_worker_exchange(channel) -> None:
    channel.exchange_declare(
        exchange=settings.RABBITMQ_EXCHANGE, exchange_type="direct", durable=True
    )


def declare_worker_queue(channel, config: WorkerConfig) -> None:
    channel.queue_declare(
        queue=config.queue,
        durable=True,
        arguments=queue_arguments(config.queue) or None,
    )
    channel.queue_bind(
        queue=config.queue,
        exchange=settings.RABBITMQ_EXCHANGE,
        routing_key=config.queue,
    )


def requeue_jobs_if_enabled(channel, config: WorkerConfig) -> None:
    if not requeue_existing_jobs_enabled():
        return
    requeued = requeue_existing_jobs(channel, config.worker_type)
    log.info(
        "Worker requeued existing jobs worker_type=%s queue=%s count=%s",
        config.worker_type,
        config.queue,
        requeued,
    )


def consume_worker_messages(channel, config: WorkerConfig) -> None:
    log_worker_consuming(config)
    processed_messages = 0
    for method_frame, _properties, body in channel.consume(
        config.queue, inactivity_timeout=1
    ):
        if worker_should_stop():
            log.info(
                "Worker stop requested worker_type=%s queue=%s",
                config.worker_type,
                config.queue,
            )
            break
        if method_frame is None:
            continue
        processed_messages += handle_delivery(channel, config, method_frame, body)
        if processed_messages >= config.max_messages:
            log.info(
                "Worker processed max messages; exiting for container restart worker_type=%s queue=%s processed_messages=%s max_messages=%s",
                config.worker_type,
                config.queue,
                processed_messages,
                config.max_messages,
            )
            break


def log_worker_consuming(config: WorkerConfig) -> None:
    log.info(
        "Worker consuming worker_type=%s queue=%s exchange=%s prefetch=%s",
        config.worker_type,
        config.queue,
        settings.RABBITMQ_EXCHANGE,
        config.prefetch_count,
    )


def worker_should_stop() -> bool:
    return _STOP


def handle_delivery(channel, config: WorkerConfig, method_frame, body: bytes) -> int:
    message = QueuedJobMessage.from_body(body)
    if worker_should_requeue_message(config.worker_type, message):
        requeue_message_for_matching_worker(channel, config, method_frame, message)
        return 0
    return process_delivery(channel, config, method_frame, message)


def worker_should_requeue_message(worker_type: str, message: QueuedJobMessage) -> bool:
    return not worker_accepts_job(worker_type, message.job_id)


def worker_accepts_job(worker_type: str, job_id: int) -> bool:
    if worker_type not in DOWNLOADER_WORKERS:
        return True
    job = Job.objects.filter(pk=job_id).only("payload", "job_type").first()
    if job is None:
        return True
    source_type = source_type_for_downloader_job(job)
    allowed = (
        SourceType.YOUTUBE
        if worker_type == "downloader-youtube"
        else SourceType.PODCAST
    )
    return source_type is allowed


# Backwards-compatible name for tests or external callers.
_worker_accepts_job = worker_accepts_job


def source_type_for_downloader_job(job: Job) -> SourceType:
    payload = job.payload if isinstance(job.payload, dict) else {}
    source_type = parse_str_enum(SourceType, payload.get("source_type"))
    if source_type is not None:
        return source_type
    media_type = parse_str_enum(MediaType, payload.get("media_type"))
    return SourceType.PODCAST if media_type is MediaType.AUDIO else SourceType.YOUTUBE


def requeue_message_for_matching_worker(
    channel, config: WorkerConfig, method_frame, message: QueuedJobMessage
) -> None:
    channel.basic_nack(method_frame.delivery_tag, requeue=True)
    log.info(
        "Message requeued for matching downloader worker worker_type=%s queue=%s job_id=%s",
        config.worker_type,
        config.queue,
        message.job_id,
    )
    time.sleep(0.25)


def process_delivery(
    channel, config: WorkerConfig, method_frame, message: QueuedJobMessage
) -> int:
    try:
        process_queued_job_message(message)
    except Exception:  # noqa: BLE001
        nack_failed_delivery(channel, config, method_frame)
    else:
        ack_completed_delivery(channel, config, method_frame)
    return 1


def nack_failed_delivery(channel, config: WorkerConfig, method_frame) -> None:
    channel.basic_nack(method_frame.delivery_tag, requeue=False)
    log.warning(
        "Message nacked worker_type=%s queue=%s delivery_tag=%s",
        config.worker_type,
        config.queue,
        method_frame.delivery_tag,
    )


def ack_completed_delivery(channel, config: WorkerConfig, method_frame) -> None:
    channel.basic_ack(method_frame.delivery_tag)
    log.info(
        "Message acked worker_type=%s queue=%s delivery_tag=%s",
        config.worker_type,
        config.queue,
        method_frame.delivery_tag,
    )


def cancel_worker_channel(channel, config: WorkerConfig) -> None:
    channel.cancel()
    log.info(
        "Worker consumer cancelled worker_type=%s queue=%s",
        config.worker_type,
        config.queue,
    )


def main() -> None:
    args = parse_args()
    install_signal_handlers()
    run_worker(
        args.worker_type, prefetch_count=args.prefetch, max_messages=args.max_messages
    )


def parse_args() -> argparse.Namespace:
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
    return parser.parse_args()


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


if __name__ == "__main__":
    main()
