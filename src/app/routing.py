SERIAL_EPISODE_CHECK_QUEUE = "getoffline.jobs.updates"
SERIAL_DOWNLOAD_QUEUE = "getoffline.jobs.downloads"
YOUTUBE_DOWNLOAD_QUEUE = "getoffline.jobs.downloads.youtube"
PODCAST_DOWNLOAD_QUEUE = "getoffline.jobs.downloads.podcast"
TRANSCRIPT_QUEUE = "getoffline.jobs.transcripts"
SUMMARY_QUEUE = "getoffline.jobs.summaries"
SYNC_QUEUE = "getoffline.jobs.sync_media"
FFMPEG_QUEUE = "getoffline.jobs.ffmpeg"
CLEANUP_QUEUE = "getoffline.jobs.cleanup"
MAX_QUEUE_PRIORITY = 10


def queue_arguments(queue: str) -> dict:
    """RabbitMQ queue declaration options shared by publishers and consumers."""
    if queue in {SERIAL_DOWNLOAD_QUEUE, YOUTUBE_DOWNLOAD_QUEUE, PODCAST_DOWNLOAD_QUEUE, TRANSCRIPT_QUEUE, FFMPEG_QUEUE}:
        return {"x-max-priority": MAX_QUEUE_PRIORITY}
    return {}


def _download_queue_name(payload: dict | None = None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    source_type = str(payload.get("source_type") or "").strip().lower()
    media_type = str(payload.get("media_type") or "").strip().lower()
    if source_type == "podcast":
        return PODCAST_DOWNLOAD_QUEUE
    if source_type == "youtube":
        return YOUTUBE_DOWNLOAD_QUEUE
    if media_type == "audio":
        return PODCAST_DOWNLOAD_QUEUE
    # Manual URL downloads default to the YouTube-capable downloader. Keeping the
    # legacy shared queue as the no-payload fallback preserves old tests and any
    # generic downloader deployments that call queue_name(job_type) directly.
    if payload:
        return YOUTUBE_DOWNLOAD_QUEUE
    return SERIAL_DOWNLOAD_QUEUE


def queue_name(job_type: str, payload: dict | None = None) -> str:
    if job_type in {"check_for_episodes", "update_downloads"}:
        return SERIAL_EPISODE_CHECK_QUEUE
    if job_type in {"download_episode", "download_single"}:
        return _download_queue_name(payload)
    if job_type == "transcode_media":
        return FFMPEG_QUEUE
    if job_type == "generate_transcript":
        return TRANSCRIPT_QUEUE
    if job_type in {"generate_summary", "summarize_missing"}:
        return SUMMARY_QUEUE
    if job_type == "retention_cleanup":
        return CLEANUP_QUEUE
    return f"getoffline.jobs.{job_type}"
