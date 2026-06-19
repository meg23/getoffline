import argparse
import json
import os
import signal
from typing import Dict

import django
import pika
from django.conf import settings
from django.db import close_old_connections
from workers.logger import get_logger

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
django.setup()

from app.routing import (  # noqa: E402
    SERIAL_DOWNLOAD_QUEUE,
    SERIAL_EPISODE_CHECK_QUEUE,
    SUMMARY_QUEUE,
    SYNC_QUEUE,
    TRANSCRIPT_QUEUE,
    FFMPEG_QUEUE,
)
from models.jobs import claim_job, finish_job  # noqa: E402
from models.models import Job  # noqa: E402
from workers.handlers import HANDLERS  # noqa: E402

log = get_logger("workers.runner")

_STOP = False

QUEUE_BY_WORKER = {
    "updates": SERIAL_EPISODE_CHECK_QUEUE,
    "downloader": SERIAL_DOWNLOAD_QUEUE,
    "ffmpeg": FFMPEG_QUEUE,
    "transcripts": TRANSCRIPT_QUEUE,
    "summaries": SUMMARY_QUEUE,
    "sync": SYNC_QUEUE,
}

JOB_TYPES_BY_WORKER = {
    "updates": {"check_for_episodes", "update_downloads"},
    "downloader": {"download_episode", "download_single"},
    "ffmpeg": {"transcode_media"},
    "transcripts": {"generate_transcript"},
    "summaries": {"generate_summary", "summarize_missing"},
    "sync": {"sync_media"},
}

SERIAL_WORKERS = {"updates", "downloader"}


def _handle_signal(signum, _frame) -> None:
    global _STOP
    log.info("Shutdown signal received signal=%s", signum)
    _STOP = True


def process_message(message: Dict) -> None:
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
        log.info("Job skipped because it was already claimed or no longer queued job_id=%s", job_id)
        return
    log.info("Job claimed job_id=%s job_type=%s profile_id=%s", job.id, job.job_type, job.profile_id)
    try:
        handler = HANDLERS[job.job_type]
        log.info("Job handler starting job_id=%s job_type=%s", job.id, job.job_type)
        handler(job)
        finish_job(job, status=Job.STATUS_SUCCEEDED)
        log.info("Job succeeded job_id=%s job_type=%s", job.id, job.job_type)
    except Exception as exc:
        finish_job(job, status=Job.STATUS_FAILED, error_message=str(exc))
        log.exception("Job failed job_id=%s job_type=%s error=%s", job.id, job.job_type, exc)
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
    rows = list(Job.objects.filter(status=Job.STATUS_QUEUED, job_type__in=job_types).order_by("created_at", "id")[:500])
    for job in rows:
        channel.basic_publish(
            exchange=settings.RABBITMQ_EXCHANGE,
            routing_key=queue,
            body=json.dumps({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1}, sort_keys=True).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
            mandatory=True,
        )
    return len(rows)


def run_worker(worker_type: str, *, prefetch_count: int | None = None) -> None:
    queue = QUEUE_BY_WORKER[worker_type]
    safe_prefetch = 1 if worker_type in SERIAL_WORKERS else max(1, int(prefetch_count or 4))
    log.info("Worker starting worker_type=%s queue=%s prefetch=%s serial=%s", worker_type, queue, safe_prefetch, worker_type in SERIAL_WORKERS)
    connection = pika.BlockingConnection(pika.URLParameters(settings.RABBITMQ_URL))
    log.info("RabbitMQ connected worker_type=%s queue=%s", worker_type, queue)
    try:
        channel = connection.channel()
        channel.exchange_declare(exchange=settings.RABBITMQ_EXCHANGE, exchange_type="direct", durable=True)
        channel.queue_declare(queue=queue, durable=True)
        channel.queue_bind(queue=queue, exchange=settings.RABBITMQ_EXCHANGE, routing_key=queue)
        channel.basic_qos(prefetch_count=safe_prefetch)
        requeued = requeue_existing_jobs(channel, worker_type)
        if requeued:
            log.info("Worker requeued existing DB jobs worker_type=%s queue=%s count=%s", worker_type, queue, requeued)
        log.info("Worker consuming worker_type=%s queue=%s exchange=%s prefetch=%s", worker_type, queue, settings.RABBITMQ_EXCHANGE, safe_prefetch)
        for method_frame, _properties, body in channel.consume(queue, inactivity_timeout=1):
            if _STOP:
                log.info("Worker stop requested worker_type=%s queue=%s", worker_type, queue)
                break
            if method_frame is None:
                continue
            message = json.loads(body.decode("utf-8"))
            try:
                process_message(message)
            except Exception:
                channel.basic_nack(method_frame.delivery_tag, requeue=False)
                log.warning("Message nacked worker_type=%s queue=%s delivery_tag=%s", worker_type, queue, method_frame.delivery_tag)
            else:
                channel.basic_ack(method_frame.delivery_tag)
                log.info("Message acked worker_type=%s queue=%s delivery_tag=%s", worker_type, queue, method_frame.delivery_tag)
        channel.cancel()
        log.info("Worker consumer cancelled worker_type=%s queue=%s", worker_type, queue)
    finally:
        connection.close()
        log.info("RabbitMQ connection closed worker_type=%s queue=%s", worker_type, queue)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a GetOffline worker")
    parser.add_argument("worker_type", choices=sorted(QUEUE_BY_WORKER), help="Which queue to consume")
    parser.add_argument("--prefetch", type=int, default=None, help="Prefetch for parallel workers; serial workers always use 1")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    run_worker(args.worker_type, prefetch_count=args.prefetch)


if __name__ == "__main__":
    main()
