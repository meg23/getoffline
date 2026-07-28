"""Library service for reusable episode/library queries and DTOs."""

from __future__ import annotations

from pathlib import Path

from django.db.models import QuerySet, Sum
from django.urls import reverse

from models.domain import DownloadStatus, parse_str_enum, ProfanityStatus
from models.models import Download, Job
from shared.schemas.media import EpisodeSummary

DOWNLOAD_STATUSES = [
    DownloadStatus.DOWNLOADED,
    DownloadStatus.MISSING,
    DownloadStatus.RETENTION_DELETED,
]
LIBRARY_PREVIEW_LIMIT = 100


def human_size(size: int | None) -> str:
    if not size:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} GB"


def human_duration(seconds: float | int | None) -> str:
    total = int(float(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def decorate_download(item: Download) -> Download:
    position = float(item.last_position_seconds or 0.0)
    item.display_size = human_size(item.file_size_bytes)
    item.display_type = (
        item.file_ext or Path(str(item.file_path or "")).suffix.lstrip(".") or "?"
    ).upper()
    item.display_kind = (
        "video"
        if item.display_type.lower() in {"mp4", "mkv", "webm", "mov"}
        else "audio"
    )
    item.status_label = "UNPLAYED"
    item.status_class = "status-unplayed"
    if position > 0 and not item.played:
        item.status_label = "STARTED"
        item.status_class = "status-started"
    if item.played:
        item.status_label = "PLAYED"
        item.status_class = "status-played"
    download_status = parse_str_enum(DownloadStatus, item.download_status)
    if download_status in {DownloadStatus.MISSING, DownloadStatus.RETENTION_DELETED}:
        item.status_label = (
            "REMOVED"
            if download_status is DownloadStatus.RETENTION_DELETED
            else "MISSING"
        )
        item.status_class = "status-missing"
    item.resolved_subtitle_path = None
    item.has_subtitles = bool(item.subtitle_path or item.subtitle_path_relative)
    profanity_status = parse_str_enum(ProfanityStatus, item.profanity_status or "clean")
    item.profanity_label = ""
    item.profanity_class = ""
    if profanity_status is ProfanityStatus.CENSORED:
        item.profanity_label = "CENSORED"
        item.profanity_class = "profanity-censored"
    elif profanity_status is ProfanityStatus.UNCENSORED:
        item.profanity_label = "EXPLICIT"
        item.profanity_class = "profanity-explicit"
    return item


def library_download_query(profile_id: str) -> QuerySet[Download, Download]:
    return Download.objects.filter(
        profile_id=profile_id, download_status__in=DOWNLOAD_STATUSES
    ).only(
        "id",
        "profile_id",
        "source_type",
        "source_name",
        "title",
        "description",
        "file_path",
        "file_path_relative",
        "file_ext",
        "file_size_bytes",
        "subtitle_path",
        "subtitle_path_relative",
        "download_status",
        "last_seen_at",
        "played",
        "favorite",
        "last_position_seconds",
        "total_listened_seconds",
        "duration_seconds",
    )


def normalize_library_filter(value: object) -> str:
    mode = str(value or "unplayed").strip().lower()
    return mode if mode in {"all", "played", "favorites", "unplayed"} else "unplayed"


def list_downloads(
    profile_id: str, *, filter_mode: str = "unplayed", show_all: bool | None = None
) -> list[Download]:
    mode = "all" if show_all is True else normalize_library_filter(filter_mode)
    rows = library_download_query(profile_id)
    if mode == "unplayed":
        rows = rows.filter(played=False, download_status=DownloadStatus.DOWNLOADED)
    elif mode == "played":
        rows = rows.filter(played=True, download_status=DownloadStatus.DOWNLOADED)
    elif mode == "favorites":
        rows = rows.filter(favorite=True, download_status=DownloadStatus.DOWNLOADED)
    rows = rows.order_by("-last_seen_at", "-id")
    if mode != "all":
        rows = rows[:LIBRARY_PREVIEW_LIMIT]
    return [decorate_download(item) for item in rows]


def library_filter_counts(profile_id: str) -> dict[str, int]:
    rows = library_download_query(profile_id)
    downloaded = rows.filter(download_status=DownloadStatus.DOWNLOADED)
    return {
        "all": rows.count(),
        "downloaded": downloaded.count(),
        "unplayed": downloaded.filter(played=False).count(),
        "played": downloaded.filter(played=True).count(),
        "favorites": downloaded.filter(favorite=True).count(),
        "missing": rows.filter(download_status=DownloadStatus.MISSING).count(),
        "retention_deleted": rows.filter(
            download_status=DownloadStatus.RETENTION_DELETED
        ).count(),
    }


def listened_seconds(profile_id: str) -> float:
    return (
        library_download_query(profile_id)
        .aggregate(total=Sum("total_listened_seconds"))
        .get("total")
        or 0
    )


def recent_jobs(profile_id: str) -> list[Job]:
    return list(
        Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:10]
    )


def episode_to_summary(item: Download) -> dict[str, object]:
    dto = EpisodeSummary(
        id=item.id,
        title=item.title or "Untitled",
        source_name=item.source_name or item.source_type or "",
        source_type=item.source_type or "",
        description=item.description or "",
        duration_seconds=float(item.duration_seconds)
        if item.duration_seconds is not None
        else None,
        played=bool(item.played),
        favorite=bool(item.favorite),
        last_position_seconds=float(item.last_position_seconds or 0.0),
        total_listened_seconds=float(item.total_listened_seconds or 0.0),
        download_status=str(item.download_status or ""),
        media_url=reverse("media", args=[item.id]),
        stream_url=reverse("api_stream", args=[item.id]),
        subtitles_url=reverse("subtitle", args=[item.id])
        if item.subtitle_path or item.subtitle_path_relative
        else None,
    )
    return dto.to_dict()
