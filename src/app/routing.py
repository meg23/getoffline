SERIAL_EPISODE_CHECK_QUEUE = "getoffline.jobs.updates"
SERIAL_DOWNLOAD_QUEUE = "getoffline.jobs.downloads"
TRANSCRIPT_QUEUE = "getoffline.transcripts"
SUMMARY_QUEUE = "getoffline.summaries"
SYNC_QUEUE = "getoffline.sync_media"
FFMPEG_QUEUE = "getoffline.ffmpeg"


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
    return f"getoffline.{job_type}"
