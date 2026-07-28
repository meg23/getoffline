import json
from typing import Any

import pika
from django.conf import settings

from models.domain import JobType, MediaType, SourceType, parse_str_enum
from models.models import Job

from .routing import MAX_QUEUE_PRIORITY, queue_arguments, queue_name


def _as_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def job_priority(message: dict[str, Any]) -> int:
    """Return the RabbitMQ priority for a job message.

    Higher numbers are consumed first.  Priorities intentionally encode the
    product rules around freshness and user intent while leaving same-priority
    work FIFO within each durable RabbitMQ queue.
    """
    job_type = parse_str_enum(JobType, message.get("job_type"))
    raw_payload = message.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

    if job_type is JobType.DOWNLOAD_SINGLE:
        if _as_bool(payload.get("manual_enqueue")) or _as_bool(
            payload.get("redownload")
        ):
            return 10
        return 9
    if job_type is JobType.DOWNLOAD_EPISODE:
        if _as_bool(payload.get("redownload")):
            return 9
        return 5
    if job_type is JobType.GENERATE_TRANSCRIPT:
        if _as_bool(payload.get("startup_missing_subtitle")):
            return 2
        source_type = parse_str_enum(
            SourceType, payload.get("source_type") or payload.get("media_source_type")
        )
        media_type = parse_str_enum(MediaType, payload.get("media_type"))
        if source_type is SourceType.PODCAST or media_type is MediaType.AUDIO:
            return 8
        if payload.get("download_id"):
            return 7
        return 3
    return 0


def _message_with_payload(message: dict[str, Any]) -> dict[str, Any]:
    if isinstance(message.get("payload"), dict):
        return message
    job_id = message.get("job_id")
    if not job_id:
        return message
    try:
        payload = (
            Job.objects.filter(pk=int(job_id)).values_list("payload", flat=True).first()
        )
    except Exception:
        return message
    if not isinstance(payload, dict):
        return message
    enriched = dict(message)
    enriched["payload"] = payload
    return enriched


def publish_job(message: dict[str, Any]) -> None:
    message = _message_with_payload(message)
    job_type = str(message["job_type"])
    queue = queue_name(
        job_type,
        message.get("payload") if isinstance(message.get("payload"), dict) else None,
    )
    priority = max(
        0, min(MAX_QUEUE_PRIORITY, int(message.get("priority", job_priority(message))))
    )
    connection = pika.BlockingConnection(pika.URLParameters(settings.RABBITMQ_URL))
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
        channel.basic_publish(
            exchange=settings.RABBITMQ_EXCHANGE,
            routing_key=queue,
            body=json.dumps(
                {k: v for k, v in message.items() if k != "payload"}, sort_keys=True
            ).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json", delivery_mode=2, priority=priority
            ),
            mandatory=True,
        )
    finally:
        connection.close()
