SERIAL_EPISODE_CHECK_QUEUE = "getoffline.jobs.updates"
SERIAL_DOWNLOAD_QUEUE = "getoffline.jobs.downloads"
TRANSCRIPT_QUEUE = "getoffline.jobs.transcripts"
SUMMARY_QUEUE = "getoffline.jobs.summaries"
SYNC_QUEUE = "getoffline.jobs.sync_media"
FFMPEG_QUEUE = "getoffline.jobs.ffmpeg"
CLEANUP_QUEUE = "getoffline.jobs.cleanup"
MAX_QUEUE_PRIORITY = 10


def queue_arguments(queue: str) -> dict:
    """RabbitMQ queue declaration options shared by publishers and consumers."""
    if queue in {SERIAL_DOWNLOAD_QUEUE, TRANSCRIPT_QUEUE, FFMPEG_QUEUE}:
        return {"x-max-priority": MAX_QUEUE_PRIORITY}
    return {}


def queue_name(job_type: str) -> str:
    if job_type in {"check_for_episodes", "update_downloads"}:
        return SERIAL_EPISODE_CHECK_QUEUE
    if job_type in {"download_episode", "download_single"}:
        return SERIAL_DOWNLOAD_QUEUE
    if job_type == "transcode_media":
        return FFMPEG_QUEUE
    if job_type == "generate_transcript":
        return TRANSCRIPT_QUEUE
    if job_type in {"generate_summary", "summarize_missing"}:
        return SUMMARY_QUEUE
    if job_type == "retention_cleanup":
        return CLEANUP_QUEUE
    return f"getoffline.jobs.{job_type}"
