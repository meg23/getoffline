from models.domain import JobType
from models.domain import MediaType
from models.domain import QueueName
from models.domain import SourceType
from models.domain import parse_str_enum

SERIAL_EPISODE_CHECK_QUEUE = QueueName.SERIAL_EPISODE_CHECK
YOUTUBE_DOWNLOAD_QUEUE = QueueName.YOUTUBE_DOWNLOAD
PODCAST_DOWNLOAD_QUEUE = QueueName.PODCAST_DOWNLOAD
TRANSCRIPT_QUEUE = QueueName.TRANSCRIPT
FFMPEG_QUEUE = QueueName.FFMPEG
TRANSFER_QUEUE = QueueName.TRANSFER
CLEANUP_QUEUE = QueueName.CLEANUP
MAX_QUEUE_PRIORITY = 10

PRIORITY_QUEUES = frozenset({
    YOUTUBE_DOWNLOAD_QUEUE,
    PODCAST_DOWNLOAD_QUEUE,
    TRANSCRIPT_QUEUE,
    FFMPEG_QUEUE,
})
SERIAL_JOB_TYPES = frozenset({JobType.CHECK_FOR_EPISODES, JobType.UPDATE_DOWNLOADS})
DOWNLOAD_JOB_TYPES = frozenset({JobType.DOWNLOAD_EPISODE, JobType.DOWNLOAD_SINGLE})


def queue_arguments(queue: str) -> dict:
    """RabbitMQ queue declaration options shared by publishers and consumers."""
    if queue in PRIORITY_QUEUES:
        return {"x-max-priority": MAX_QUEUE_PRIORITY}
    return {}


def _download_queue_name(payload: dict | None = None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    source_type = parse_str_enum(SourceType, payload.get("source_type"))
    media_type = parse_str_enum(MediaType, payload.get("media_type"))
    if source_type is SourceType.PODCAST:
        return PODCAST_DOWNLOAD_QUEUE
    if source_type is SourceType.YOUTUBE:
        return YOUTUBE_DOWNLOAD_QUEUE
    if media_type is MediaType.AUDIO:
        return PODCAST_DOWNLOAD_QUEUE
    # Manual URL downloads and payload-less download jobs default to the
    # YouTube-capable downloader; the legacy shared downloads queue is no longer
    # declared or consumed.
    return YOUTUBE_DOWNLOAD_QUEUE


def queue_name(job_type: str, payload: dict | None = None) -> str:
    parsed_job_type = parse_str_enum(JobType, job_type)
    if parsed_job_type in SERIAL_JOB_TYPES:
        return SERIAL_EPISODE_CHECK_QUEUE
    if parsed_job_type in DOWNLOAD_JOB_TYPES:
        return _download_queue_name(payload)
    if parsed_job_type is JobType.TRANSCODE_MEDIA:
        return FFMPEG_QUEUE
    if parsed_job_type is JobType.GENERATE_TRANSCRIPT:
        return TRANSCRIPT_QUEUE
    if parsed_job_type is JobType.TRANSFER_MEDIA:
        return TRANSFER_QUEUE
    if parsed_job_type is JobType.RETENTION_CLEANUP:
        return CLEANUP_QUEUE
    return f"getoffline.jobs.{job_type}"
