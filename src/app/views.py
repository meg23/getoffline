import hashlib
import logging
import os
import time
import mimetypes
import uuid
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode

from django.http import (
    FileResponse,
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.db.models import Count, Q, Sum
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.utils.http import url_has_allowed_host_and_scheme

from models.jobs import create_job
from models.models import (
    AppConfigValue,
    Download,
    DownloadSettings,
    Job,
    ProfileConfigValue,
    ProfileDownloadSettings,
    ScheduledJob,
    SourceConfig,
    TranscriptSegment,
)

from .queue import publish_job
from .routing import (
    PODCAST_DOWNLOAD_QUEUE,
    SERIAL_EPISODE_CHECK_QUEUE,
    TRANSFER_QUEUE,
    TRANSCRIPT_QUEUE,
    YOUTUBE_DOWNLOAD_QUEUE,
    queue_name,
)

ALLOWED_JOB_TYPES = {
    "update_downloads",
    "download_single",
    "transfer_media",
}
DOWNLOAD_STATUSES = ["downloaded", "missing", "retention_deleted"]
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
log = logging.getLogger(__name__)
MEDIA_RANGE_CHUNK_SIZE = 64 * 1024
MEDIA_INITIAL_RANGE_SIZE = 1024 * 1024


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
    if item.download_status in {"missing", "retention_deleted"}:
        item.status_label = (
            "REMOVED" if item.download_status == "retention_deleted" else "MISSING"
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
    return Path(str(value)).expanduser().resolve()


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
        status__in=[Job.STATUS_QUEUED, Job.STATUS_RUNNING],
    ).update(
        status=Job.STATUS_FAILED,
        error_message="Superseded by a manual transcript regeneration request.",
        finished_at=now,
        updated_at=now,
    )


@login_required
def library(request: HttpRequest) -> HttpResponse:
    total_start = time.perf_counter()
    profile_id = _profile_id(request)

    setup_start = time.perf_counter()
    downloads_qs = (
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
    setup_elapsed = time.perf_counter() - setup_start

    rows_start = time.perf_counter()
    downloads = [
        _decorate_download(item)
        for item in downloads_qs.order_by("-last_seen_at", "-id")[:100]
    ]
    rows_elapsed = time.perf_counter() - rows_start

    stats_start = time.perf_counter()
    played_count = sum(1 for item in downloads if item.played)
    favorite_count = sum(1 for item in downloads if item.favorite)
    listened_seconds = (
        downloads_qs.aggregate(total=Sum("total_listened_seconds")).get("total") or 0
    )
    stats_elapsed = time.perf_counter() - stats_start

    jobs_start = time.perf_counter()
    recent_jobs = list(
        Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:10]
    )
    jobs_elapsed = time.perf_counter() - jobs_start

    profile_name = request.user.get_username() or profile_id

    render_start = time.perf_counter()
    response = render(
        request,
        "app/library.html",
        {
            "downloads": downloads,
            "jobs": recent_jobs,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "profile_initial": (profile_name[:1] or "U").upper(),
            "stats": {
                "visible": len(downloads),
                "played": played_count,
                "new": max(len(downloads) - played_count, 0),
                "favorites": favorite_count,
                "listened": _human_duration(listened_seconds),
            },
        },
    )
    render_elapsed = time.perf_counter() - render_start
    total_elapsed = time.perf_counter() - total_start

    log.info(
        profile_id,
        setup_elapsed,
        rows_elapsed,
        stats_elapsed,
        jobs_elapsed,
        render_elapsed,
        total_elapsed,
        len(downloads),
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


def _job_stage(job: Job) -> tuple[str, str]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    active_stage = str(payload.get("active_stage") or "").strip()
    if active_stage == "downloading":
        return "downloading", "Downloading"
    if active_stage == "transcript_generation":
        return "transcript_generation", "Transcript generation"
    if job.job_type in {"download_episode", "download_single"}:
        return "downloading", "Downloading"
    if job.job_type in {"generate_transcript", "transcode_media"}:
        return "transcript_generation", "Transcript generation"
    return "queued", job.job_type.replace("_", " ").title()


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
        return download.download_status != "downloaded"
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
            status=Job.STATUS_RUNNING,
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
            "stage": _job_stage(job)[0],
            "stage_label": _job_stage(job)[1],
            "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        }
        for job in active_jobs
    ]


@login_required
def active_pipeline_status(request: HttpRequest) -> JsonResponse:
    items = _active_pipeline_items(_profile_id(request))
    return JsonResponse({"ok": True, "items": items, "active": bool(items)})


@login_required
def jobs(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    rows = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[
        :100
    ]
    return render(request, "app/jobs.html", {"jobs": rows, "profile_id": profile_id})


@login_required
def player(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    item.resolved_subtitle_path = (
        _resolve_subtitle_path(item) if item.download_status == "downloaded" else None
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
def media(request: HttpRequest, download_id: int) -> HttpResponse:
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
        if requested_end is None:
            end = min(start + MEDIA_INITIAL_RANGE_SIZE - 1, file_size - 1)
        else:
            end = max(start, min(requested_end, file_size - 1))
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
def subtitle(request: HttpRequest, download_id: int) -> HttpResponse:
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
        queue: {Job.STATUS_QUEUED: 0, Job.STATUS_RUNNING: 0} for queue in queue_labels
    }
    rows = (
        Job.objects.filter(
            profile_id=profile_id, status__in=[Job.STATUS_QUEUED, Job.STATUS_RUNNING]
        )
        .values("job_type", "status", "payload")
        .annotate(total=Count("id"))
    )
    for row in rows:
        queue = queue_name(
            str(row["job_type"]),
            row.get("payload") if isinstance(row.get("payload"), dict) else None,
        )
        counts.setdefault(queue, {Job.STATUS_QUEUED: 0, Job.STATUS_RUNNING: 0})
        counts[queue][str(row["status"])] = int(row["total"] or 0)
        queue_labels.setdefault(queue, queue.removeprefix("getoffline."))
    return [
        {
            "queue": queue,
            "label": queue_labels[queue],
            "queued": values[Job.STATUS_QUEUED],
            "running": values[Job.STATUS_RUNNING],
            "total": values[Job.STATUS_QUEUED] + values[Job.STATUS_RUNNING],
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
            "youtube_sources": sources.filter(source_type=SourceConfig.SOURCE_YOUTUBE),
            "podcast_sources": sources.filter(source_type=SourceConfig.SOURCE_PODCAST),
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


def _write_manual_upload(profile_id: str, uploaded_file) -> tuple[Download, Path]:
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

    hasher = hashlib.sha1()
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
            "download_status": "downloaded",
            "raw_metadata_json": '{"ingest_method":"drag-and-drop"}',
            "last_seen_at": now,
            "completed_at": now,
        },
    )
    return download, destination_path


@login_required
def enqueue_job(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    profile_id = _profile_id(request)
    job_type = str(request.POST.get("job_type") or "").strip()
    if job_type not in ALLOWED_JOB_TYPES:
        return HttpResponseBadRequest("Unsupported job_type")

    payload = {"source": "django_app"}
    completion_token = ""
    if job_type == "update_downloads":
        completion_token = uuid.uuid4().hex
        payload["completion_token"] = completion_token
    if job_type == "download_single":
        payload["manual_enqueue"] = True
    if request.POST.get("url"):
        payload["url"] = str(request.POST["url"]).strip()
    default_idempotency = f"{job_type}:{profile_id}:{payload.get('url', 'manual')}"
    if job_type == "update_downloads":
        default_idempotency = f"{job_type}:{profile_id}:{completion_token}"
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
        status_query = urlencode({"profile_id": profile_id, "token": completion_token})
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
            "ok": source_status != Job.STATUS_FAILED,
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


@login_required
@require_POST
def save_position(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    try:
        position = max(0.0, float(request.POST.get("position_seconds") or 0.0))
    except (TypeError, ValueError):
        return HttpResponse(status=400)
    reason = str(request.POST.get("reason") or "").strip().lower()
    completed = reason in {"ended", "mini-ended"}
    delta = max(0.0, position - float(item.last_position_seconds or 0.0))
    item.last_position_seconds = 0.0 if completed else position
    item.total_listened_seconds = float(item.total_listened_seconds or 0.0) + delta
    item.last_position_updated_at = timezone.now()
    item.last_seen_at = timezone.now()
    update_fields = [
        "last_position_seconds",
        "total_listened_seconds",
        "last_position_updated_at",
        "last_seen_at",
    ]
    log.info(
        "player save_position download_id=%s position=%.3f reason=%s completed=%s previous=%.3f delta=%.3f",
        item.id,
        position,
        reason or "unknown",
        completed,
        float(item.last_position_seconds or 0.0),
        delta,
    )
    if completed:
        item.played = True
        item.played_at = timezone.now()
        update_fields.extend(["played", "played_at"])
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


@login_required
@require_POST
def delete_file(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id, profile_id=_profile_id(request))
    _delete_download_media_file(item)
    item.download_status = "missing"
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
    source_type = str(request.POST.get("source_type") or "").strip().lower()
    if source_type not in {SourceConfig.SOURCE_YOUTUBE, SourceConfig.SOURCE_PODCAST}:
        return HttpResponseBadRequest("Invalid source_type")
    profile_id = _profile_id(request)
    position = (
        SourceConfig.objects.filter(profile_id=profile_id, source_type=source_type)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
        or -1
    ) + 1
    SourceConfig.objects.create(
        profile_id=profile_id,
        source_type=source_type,
        position=position,
        name=str(request.POST.get("name") or "").strip(),
        url=str(request.POST.get("url") or "").strip(),
        media_type=(
            str(request.POST.get("media_type") or "audio").strip().lower()
            if source_type == SourceConfig.SOURCE_YOUTUBE
            else None
        ),
        enabled=True,
        subtitles=request.POST.get("subtitles", "1") in {"1", "true", "yes", "on"},
        subtitle_offset_seconds=_optional_float(
            request.POST.get("subtitle_offset_seconds")
        ),
        max_downloads=_optional_int(request.POST.get("max_downloads")),
        delete_explicit_content=request.POST.get("delete_explicit_content")
        in {"1", "true", "yes", "on"},
        include_shorts=source_type == SourceConfig.SOURCE_YOUTUBE
        and request.POST.get("include_shorts") in {"1", "true", "yes", "on"},
        include_livestreams=source_type == SourceConfig.SOURCE_YOUTUBE
        and request.POST.get("include_livestreams") in {"1", "true", "yes", "on"},
        updated_at=timezone.now(),
    )
    return HttpResponseRedirect(reverse("settings"))


@login_required
@require_POST
def update_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    source = get_object_or_404(
        SourceConfig, pk=source_id, profile_id=_profile_id(request)
    )
    source.name = str(request.POST.get("name") or source.name).strip()
    source.url = str(request.POST.get("url") or source.url).strip()
    if source.source_type == SourceConfig.SOURCE_YOUTUBE:
        source.media_type = (
            str(request.POST.get("media_type") or source.media_type or "audio")
            .strip()
            .lower()
        )
    source.subtitles = request.POST.get("subtitles", "1") in {"1", "true", "yes", "on"}
    source.subtitle_offset_seconds = _optional_float(
        request.POST.get("subtitle_offset_seconds")
    )
    source.max_downloads = _optional_int(request.POST.get("max_downloads"))
    source.delete_explicit_content = request.POST.get("delete_explicit_content") in {
        "1",
        "true",
        "yes",
        "on",
    }
    if source.source_type == SourceConfig.SOURCE_YOUTUBE:
        source.include_shorts = request.POST.get("include_shorts") in {
            "1",
            "true",
            "yes",
            "on",
        }
        source.include_livestreams = request.POST.get("include_livestreams") in {
            "1",
            "true",
            "yes",
            "on",
        }
    source.updated_at = timezone.now()
    source.save(
        update_fields=[
            "name",
            "url",
            "media_type",
            "subtitles",
            "subtitle_offset_seconds",
            "max_downloads",
            "delete_explicit_content",
            "include_shorts",
            "include_livestreams",
            "updated_at",
        ]
    )
    return HttpResponseRedirect(reverse("settings"))


@login_required
@require_POST
def save_sources(request: HttpRequest, source_type: str) -> HttpResponseRedirect:
    source_type = str(source_type or "").strip().lower()
    if source_type not in {SourceConfig.SOURCE_YOUTUBE, SourceConfig.SOURCE_PODCAST}:
        return HttpResponseBadRequest("Invalid source_type")
    profile_id = _profile_id(request)
    source_ids = [
        int(value)
        for value in request.POST.getlist("source_ids")
        if str(value).isdigit()
    ]
    sources = SourceConfig.objects.filter(
        pk__in=source_ids, profile_id=profile_id, source_type=source_type
    )
    sources_by_id = {source.id: source for source in sources}
    now = timezone.now()
    for source_id in source_ids:
        source = sources_by_id.get(source_id)
        if source is None:
            continue
        prefix = f"source_{source_id}__"
        if request.POST.get(prefix + "delete") in {"1", "true", "yes", "on"}:
            source.delete()
            continue
        source.name = str(request.POST.get(prefix + "name") or source.name).strip()
        source.url = str(request.POST.get(prefix + "url") or source.url).strip()
        if source.source_type == SourceConfig.SOURCE_YOUTUBE:
            source.media_type = (
                str(
                    request.POST.get(prefix + "media_type")
                    or source.media_type
                    or "audio"
                )
                .strip()
                .lower()
            )
        source.enabled = request.POST.get(prefix + "enabled", "1") in {
            "1",
            "true",
            "yes",
            "on",
        }
        source.subtitles = request.POST.get(prefix + "subtitles", "1") in {
            "1",
            "true",
            "yes",
            "on",
        }
        source.subtitle_offset_seconds = _optional_float(
            request.POST.get(prefix + "subtitle_offset_seconds")
        )
        source.max_downloads = _optional_int(request.POST.get(prefix + "max_downloads"))
        source.delete_explicit_content = request.POST.get(
            prefix + "delete_explicit_content"
        ) in {"1", "true", "yes", "on"}
        if source.source_type == SourceConfig.SOURCE_YOUTUBE:
            source.include_shorts = request.POST.get(prefix + "include_shorts") in {
                "1",
                "true",
                "yes",
                "on",
            }
            source.include_livestreams = request.POST.get(
                prefix + "include_livestreams"
            ) in {"1", "true", "yes", "on"}
        source.updated_at = now
        source.save(
            update_fields=[
                "name",
                "url",
                "media_type",
                "enabled",
                "subtitles",
                "subtitle_offset_seconds",
                "max_downloads",
                "delete_explicit_content",
                "include_shorts",
                "include_livestreams",
                "updated_at",
            ]
        )
    return HttpResponseRedirect(reverse("settings"))


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
        rows.update(download_status="missing", last_seen_at=now)
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
            log.info("Purge batch action matched no downloads profile_id=%s", profile_id)
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
