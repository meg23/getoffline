from enum import StrEnum


class SourceType(StrEnum):
    YOUTUBE = "youtube"
    PODCAST = "podcast"


class MediaType(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"


class DownloadStatus(StrEnum):
    DOWNLOADED = "downloaded"
    FILTERED = "filtered"
    MISSING = "missing"
    RETENTION_DELETED = "retention_deleted"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobType(StrEnum):
    CHECK_FOR_EPISODES = "check_for_episodes"
    UPDATE_DOWNLOADS = "update_downloads"
    DOWNLOAD_EPISODE = "download_episode"
    DOWNLOAD_SINGLE = "download_single"
    TRANSCODE_MEDIA = "transcode_media"
    GENERATE_TRANSCRIPT = "generate_transcript"
    RETENTION_CLEANUP = "retention_cleanup"


class QueueName(StrEnum):
    SERIAL_EPISODE_CHECK = "getoffline.jobs.updates"
    YOUTUBE_DOWNLOAD = "getoffline.jobs.downloads.youtube"
    PODCAST_DOWNLOAD = "getoffline.jobs.downloads.podcast"
    TRANSCRIPT = "getoffline.jobs.transcripts"
    FFMPEG = "getoffline.jobs.ffmpeg"
    CLEANUP = "getoffline.jobs.cleanup"


class CpuSlotStatus(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"


class HeavyJobKind(StrEnum):
    FFMPEG = "ffmpeg"
    TRANSCRIPT = "transcript"


class ProfanityStatus(StrEnum):
    CLEAN = "clean"
    UNCENSORED = "uncensored"
    CENSORED = "censored"


def parse_str_enum(enum_type: type[StrEnum], value: object) -> StrEnum | None:
    try:
        return enum_type(str(value or "").strip().lower())
    except ValueError:
        return None
