# mypy: ignore-errors
"""Dashboard/settings action services used by the API layer.

This module owns the database, filesystem, and queue-touching operations that
used to live in the browser-facing app views. The frontend calls API endpoints;
API controllers delegate stateful work here.
"""

import hashlib
import logging
import mimetypes
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.models import Count
from django.db.models import Sum
from django.http import FileResponse
from django.http import Http404
from django.http import HttpRequest
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseRedirect
from django.http import JsonResponse
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from models.domain import DownloadStatus
from models.domain import JobStatus
from models.domain import JobType
from models.domain import SourceType
from models.domain import parse_str_enum
from models.jobs import create_job
from models.models import AppConfigValue
from models.models import Download
from models.models import DownloadSettings
from models.models import Job
from models.models import ProfileConfigValue
from models.models import ProfileDownloadSettings
from models.models import ScheduledJob
from models.models import SourceConfig
from models.models import TranscriptSegment

from app.queue import publish_job
from app.routing import PODCAST_DOWNLOAD_QUEUE
from app.routing import SERIAL_EPISODE_CHECK_QUEUE
from app.routing import TRANSCRIPT_QUEUE
from app.routing import TRANSFER_QUEUE
from app.routing import YOUTUBE_DOWNLOAD_QUEUE
from app.routing import queue_name

ALLOWED_JOB_TYPES = frozenset(
    {
        JobType.UPDATE_DOWNLOADS,
        JobType.DOWNLOAD_SINGLE,
        JobType.TRANSFER_MEDIA,
    }
)
DOWNLOAD_STATUSES = [
    DownloadStatus.DOWNLOADED,
    DownloadStatus.MISSING,
    DownloadStatus.RETENTION_DELETED,
]
LIBRARY_PREVIEW_LIMIT = 100
MEDIA_UPLOAD_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".wav",
    ".flac",
    ".aac",
    ".ogg",
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
}
VIDEO_UPLOAD_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}


@dataclass(frozen=True)
class JobStage:
    name: str
    label: str

    def __iter__(self):
        yield self.name
        yield self.label


@dataclass(frozen=True)
class ManualUploadResult:
    download: Download
    path: Path

    def __iter__(self):
        yield self.download
        yield self.path


@dataclass(frozen=True)
class LibraryPageData:
    downloads: list[Download]
    jobs: list[Job]
    profile_id: str
    profile_name: str
    show_all_downloads: bool
    listened_seconds: float


@dataclass(frozen=True)
class PlaybackUpdate:
    position: float
    reason: str
    completed: bool
    listened_delta: float


@dataclass(frozen=True)
class SourceFormData:
    name: str
    url: str
    media_type: str | None
    enabled: bool
    subtitles: bool
    subtitle_offset_seconds: float | None
    max_downloads: int | None
    delete_explicit_content: bool
    include_shorts: bool
    include_livestreams: bool
    title_exclude: str


log = logging.getLogger(__name__)
MEDIA_RANGE_CHUNK_SIZE = 64 * 1024


def _optional_int(value: object) -> int | None:
    raw = str(value or "").strip()
    return int(raw) if raw.isdigit() else None


def _optional_float(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _human_size(size: int | None) -> str:
    if not size:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} GB"


def _human_duration(seconds: float | int | None) -> str:
    total = int(float(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _decorate_download(item: Download) -> Download:
    position = float(item.last_position_seconds or 0.0)
    item.display_size = _human_size(item.file_size_bytes)
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

    # Important performance fix:
    # Do not call _resolve_subtitle_path() here. That function checks the
    # filesystem and calls _profile_output_root(), so doing it for every row
    # makes the library page slow when many downloads are listed.
    #
    # The actual /subtitle/<id>/ endpoint still does the real filesystem
    # validation when the player requests subtitles.
    item.resolved_subtitle_path = None
    item.has_subtitles = bool(item.subtitle_path or item.subtitle_path_relative)
    return item


def _profile_id(request: HttpRequest) -> str:
    """Return the storage partition for the signed-in user.

    The database still stores a profile_id column for compatibility with workers and
    existing data, but the web app no longer exposes profile switching. Each login
    user owns one implicit partition named after their username.
    """
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        return str(user.get_username() or "default")
    return "default"


def _redirect_back(
    request: HttpRequest, fallback: str = "library"
) -> HttpResponseRedirect:
    return HttpResponseRedirect(
        request.POST.get("next") or request.headers.get("Referer") or reverse(fallback)
    )


def _safe_path(raw_path: str | None) -> Path:
    if not raw_path:
        raise Http404("File unavailable")
    path = Path(raw_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise Http404("File unavailable")
    return path


def _profile_output_root(profile_id: str) -> Path:
    value = (
        ProfileConfigValue.objects.filter(profile_id=profile_id, key="output_root")
        .values_list("value", flat=True)
        .first()
        or AppConfigValue.objects.filter(key="output_root")
        .values_list("value", flat=True)
        .first()
        or PROFILE_DEFAULTS["output_root"]
    )
    return Path(str(value)).expanduser().absolute()


def _resolve_media_path(item: Download) -> Path:
    candidates: list[Path] = []
    if item.file_path_relative:
        candidates.append(
            _profile_output_root(item.profile_id) / str(item.file_path_relative)
        )
    if item.file_path:
        candidates.append(Path(str(item.file_path)))
    for candidate in candidates:
        try:
            return _safe_path(str(candidate))
        except Http404:
            continue
    raise Http404("File unavailable")


def _srt_to_vtt(content: str) -> str:
    lines = content.replace("\ufeff", "").splitlines()
    timestamp_re = re.compile(
        r"^(\d{2}:\d{2}:\d{2}),(\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}),(\d{3})(.*)$"
    )
    out_lines = ["WEBVTT", ""]
    for line in lines:
        match = timestamp_re.match(line)
        if match:
            start_time, start_ms, end_time, end_ms, tail = match.groups()
            out_lines.append(f"{start_time}.{start_ms} --> {end_time}.{end_ms}{tail}")
        elif line.strip().isdigit():
            continue
        else:
            out_lines.append(line)
    return "\n".join(out_lines).strip() + "\n"


def _resolve_subtitle_path(item: Download) -> Path | None:
    media_path = _resolve_media_path(item)
    candidates: list[Path] = []
    if item.subtitle_path:
        candidates.append(Path(str(item.subtitle_path)))
    if item.subtitle_path_relative:
        candidates.append(
            _profile_output_root(item.profile_id) / str(item.subtitle_path_relative)
        )
    candidates.extend([media_path.with_suffix(".srt"), media_path.with_suffix(".vtt")])

    root = _profile_output_root(item.profile_id)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_file() and resolved.suffix.lower() in {".srt", ".vtt"}:
            return resolved
    return None


def _supersede_active_transcript_job(profile_id: str, download_id: int) -> None:
    """Clear active transcript jobs before an explicit manual regeneration."""
    now = timezone.now()
    Job.objects.filter(
        profile_id=profile_id,
        job_type="generate_transcript",
        idempotency_key=f"generate_transcript:{profile_id}:{download_id}",
        status__in=[JobStatus.QUEUED, JobStatus.RUNNING],
    ).update(
        status=JobStatus.FAILED,
        error_message="Superseded by a manual transcript regeneration request.",
        finished_at=now,
        updated_at=now,
    )


def _library_download_query(profile_id: str):
    return (
        Download.objects.filter(
            profile_id=profile_id, download_status__in=DOWNLOAD_STATUSES
        )
        # Keep the library query narrow. Avoid pulling raw yt-dlp metadata JSON.
        .only(
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
        )
    )


def _library_filter_requested(request: HttpRequest) -> bool:
    return request.GET.get("filter") == "all"


def _library_visible_downloads(
    downloads_qs, show_all_downloads: bool
) -> list[Download]:
    ordered_downloads = downloads_qs.order_by("-last_seen_at", "-id")
    if not show_all_downloads:
        ordered_downloads = ordered_downloads[:LIBRARY_PREVIEW_LIMIT]
    return [_decorate_download(item) for item in ordered_downloads]


def _library_listened_seconds(downloads_qs) -> float:
    return downloads_qs.aggregate(total=Sum("total_listened_seconds")).get("total") or 0


def _recent_jobs(profile_id: str) -> list[Job]:
    return list(
        Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:10]
    )


def _library_page_data(request: HttpRequest) -> LibraryPageData:
    profile_id = _profile_id(request)
    downloads_qs = _library_download_query(profile_id)
    show_all_downloads = _library_filter_requested(request)
    downloads = _library_visible_downloads(downloads_qs, show_all_downloads)
    return LibraryPageData(
        downloads=downloads,
        jobs=_recent_jobs(profile_id),
        profile_id=profile_id,
        profile_name=request.user.get_username() or profile_id,
        show_all_downloads=show_all_downloads,
        listened_seconds=_library_listened_seconds(downloads_qs),
    )


def _library_stats(page: LibraryPageData) -> dict[str, object]:
    played_count = sum(1 for item in page.downloads if item.played)
    return {
        "visible": len(page.downloads),
        "played": played_count,
        "new": max(len(page.downloads) - played_count, 0),
        "favorites": sum(1 for item in page.downloads if item.favorite),
        "listened": _human_duration(page.listened_seconds),
    }


def _library_context(page: LibraryPageData) -> dict[str, object]:
    return {
        "downloads": page.downloads,
        "jobs": page.jobs,
        "profile_id": page.profile_id,
        "profile_name": page.profile_name,
        "profile_initial": (page.profile_name[:1] or "U").upper(),
        "library_filter_mode": "all" if page.show_all_downloads else "unplayed",
        "stats": _library_stats(page),
    }


def _log_library_render(
    profile_id: str, visible_downloads: int, elapsed: float
) -> None:
    log.info(
        "Library rendered profile_id=%s total=%.3fs visible_downloads=%s",
        profile_id,
        elapsed,
        visible_downloads,
    )


@login_required
def _legacy_library(request: HttpRequest) -> HttpResponse:
    started_at = time.perf_counter()
    page = _library_page_data(request)
    response = render(request, "app/library.html", _library_context(page))
    _log_library_render(
        page.profile_id, len(page.downloads), time.perf_counter() - started_at
    )
    return response


def _job_display_title(job: Job) -> str:
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_id = payload.get("download_id")
    if download_id:
        title = (
            Download.objects.filter(pk=download_id, profile_id=job.profile_id)
            .values_list("title", flat=True)
            .first()
        )
        if title:
            return str(title)
    for key in ("active_title", "title", "episode_title", "item_title", "url"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return job.job_type.replace("_", " ").title()


def _job_stage(job: Job) -> JobStage:
    payload = job.payload if isinstance(job.payload, dict) else {}
    active_stage = str(payload.get("active_stage") or "").strip()
    if active_stage == "downloading":
        return JobStage("downloading", "Downloading")
    if active_stage == "transcript_generation":
        return JobStage("transcript_generation", "Transcript generation")
    if job.job_type in {"download_episode", "download_single"}:
        return JobStage("downloading", "Downloading")
    if job.job_type in {"generate_transcript", "transcode_media"}:
        return JobStage("transcript_generation", "Transcript generation")
    return JobStage("queued", job.job_type.replace("_", " ").title())


def _active_pipeline_cutoff():
    raw_timeout = str(
        os.getenv("GETOFFLINE_ACTIVE_PIPELINE_STALE_SECONDS", "3600")
    ).strip()
    if not raw_timeout.isdigit():
        return None
    timeout_seconds = int(raw_timeout)
    if timeout_seconds <= 0:
        return None
    return timezone.now() - timedelta(seconds=timeout_seconds)


def _job_is_fresh(job: Job) -> bool:
    cutoff = _active_pipeline_cutoff()
    if cutoff is None:
        return True
    heartbeat_at = job.updated_at or job.started_at or job.created_at
    return bool(heartbeat_at and heartbeat_at >= cutoff)


def _job_still_needs_work(job: Job) -> bool:
    payload = job.payload if isinstance(job.payload, dict) else {}
    download_id = _optional_int(payload.get("download_id"))
    if not download_id:
        return True

    download = Download.objects.filter(
        pk=download_id, profile_id=job.profile_id
    ).first()
    if download is None:
        return job.job_type in {"download_episode", "download_single"}
    if job.job_type in {"generate_transcript", "transcode_media"}:
        has_transcript = TranscriptSegment.objects.filter(
            download_id=download_id
        ).exists()
        has_subtitle_file = bool(
            download.subtitle_path or download.subtitle_path_relative
        )
        return not (has_transcript or has_subtitle_file)
    if job.job_type in {"download_episode", "download_single"}:
        return (
            parse_str_enum(DownloadStatus, download.download_status)
            is not DownloadStatus.DOWNLOADED
        )
    return True


def _active_pipeline_items(profile_id: str) -> list[dict[str, object]]:
    jobs = list(
        Job.objects.filter(
            profile_id=profile_id,
            job_type__in=[
                "download_episode",
                "download_single",
                "generate_transcript",
                "transcode_media",
            ],
            status=JobStatus.RUNNING,
        ).order_by("started_at", "created_at", "id")[:40]
    )
    active_jobs = [
        job for job in jobs if _job_is_fresh(job) and _job_still_needs_work(job)
    ][:20]
    return [
        {
            "id": job.id,
            "title": _job_display_title(job),
            "status": job.status,
            "stage": _job_stage(job).name,
            "stage_label": _job_stage(job).label,
            "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        }
        for job in active_jobs
    ]


@login_required
def active_pipeline_status(request: HttpRequest) -> JsonResponse:
    items = _active_pipeline_items(_profile_id(request))
    return JsonResponse({"ok": True, "items": items, "active": bool(items)})


@login_required
def _legacy_jobs(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    rows = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[
        :100
    ]
    return render(request, "app/jobs.html", {"jobs": rows, "profile_id": profile_id})


@login_required
def _legacy_player(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    item.resolved_subtitle_path = (
        _resolve_subtitle_path(item)
        if parse_str_enum(DownloadStatus, item.download_status)
        is DownloadStatus.DOWNLOADED
        else None
    )
    item.has_subtitles = item.resolved_subtitle_path is not None
    try:
        requested_seek = float(request.GET.get("t") or 0.0)
    except (TypeError, ValueError):
        requested_seek = 0.0
    seek = max(float(item.last_position_seconds or 0.0), requested_seek)
    media_kind = (
        "video"
        if (item.file_ext or Path(str(item.file_path or "")).suffix.lstrip(".")).lower()
        in {"mp4", "mkv", "webm", "mov"}
        else "audio"
    )
    log.info(
        "player render download_id=%s media_kind=%s saved_position=%.3f requested_seek=%.3f rendered_seek=%.3f played=%s",
        item.id,
        media_kind,
        float(item.last_position_seconds or 0.0),
        requested_seek,
        seek,
        item.played,
    )
    return render(
        request,
        "app/player.html",
        {"item": item, "seek_seconds": seek, "media_kind": media_kind},
    )


def _file_range_iterator(path: Path, start: int, length: int):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(MEDIA_RANGE_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@login_required
def _legacy_media(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    path = _resolve_media_path(item)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_size = path.stat().st_size
    range_header = request.headers.get("Range", "")
    if range_header.startswith("bytes="):
        start_text, _, end_text = range_header.removeprefix("bytes=").partition("-")
        try:
            if start_text:
                start = int(start_text)
                requested_end = int(end_text) if end_text else None
            else:
                suffix_length = int(end_text) if end_text else file_size
                start = max(file_size - suffix_length, 0)
                requested_end = file_size - 1
        except ValueError:
            start, requested_end = 0, None
        start = max(0, min(start, file_size - 1))
        end = (
            file_size - 1
            if requested_end is None
            else max(start, min(requested_end, file_size - 1))
        )
        length = end - start + 1
        response = StreamingHttpResponse(
            _file_range_iterator(path, start, length),
            status=206,
            content_type=content_type,
        )
        response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        response["Content-Length"] = str(length)
    else:
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)
    response["Accept-Ranges"] = "bytes"
    return response


@login_required
def _legacy_subtitle(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    path = _resolve_subtitle_path(item)
    if path is None:
        raise Http404("Subtitle unavailable")
    subtitle_text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".srt":
        subtitle_text = _srt_to_vtt(subtitle_text)
    elif not subtitle_text.lstrip().startswith("WEBVTT"):
        subtitle_text = "WEBVTT\n\n" + subtitle_text
    return HttpResponse(subtitle_text, content_type="text/vtt; charset=utf-8")


PROFILE_DEFAULTS = {
    "output_root": "./downloads/default",
    "processing_workers": "2",
    "auto_update_minutes": "20",
    "auto_delete_content_days": "0",
    "manual_upload_delete_explicit_content": "0",
    "audio_format": "mp3",
    "video_format": "mp4",
    "video_codec": "h264",
    "ffmpeg_path": "ffmpeg",
    "audio_quality": "0",
    "ffmpeg_audio_filter": "loudnorm=I=-14:TP=-1.5:LRA=11",
    "ytdlp_video_max_height": "720",
    "max_downloads": "3",
    "js_runtime_path": "qjs",
    "android_sync_enabled": "0",
    "android_sync_max_items": "10",
    "android_sync_destination": "/sdcard/Movies/GetOffline",
    "android_sync_adb_path": "adb",
    "android_sync_connection_mode": "usb",
    "android_sync_wifi_address": "",
    "android_sync_include_subtitles": "1",
    "android_sync_include_unplayed": "1",
    "android_sync_include_started": "1",
    "android_sync_include_played": "0",
    "android_sync_exclude_regex": "",
}


def _profile_settings(profile_id: str) -> dict[str, str]:
    values = dict(PROFILE_DEFAULTS)
    values["output_root"] = f"./downloads/{profile_id}"
    values.update(
        {row.key: row.value for row in AppConfigValue.objects.order_by("key")}
    )
    values.update(
        {
            row.key: row.value
            for row in ProfileConfigValue.objects.filter(profile_id=profile_id)
        }
    )
    return values


def _checked(settings: dict[str, str], key: str) -> bool:
    return str(settings.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}


def _posted_bool(request: HttpRequest, key: str, default: str = "") -> bool:
    return request.POST.get(key, default) in {"1", "true", "yes", "on"}


def _source_form_data(
    request: HttpRequest, source_type: SourceType | str, prefix: str = ""
) -> SourceFormData:
    parsed_source_type = parse_str_enum(SourceType, source_type)
    is_youtube = parsed_source_type is SourceType.YOUTUBE
    return SourceFormData(
        name=str(request.POST.get(prefix + "name") or "").strip(),
        url=str(request.POST.get(prefix + "url") or "").strip(),
        media_type=(
            str(request.POST.get(prefix + "media_type") or "audio").strip().lower()
            if is_youtube
            else None
        ),
        enabled=_posted_bool(request, prefix + "enabled", "1"),
        subtitles=_posted_bool(request, prefix + "subtitles", "1"),
        subtitle_offset_seconds=_optional_float(
            request.POST.get(prefix + "subtitle_offset_seconds")
        ),
        max_downloads=_optional_int(request.POST.get(prefix + "max_downloads")),
        delete_explicit_content=_posted_bool(
            request, prefix + "delete_explicit_content"
        ),
        include_shorts=is_youtube and _posted_bool(request, prefix + "include_shorts"),
        include_livestreams=is_youtube
        and _posted_bool(request, prefix + "include_livestreams"),
        title_exclude=str(request.POST.get(prefix + "title_exclude") or "").strip(),
    )


def _source_update_fields(*, include_enabled: bool = True) -> list[str]:
    fields = [
        "name",
        "url",
        "media_type",
        "enabled",
        "subtitles",
        "subtitle_offset_seconds",
        "max_downloads",
        "delete_explicit_content",
        "title_exclude",
        "include_shorts",
        "include_livestreams",
        "updated_at",
    ]
    if not include_enabled:
        fields.remove("enabled")
    return fields


def _apply_source_form_data(
    source: SourceConfig, form: SourceFormData, *, now, include_enabled: bool = True
) -> SourceConfig:
    source.name = form.name or source.name
    source.url = form.url or source.url
    if parse_str_enum(SourceType, source.source_type) is SourceType.YOUTUBE:
        source.media_type = form.media_type or source.media_type or "audio"
        source.include_shorts = form.include_shorts
        source.include_livestreams = form.include_livestreams
    if include_enabled:
        source.enabled = form.enabled
    source.subtitles = form.subtitles
    source.subtitle_offset_seconds = form.subtitle_offset_seconds
    source.max_downloads = form.max_downloads
    source.delete_explicit_content = form.delete_explicit_content
    source.title_exclude = form.title_exclude
    source.updated_at = now
    return source


def _sync_update_downloads_schedule(
    profile_id: str, raw_minutes: object, *, now=None
) -> None:
    """Keep the automatic update scheduler in sync with the settings page."""
    now = now or timezone.now()
    try:
        minutes = int(str(raw_minutes or "").strip())
    except (TypeError, ValueError):
        return

    schedule = ScheduledJob.objects.filter(
        profile_id=profile_id, job_type="update_downloads"
    ).first()
    if minutes <= 0:
        if schedule is not None and schedule.enabled:
            schedule.enabled = False
            schedule.updated_at = now
            schedule.save(update_fields=["enabled", "updated_at"])
        return

    interval_seconds = max(60, minutes * 60)
    next_run_at = now + timedelta(seconds=interval_seconds)
    defaults = {
        "enabled": True,
        "interval_seconds": interval_seconds,
        "payload": {"source": "scheduler"},
        "idempotency_key_template": "scheduled:update_downloads:${profile_id}:${due_hour}",
        "updated_at": now,
    }
    if schedule is None:
        ScheduledJob.objects.create(
            profile_id=profile_id,
            job_type="update_downloads",
            next_run_at=next_run_at,
            **defaults,
        )
        return

    update_fields = [
        "enabled",
        "interval_seconds",
        "payload",
        "idempotency_key_template",
        "updated_at",
    ]
    old_interval_seconds = schedule.interval_seconds
    schedule.enabled = True
    schedule.interval_seconds = interval_seconds
    schedule.payload = {"source": "scheduler"}
    schedule.idempotency_key_template = (
        "scheduled:update_downloads:${profile_id}:${due_hour}"
    )
    if schedule.next_run_at <= now or old_interval_seconds != interval_seconds:
        schedule.next_run_at = next_run_at
        update_fields.append("next_run_at")
    schedule.updated_at = now
    schedule.save(update_fields=update_fields)


def _queue_counts(profile_id: str) -> list[dict[str, object]]:
    queue_labels = {
        SERIAL_EPISODE_CHECK_QUEUE: "Updates",
        YOUTUBE_DOWNLOAD_QUEUE: "YouTube downloads",
        PODCAST_DOWNLOAD_QUEUE: "Podcast downloads",
        TRANSCRIPT_QUEUE: "Transcripts",
        TRANSFER_QUEUE: "Transfer",
    }
    counts = {
        queue: {JobStatus.QUEUED: 0, JobStatus.RUNNING: 0} for queue in queue_labels
    }
    rows = (
        Job.objects.filter(
            profile_id=profile_id, status__in=[JobStatus.QUEUED, JobStatus.RUNNING]
        )
        .values("job_type", "status", "payload")
        .annotate(total=Count("id"))
    )
    for row in rows:
        queue = queue_name(
            str(row["job_type"]),
            row.get("payload") if isinstance(row.get("payload"), dict) else None,
        )
        counts.setdefault(queue, {JobStatus.QUEUED: 0, JobStatus.RUNNING: 0})
        counts[queue][str(row["status"])] = int(row["total"] or 0)
        queue_labels.setdefault(queue, queue.removeprefix("getoffline."))
    return [
        {
            "queue": queue,
            "label": queue_labels[queue],
            "queued": values[JobStatus.QUEUED],
            "running": values[JobStatus.RUNNING],
            "total": values[JobStatus.QUEUED] + values[JobStatus.RUNNING],
        }
        for queue, values in sorted(
            counts.items(), key=lambda item: queue_labels[item[0]].lower()
        )
    ]


@login_required
def settings_page(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    settings = _profile_settings(profile_id)
    sources = SourceConfig.objects.filter(profile_id=profile_id).order_by(
        "source_type", "position", "id"
    )
    download_settings = ProfileDownloadSettings.objects.filter(
        profile_id=profile_id
    ).first()
    if download_settings is None and profile_id == "default":
        legacy = DownloadSettings.objects.filter(pk=1).first()
        if legacy is not None:
            download_settings = ProfileDownloadSettings(
                profile_id=profile_id, youtube_cookie_text=legacy.youtube_cookie_text
            )
    profile_name = request.user.get_username() or profile_id
    return render(
        request,
        "app/settings.html",
        {
            "settings": settings,
            "sources": sources,
            "youtube_sources": sources.filter(source_type=SourceType.YOUTUBE),
            "podcast_sources": sources.filter(source_type=SourceType.PODCAST),
            "download_settings": download_settings,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "profile_initial": (profile_name[:1] or "U").upper(),
            "manual_upload_filter_checked": _checked(
                settings, "manual_upload_delete_explicit_content"
            ),
            "android_sync_enabled_checked": _checked(settings, "android_sync_enabled"),
            "android_sync_include_subtitles_checked": _checked(
                settings, "android_sync_include_subtitles"
            ),
            "android_sync_include_unplayed_checked": _checked(
                settings, "android_sync_include_unplayed"
            ),
            "android_sync_include_started_checked": _checked(
                settings, "android_sync_include_started"
            ),
            "android_sync_include_played_checked": _checked(
                settings, "android_sync_include_played"
            ),
            "queue_counts": _queue_counts(profile_id),
        },
    )


def _normalize_upload_stem(value: str) -> str:
    normalized = re.sub(r"\.{2,}", ".", str(value or "")).rstrip(". ")
    normalized = re.sub(r"[^A-Za-z0-9._ -]+", "-", normalized).strip(". -")
    return normalized or "manual-upload"


def _write_manual_upload(profile_id: str, uploaded_file) -> ManualUploadResult:
    original_name = Path(str(uploaded_file.name or "")).name
    if not original_name:
        raise ValueError("Missing filename")
    suffix = Path(original_name).suffix.lower()
    if suffix not in MEDIA_UPLOAD_EXTENSIONS:
        raise ValueError("Unsupported media type")

    output_root = _profile_output_root(profile_id)
    destination_root = output_root / "manual"
    destination_root.mkdir(parents=True, exist_ok=True)
    stem = _normalize_upload_stem(Path(original_name).stem)
    destination_path = destination_root / f"{stem}{suffix}"
    counter = 1
    while destination_path.exists():
        destination_path = destination_root / f"{stem}-{counter}{suffix}"
        counter += 1

    hasher = hashlib.sha1(usedforsecurity=False)
    bytes_written = 0
    with destination_path.open("wb") as destination:
        for chunk in uploaded_file.chunks():
            if not chunk:
                continue
            destination.write(chunk)
            hasher.update(chunk)
            bytes_written += len(chunk)
    if bytes_written <= 0:
        destination_path.unlink(missing_ok=True)
        raise ValueError("Empty file payload")

    now = timezone.now()
    item_uid = f"manual-{hasher.hexdigest()}-{bytes_written}"
    relative_path = (
        str(destination_path.relative_to(output_root))
        if destination_path.is_relative_to(output_root)
        else None
    )
    download, _created = Download.objects.update_or_create(
        profile_id=profile_id,
        item_uid=item_uid,
        defaults={
            "source_type": "manual",
            "source_name": "Manual Uploads",
            "source_url": None,
            "item_id": item_uid,
            "item_url": None,
            "media_url": None,
            "title": original_name,
            "description": "Imported via browser drag-and-drop",
            "uploader": "local",
            "channel": "Manual Uploads",
            "upload_date": now.date().isoformat(),
            "duration_seconds": None,
            "file_path": str(destination_path),
            "file_path_relative": relative_path,
            "file_ext": suffix.lstrip("."),
            "file_size_bytes": bytes_written,
            "subtitle_path": None,
            "subtitle_path_relative": None,
            "download_status": DownloadStatus.DOWNLOADED,
            "raw_metadata_json": '{"ingest_method":"drag-and-drop"}',
            "last_seen_at": now,
            "completed_at": now,
        },
    )
    return ManualUploadResult(download, destination_path)


@login_required
def enqueue_job(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    profile_id = _profile_id(request)
    job_type = str(request.POST.get("job_type") or "").strip()
    if job_type not in ALLOWED_JOB_TYPES:
        return HttpResponseBadRequest("Unsupported job_type")

    payload = {"source": "django_app"}
    completion_marker = ""
    if job_type == "update_downloads":
        completion_marker = uuid.uuid4().hex
        payload["completion_token"] = completion_marker
    if job_type == "download_single":
        payload["manual_enqueue"] = True
    if request.POST.get("url"):
        payload["url"] = str(request.POST["url"]).strip()
    default_idempotency = f"{job_type}:{profile_id}:{payload.get('url', 'manual')}"
    if job_type == "update_downloads":
        default_idempotency = f"{job_type}:{profile_id}:{completion_marker}"
    idempotency_key = request.POST.get("idempotency_key") or default_idempotency
    job = create_job(
        profile_id=profile_id,
        job_type=job_type,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    publish_job(
        {
            "job_id": job.id,
            "job_type": job.job_type,
            "profile_id": job.profile_id,
            "attempt": 1,
        }
    )
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.headers.get("accept") == "application/json"
    )
    if wants_json:
        status_params = {"profile_id": profile_id}
        if completion_marker:
            status_params["token"] = completion_marker
        else:
            status_params["job_id"] = str(job.id)
        status_query = urlencode(status_params)
        status_url = f"{reverse('worker_message_status')}?{status_query}"
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.id,
                "status": job.status,
                "status_url": status_url,
            }
        )
    next_url = str(request.POST.get("next") or "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return HttpResponseRedirect(next_url)
    return HttpResponseRedirect(reverse("jobs"))


@login_required
def worker_message_status(request: HttpRequest) -> JsonResponse:
    profile_id = _profile_id(request)
    token = str(request.GET.get("token") or "").strip()
    job_id = str(request.GET.get("job_id") or "").strip()
    if not token and job_id:
        job = Job.objects.filter(profile_id=profile_id, id=job_id).first()
        if job is None:
            return JsonResponse({"finished": False, "ok": True, "status": "pending"})
        return JsonResponse(
            {
                "finished": job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED},
                "ok": job.status is not JobStatus.FAILED,
                "status": job.status,
                "error_message": job.error_message or "",
            }
        )
    if not token:
        return JsonResponse(
            {
                "finished": False,
                "ok": False,
                "error_message": "Missing completion token",
            },
            status=400,
        )
    message = (
        Job.objects.filter(
            profile_id=profile_id,
            job_type="worker_message",
            payload__event_type="update_downloads_finished",
            payload__completion_token=token,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if message is None:
        return JsonResponse({"finished": False, "ok": True, "status": "pending"})
    payload = message.payload if isinstance(message.payload, dict) else {}
    source_status = str(payload.get("source_status") or "")
    return JsonResponse(
        {
            "finished": True,
            "ok": parse_str_enum(JobStatus, source_status) is not JobStatus.FAILED,
            "status": source_status,
            "error_message": str(payload.get("error_message") or ""),
        }
    )


@login_required
@require_POST
def manual_upload(request: HttpRequest) -> JsonResponse:
    profile_id = _profile_id(request)
    uploaded_files = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not uploaded_files:
        return JsonResponse(
            {"ok": False, "error_message": "No files uploaded."}, status=400
        )

    created: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for uploaded_file in uploaded_files:
        try:
            download, path = _write_manual_upload(profile_id, uploaded_file)
            media_type = (
                "video" if path.suffix.lower() in VIDEO_UPLOAD_EXTENSIONS else "audio"
            )
            job = create_job(
                profile_id=profile_id,
                job_type="generate_transcript",
                payload={
                    "download_id": download.id,
                    "subtitles": True,
                    "source_type": "manual",
                    "media_type": media_type,
                    "recent_download": True,
                    "manual_upload": True,
                },
                idempotency_key=f"generate_transcript:{profile_id}:{download.id}",
            )
            publish_job(
                {
                    "job_id": job.id,
                    "job_type": job.job_type,
                    "profile_id": job.profile_id,
                    "attempt": 1,
                }
            )
            created.append(
                {"id": download.id, "title": download.title, "job_id": job.id}
            )
        except ValueError as exc:
            errors.append(
                {"filename": str(getattr(uploaded_file, "name", "")), "error": str(exc)}
            )

    status = 201 if created else 400
    return JsonResponse(
        {"ok": bool(created), "uploads": created, "errors": errors}, status=status
    )


@login_required
@require_POST
def mark_played(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    item.played = True
    item.played_at = timezone.now()
    item.last_seen_at = timezone.now()
    item.save(update_fields=["played", "played_at", "last_seen_at"])
    return _redirect_back(request)


@login_required
@require_POST
def mark_unplayed(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    item.played = False
    item.played_at = None
    item.last_seen_at = timezone.now()
    item.save(update_fields=["played", "played_at", "last_seen_at"])
    return _redirect_back(request)


@login_required
@require_POST
def favorite(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    item.favorite = True
    item.last_seen_at = timezone.now()
    item.save(update_fields=["favorite", "last_seen_at"])
    return _redirect_back(request)


@login_required
@require_POST
def unfavorite(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    item.favorite = False
    item.last_seen_at = timezone.now()
    item.save(update_fields=["favorite", "last_seen_at"])
    return _redirect_back(request)


def _playback_update_from_request(
    request: HttpRequest, item: Download
) -> PlaybackUpdate | None:
    try:
        position = max(0.0, float(request.POST.get("position_seconds") or 0.0))
    except (TypeError, ValueError):
        return None
    reason = str(request.POST.get("reason") or "").strip().lower()
    completed = reason in {"ended", "mini-ended"}
    previous_position = float(item.last_position_seconds or 0.0)
    return PlaybackUpdate(
        position=position,
        reason=reason,
        completed=completed,
        listened_delta=max(0.0, position - previous_position),
    )


def _apply_playback_update(item: Download, update: PlaybackUpdate, *, now) -> list[str]:
    item.last_position_seconds = 0.0 if update.completed else update.position
    item.total_listened_seconds = (
        float(item.total_listened_seconds or 0.0) + update.listened_delta
    )
    item.last_position_updated_at = now
    item.last_seen_at = now
    update_fields = [
        "last_position_seconds",
        "total_listened_seconds",
        "last_position_updated_at",
        "last_seen_at",
    ]
    if update.completed:
        item.played = True
        item.played_at = now
        update_fields.extend(["played", "played_at"])
    return update_fields


def _log_playback_update(item: Download, update: PlaybackUpdate) -> None:
    log.info(
        "player save_position download_id=%s position=%.3f reason=%s completed=%s delta=%.3f",
        item.id,
        update.position,
        update.reason or "unknown",
        update.completed,
        update.listened_delta,
    )


@login_required
@require_POST
def save_position(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    update = _playback_update_from_request(request, item)
    if update is None:
        return HttpResponse(status=400)

    update_fields = _apply_playback_update(item, update, now=timezone.now())
    _log_playback_update(item, update)
    item.save(update_fields=update_fields)
    return HttpResponse(status=204)


def _delete_download_media_file(item: Download) -> None:
    if not item.file_path:
        log.info("No media file path to delete for download_id=%s", item.pk)
        return
    media_path = Path(item.file_path).expanduser()
    try:
        exists_before = media_path.exists() or media_path.is_symlink()
        log.info(
            "Deleting media file for download_id=%s path=%s exists=%s is_dir=%s is_symlink=%s",
            item.pk,
            media_path,
            exists_before,
            media_path.is_dir(),
            media_path.is_symlink(),
        )
        if media_path.is_dir() and not media_path.is_symlink():
            log.warning(
                "Refusing to delete directory for download_id=%s path=%s",
                item.pk,
                media_path,
            )
            return
        media_path.unlink(missing_ok=True)
        log.info(
            "Deleted media file for download_id=%s path=%s existed_before=%s exists_after=%s",
            item.pk,
            media_path,
            exists_before,
            media_path.exists() or media_path.is_symlink(),
        )
    except OSError:
        log.exception(
            "Could not delete media file for download_id=%s path=%s",
            item.pk,
            media_path,
        )


def _delete_external_download_dependents(row_ids: list[int]) -> None:
    if not row_ids:
        return
    existing_tables = set(connection.introspection.table_names())
    dependent_delete_statements = {
        "media_summaries": "DELETE FROM media_summaries WHERE download_id = %s",
    }
    with connection.cursor() as cursor:
        for table_name, delete_statement in dependent_delete_statements.items():
            if table_name not in existing_tables:
                log.info(
                    "Purge dependency cleanup skipped missing table=%s row_ids=%s",
                    table_name,
                    row_ids,
                )
                continue
            cursor.executemany(delete_statement, [(row_id,) for row_id in row_ids])
            deleted = cursor.rowcount
            log.info(
                "Purge dependency cleanup deleted table=%s row_ids=%s deleted_count=%s",
                table_name,
                row_ids,
                deleted,
            )


@login_required
@require_POST
def delete_file(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    _delete_download_media_file(item)
    item.download_status = DownloadStatus.MISSING
    item.last_seen_at = timezone.now()
    item.save(update_fields=["download_status", "last_seen_at"])
    return _redirect_back(request)


@login_required
def transcript_search(request: HttpRequest) -> JsonResponse:
    profile_id = _profile_id(request)
    query = str(request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    segments = (
        TranscriptSegment.objects.select_related("download")
        .filter(download__profile_id=profile_id, text__icontains=query)
        .filter(download__download_status__in=DOWNLOAD_STATUSES)
        .order_by("download_id", "start_seconds")[:50]
    )
    results = [
        {
            "id": segment.download_id,
            "title": segment.download.title or "Untitled",
            "source_name": segment.download.source_name or segment.download.source_type,
            "start_seconds": segment.start_seconds,
            "text": segment.text,
            "url": reverse("player", args=[segment.download_id])
            + f"?t={int(segment.start_seconds)}",
        }
        for segment in segments
    ]
    return JsonResponse({"results": results})


@login_required
@require_POST
def edit_metadata(request: HttpRequest) -> JsonResponse:
    raw_id = str(request.POST.get("id") or "").strip()
    if not raw_id.isdigit():
        return JsonResponse({"ok": False, "error": "Invalid id"}, status=400)
    item = get_object_or_404(Download, pk=int(raw_id), profile_id=_profile_id(request))
    title = str(request.POST.get("title") or "").strip()
    source_name = str(request.POST.get("source_name") or "").strip()
    if not title or not source_name:
        return JsonResponse(
            {"ok": False, "error": "Title and source name are required"}, status=400
        )
    item.title = title
    item.source_name = source_name
    item.last_seen_at = timezone.now()
    item.save(update_fields=["title", "source_name", "last_seen_at"])
    return JsonResponse({"ok": True})


@login_required
@require_POST
def save_config(request: HttpRequest) -> HttpResponseRedirect:
    profile_id = _profile_id(request)
    now = timezone.now()
    checkbox_keys = {
        "manual_upload_delete_explicit_content",
        "android_sync_enabled",
        "android_sync_include_subtitles",
        "android_sync_include_unplayed",
        "android_sync_include_started",
        "android_sync_include_played",
    }
    posted_config_keys = {
        key.removeprefix("config__")
        for key in request.POST
        if key.startswith("config__")
    }
    for checkbox_key in checkbox_keys:
        if (
            checkbox_key in posted_config_keys
            and f"config__{checkbox_key}" not in request.POST
        ):
            ProfileConfigValue.objects.update_or_create(
                profile_id=profile_id,
                key=checkbox_key,
                defaults={"value": "0", "updated_at": now},
            )
    for key, value in request.POST.items():
        if not key.startswith("config__"):
            continue
        config_key = key.removeprefix("config__")
        ProfileConfigValue.objects.update_or_create(
            profile_id=profile_id,
            key=config_key,
            defaults={"value": str(value), "updated_at": now},
        )
    if "youtube_cookie_text" in request.POST:
        ProfileDownloadSettings.objects.update_or_create(
            profile_id=profile_id,
            defaults={
                "youtube_cookie_text": request.POST.get("youtube_cookie_text") or "",
                "cookie_updated_at": now,
                "updated_at": now,
            },
        )
    if "config__auto_update_minutes" in request.POST:
        _sync_update_downloads_schedule(
            profile_id, request.POST.get("config__auto_update_minutes"), now=now
        )
    return HttpResponseRedirect(reverse("settings"))


@login_required
@require_POST
def add_source(request: HttpRequest) -> HttpResponseRedirect:
    source_type = parse_str_enum(SourceType, request.POST.get("source_type"))
    if source_type not in {SourceType.YOUTUBE, SourceType.PODCAST}:
        return HttpResponseBadRequest("Invalid source_type")

    profile_id = _profile_id(request)
    form = _source_form_data(request, source_type)
    position = _next_source_position(profile_id, source_type)
    SourceConfig.objects.create(
        profile_id=profile_id,
        source_type=source_type,
        position=position,
        name=form.name,
        url=form.url,
        media_type=form.media_type,
        enabled=True,
        subtitles=form.subtitles,
        subtitle_offset_seconds=form.subtitle_offset_seconds,
        max_downloads=form.max_downloads,
        delete_explicit_content=form.delete_explicit_content,
        include_shorts=form.include_shorts,
        include_livestreams=form.include_livestreams,
        title_exclude=form.title_exclude,
        updated_at=timezone.now(),
    )
    return HttpResponseRedirect(reverse("settings"))


def _next_source_position(profile_id: str, source_type: SourceType) -> int:
    return (
        SourceConfig.objects.filter(profile_id=profile_id, source_type=source_type)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
        or -1
    ) + 1


@login_required
@require_POST
def update_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    source = get_object_or_404(
        SourceConfig, pk=source_id, profile_id=_profile_id(request)
    )
    form = _source_form_data(request, source.source_type)
    _apply_source_form_data(source, form, now=timezone.now(), include_enabled=False)
    source.save(update_fields=_source_update_fields(include_enabled=False))
    return HttpResponseRedirect(reverse("settings"))


@login_required
@require_POST
def save_sources(request: HttpRequest, source_type: str) -> HttpResponseRedirect:
    source_type = str(source_type or "").strip().lower()
    if source_type not in {SourceType.YOUTUBE, SourceType.PODCAST}:
        return HttpResponseBadRequest("Invalid source_type")

    profile_id = _profile_id(request)
    source_ids = _posted_source_ids(request)
    sources_by_id = _editable_sources_by_id(profile_id, source_type, source_ids)
    now = timezone.now()
    for source_id in source_ids:
        source = sources_by_id.get(source_id)
        if source is None:
            continue
        _save_source_row(request, source, now=now)
    return HttpResponseRedirect(reverse("settings"))


def _posted_source_ids(request: HttpRequest) -> list[int]:
    return [
        int(value)
        for value in request.POST.getlist("source_ids")
        if str(value).isdigit()
    ]


def _editable_sources_by_id(
    profile_id: str, source_type: str, source_ids: list[int]
) -> dict[int, SourceConfig]:
    sources = SourceConfig.objects.filter(
        pk__in=source_ids, profile_id=profile_id, source_type=source_type
    )
    return {source.id: source for source in sources}


def _save_source_row(request: HttpRequest, source: SourceConfig, *, now) -> None:
    prefix = f"source_{source.id}__"
    if _posted_bool(request, prefix + "delete"):
        source.delete()
        return
    form = _source_form_data(request, source.source_type, prefix=prefix)
    _apply_source_form_data(source, form, now=now)
    source.save(update_fields=_source_update_fields())


@login_required
@require_POST
def toggle_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    source = get_object_or_404(
        SourceConfig, pk=source_id, profile_id=_profile_id(request)
    )
    source.enabled = not source.enabled
    source.updated_at = timezone.now()
    source.save(update_fields=["enabled", "updated_at"])
    return HttpResponseRedirect(reverse("settings"))


@login_required
@require_POST
def delete_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    source = get_object_or_404(
        SourceConfig, pk=source_id, profile_id=_profile_id(request)
    )
    source.delete()
    return HttpResponseRedirect(reverse("settings"))


@login_required
@require_POST
def batch_update(request: HttpRequest) -> HttpResponseRedirect:
    ids = [int(value) for value in request.POST.getlist("ids") if str(value).isdigit()]
    action = str(request.POST.get("batch_action") or "").strip()
    profile_id = _profile_id(request)
    now = timezone.now()
    rows = Download.objects.filter(pk__in=ids, profile_id=profile_id)
    log.info(
        "Batch update requested profile_id=%s action=%s requested_ids=%s matched_count=%s",
        profile_id,
        action or "<empty>",
        ids,
        rows.count(),
    )
    if action == "played":
        rows.update(played=True, played_at=now, last_seen_at=now)
    elif action == "unplayed":
        rows.update(played=False, played_at=None, last_seen_at=now)
    elif action == "favorite":
        rows.update(favorite=True, last_seen_at=now)
    elif action == "unfavorite":
        rows.update(favorite=False, last_seen_at=now)
    elif action == "delete":
        for item in rows:
            _delete_download_media_file(item)
        rows.update(download_status=DownloadStatus.MISSING, last_seen_at=now)
    elif action == "purge":
        selected_rows = list(rows)
        row_ids = [item.pk for item in selected_rows]
        log.info(
            "Purge batch action starting profile_id=%s requested_ids=%s matched_ids=%s",
            profile_id,
            ids,
            row_ids,
        )
        for item in selected_rows:
            log.info(
                "Purging download_id=%s profile_id=%s title=%r status=%s file_path=%r",
                item.pk,
                item.profile_id,
                item.title,
                item.download_status,
                item.file_path,
            )
            _delete_download_media_file(item)
        if row_ids:
            try:
                _delete_external_download_dependents(row_ids)
                deleted_count, deleted_by_model = Download.objects.filter(
                    pk__in=row_ids, profile_id=profile_id
                ).delete()
            except Exception:
                log.exception(
                    "Purge database delete failed profile_id=%s row_ids=%s",
                    profile_id,
                    row_ids,
                )
                raise
            log.info(
                "Purge database delete finished profile_id=%s row_ids=%s deleted_count=%s deleted_by_model=%s",
                profile_id,
                row_ids,
                deleted_count,
                deleted_by_model,
            )
        else:
            log.info(
                "Purge batch action matched no downloads profile_id=%s", profile_id
            )
    elif action == "edit-metadata":
        return _redirect_back(request)
    elif action == "download":
        for item in rows:
            job = create_job(
                profile_id=profile_id,
                job_type="download_single",
                payload={
                    "source": "django_app",
                    "url": item.item_url or item.media_url or item.source_url,
                    "source_type": item.source_type,
                    "source_name": item.source_name,
                    "media_type": "audio" if item.source_type == "podcast" else "video",
                    "subtitles": True,
                    "redownload": True,
                },
                idempotency_key=f"download_single:{profile_id}:{item.pk}",
            )
            publish_job(
                {
                    "job_id": job.id,
                    "job_type": job.job_type,
                    "profile_id": job.profile_id,
                    "attempt": 1,
                }
            )
    return _redirect_back(request)
