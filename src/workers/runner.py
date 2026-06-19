import argparse
import json
import os
import signal
from typing import Dict

import django
import pika
from django.conf import settings
from django.db import close_old_connections

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
django.setup()

from app.routing import (  # noqa: E402
    SERIAL_DOWNLOAD_QUEUE,
    SERIAL_EPISODE_CHECK_QUEUE,
    SUMMARY_QUEUE,
    SYNC_QUEUE,
    TRANSCRIPT_QUEUE,
)
from models.jobs import claim_job, finish_job  # noqa: E402
from models.models import Job  # noqa: E402
from workers.handlers import HANDLERS  # noqa: E402

_STOP = False

QUEUE_BY_WORKER = {
    "episode-checker": SERIAL_EPISODE_CHECK_QUEUE,
    "downloader": SERIAL_DOWNLOAD_QUEUE,
    "transcripts": TRANSCRIPT_QUEUE,
    "summaries": SUMMARY_QUEUE,
    "sync": SYNC_QUEUE,
}

SERIAL_WORKERS = {"episode-checker", "downloader"}


def _handle_signal(_signum, _frame) -> None:
    global _STOP
    _STOP = True


def process_message(message: Dict) -> None:
    close_old_connections()
    job = claim_job(int(message["job_id"]))
    if job is None:
        return
    try:
        HANDLERS[job.job_type](job)
        finish_job(job, status=Job.STATUS_SUCCEEDED)
    except Exception as exc:
        finish_job(job, status=Job.STATUS_FAILED, error_message=str(exc))
        raise
    finally:
        close_old_connections()


def run_worker(worker_type: str, *, prefetch_count: int | None = None) -> None:
    queue = QUEUE_BY_WORKER[worker_type]
    safe_prefetch = 1 if worker_type in SERIAL_WORKERS else max(1, int(prefetch_count or 4))
    connection = pika.BlockingConnection(pika.URLParameters(settings.RABBITMQ_URL))
    try:
        channel = connection.channel()
        channel.exchange_declare(exchange=settings.RABBITMQ_EXCHANGE, exchange_type="direct", durable=True)
        channel.queue_declare(queue=queue, durable=True)
        channel.queue_bind(queue=queue, exchange=settings.RABBITMQ_EXCHANGE, routing_key=queue)
        channel.basic_qos(prefetch_count=safe_prefetch)
        for method_frame, _properties, body in channel.consume(queue, inactivity_timeout=1):
            if _STOP:
                break
            if method_frame is None:
                continue
            message = json.loads(body.decode("utf-8"))
            try:
                process_message(message)
            except Exception:
                channel.basic_nack(method_frame.delivery_tag, requeue=False)
            else:
                channel.basic_ack(method_frame.delivery_tag)
        channel.cancel()
    finally:
        connection.close()


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
