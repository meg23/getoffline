import json
from typing import Any, Dict

import pika
from django.conf import settings


from .routing import queue_name


def publish_job(message: Dict[str, Any]) -> None:
    job_type = str(message["job_type"])
    queue = queue_name(job_type)
    connection = pika.BlockingConnection(pika.URLParameters(settings.RABBITMQ_URL))
    try:
        channel = connection.channel()
        channel.exchange_declare(exchange=settings.RABBITMQ_EXCHANGE, exchange_type="direct", durable=True)
        channel.queue_declare(queue=queue, durable=True)
        channel.queue_bind(queue=queue, exchange=settings.RABBITMQ_EXCHANGE, routing_key=queue)
        channel.basic_publish(
            exchange=settings.RABBITMQ_EXCHANGE,
            routing_key=queue,
            body=json.dumps(message, sort_keys=True).encode("utf-8"),
            properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
            mandatory=True,
        )
    finally:
        connection.close()
