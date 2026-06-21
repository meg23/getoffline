import mimetypes
import uuid
import re
from pathlib import Path
from urllib.parse import urlencode

from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.db.models import Count, Q, Sum
from django.views.decorators.http import require_POST
from django.utils.http import url_has_allowed_host_and_scheme

from models.jobs import create_job
from models.models import AppConfigValue, Download, DownloadSettings, Job, ProfileConfigValue, ProfileDownloadSettings, SourceConfig, TranscriptSegment

from .queue import publish_job
from .routing import FFMPEG_QUEUE, SERIAL_DOWNLOAD_QUEUE, SERIAL_EPISODE_CHECK_QUEUE, SUMMARY_QUEUE, SYNC_QUEUE, TRANSCRIPT_QUEUE, queue_name


ALLOWED_JOB_TYPES = {"update_downloads", "download_single", "sync_media", "summarize_missing"}
DOWNLOAD_STATUSES = ["downloaded", "missing", "retention_deleted"]


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
    item.display_type = (item.file_ext or Path(str(item.file_path or "")).suffix.lstrip(".") or "?").upper()
    item.display_kind = "video" if item.display_type.lower() in {"mp4", "mkv", "webm", "mov"} else "audio"
    item.status_label = "UNPLAYED"
    item.status_class = "status-unplayed"
    if position > 0 and not item.played:
        item.status_label = "STARTED"
        item.status_class = "status-started"
    if item.played:
        item.status_label = "PLAYED"
        item.status_class = "status-played"
    if item.download_status in {"missing", "retention_deleted"}:
        item.status_label = "REMOVED" if item.download_status == "retention_deleted" else "MISSING"
        item.status_class = "status-missing"
    try:
        item.resolved_subtitle_path = _resolve_subtitle_path(item) if item.download_status == "downloaded" else None
    except Http404:
        item.resolved_subtitle_path = None
    item.has_subtitles = item.resolved_subtitle_path is not None
    return item


def _profile_choices(active_profile_id: str) -> list[dict[str, object]]:
    profile_ids = {"default", active_profile_id}
    for model in (Download, SourceConfig, ProfileConfigValue):
        profile_ids.update(value for value in model.objects.values_list("profile_id", flat=True).distinct() if value)
    return [
        {
            "id": profile_id,
            "name": profile_id if profile_id != "default" else "max",
            "selected": profile_id == active_profile_id,
        }
        for profile_id in sorted(profile_ids, key=lambda value: (value != "default", value.lower()))
    ]


def _profile_id(request: HttpRequest) -> str:
    return str(request.GET.get("profile_id") or request.POST.get("profile_id") or request.session.get("profile_id") or "default")


def _redirect_back(request: HttpRequest, fallback: str = "library") -> HttpResponseRedirect:
    return HttpResponseRedirect(request.POST.get("next") or request.headers.get("Referer") or reverse(fallback))


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
        or AppConfigValue.objects.filter(key="output_root").values_list("value", flat=True).first()
        or PROFILE_DEFAULTS["output_root"]
    )
    return Path(str(value)).expanduser().resolve()


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
    media_path = _safe_path(item.file_path)
    candidates: list[Path] = []
    if item.subtitle_path:
        candidates.append(Path(str(item.subtitle_path)))
    if item.subtitle_path_relative:
        candidates.append(_profile_output_root(item.profile_id) / str(item.subtitle_path_relative))
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


def _queue_missing_summary_batch(profile_id: str, *, reason: str) -> bool:
    """Queue one batch summary fanout job when downloaded subtitle-backed rows lack summaries."""
    has_missing_summary = (
        Download.objects.filter(profile_id=profile_id, download_status="downloaded")
        .exclude(Q(subtitle_path__isnull=True) | Q(subtitle_path=""))
        .filter(summary__isnull=True)
        .exists()
    )
    if not has_missing_summary:
        return False

    idempotency_key = f"summarize_missing:{profile_id}:auto"
    active_job = Job.objects.filter(
        idempotency_key=idempotency_key,
        status__in=[Job.STATUS_QUEUED, Job.STATUS_RUNNING],
    ).first()
    if active_job is not None:
        return False

    job = create_job(
        profile_id=profile_id,
        job_type="summarize_missing",
        payload={"source": "django_app", "auto_enqueue": True, "reason": reason},
        idempotency_key=idempotency_key,
    )
    try:
        publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    except Exception:
        job.status = Job.STATUS_FAILED
        job.error_message = "Failed to publish automatic summarize_missing job"
        job.finished_at = timezone.now()
        job.updated_at = job.finished_at
        job.save(update_fields=["status", "error_message", "finished_at", "updated_at"])
        return False
    return True

def library(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    _queue_missing_summary_batch(profile_id, reason="library_missing_summary")
    downloads_qs = Download.objects.select_related("summary").filter(profile_id=profile_id, download_status__in=DOWNLOAD_STATUSES)
    downloads = [_decorate_download(item) for item in downloads_qs.order_by("-last_seen_at", "-id")[:500]]
    played_count = sum(1 for item in downloads if item.played)
    favorite_count = sum(1 for item in downloads if item.favorite)
    listened_seconds = downloads_qs.aggregate(total=Sum("total_listened_seconds")).get("total") or 0
    recent_jobs = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:10]
    profile_name = profile_id if profile_id != "default" else "max"
    return render(
        request,
        "app/library.html",
        {
            "downloads": downloads,
            "jobs": recent_jobs,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "profile_initial": (profile_name[:1] or "M").upper(),
            "profiles": _profile_choices(profile_id),
            "stats": {
                "visible": len(downloads),
                "played": played_count,
                "new": max(len(downloads) - played_count, 0),
                "favorites": favorite_count,
                "listened": _human_duration(listened_seconds),
            },
        },
    )


def jobs(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    rows = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:100]
    return render(request, "app/jobs.html", {"jobs": rows, "profile_id": profile_id})


def player(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id)
    item.resolved_subtitle_path = _resolve_subtitle_path(item) if item.download_status == "downloaded" else None
    item.has_subtitles = item.resolved_subtitle_path is not None
    if not hasattr(item, "summary") and item.download_status == "downloaded" and item.has_subtitles:
        _queue_missing_summary_batch(item.profile_id, reason="player_missing_summary")
    try:
        requested_seek = float(request.GET.get("t") or 0.0)
    except (TypeError, ValueError):
        requested_seek = 0.0
    seek = max(float(item.last_position_seconds or 0.0), requested_seek)
    media_kind = "video" if (item.file_ext or Path(str(item.file_path or "")).suffix.lstrip(".")).lower() in {"mp4", "mkv", "webm", "mov"} else "audio"
    return render(request, "app/player.html", {"item": item, "seek_seconds": seek, "media_kind": media_kind})


def media(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id)
    path = _safe_path(item.file_path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_size = path.stat().st_size
    range_header = request.headers.get("Range", "")
    if range_header.startswith("bytes="):
        start_text, _, end_text = range_header.removeprefix("bytes=").partition("-")
        try:
            start = int(start_text) if start_text else 0
            end = int(end_text) if end_text else file_size - 1
        except ValueError:
            start, end = 0, file_size - 1
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        length = end - start + 1
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(length)
        response = HttpResponse(data, status=206, content_type=content_type)
        response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        response["Content-Length"] = str(length)
    else:
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)
    response["Accept-Ranges"] = "bytes"
    return response


def subtitle(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id)
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
    "output_root": "./downloads/profiles/default",
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
    "deno_path": "deno",
    "summary_model": "qwen2.5:0.5b",
    "ollama_path": "ollama",
    "android_sync_target": "android",
    "android_sync_enabled": "0",
    "android_sync_max_items": "10",
    "android_sync_directory": "./offline-sync",
    "android_sync_destination": "/sdcard/Movies/GetOffline",
    "android_sync_adb_path": "adb",
    "android_sync_connection_mode": "usb",
    "android_sync_wifi_address": "",
    "android_sync_include_subtitles": "1",
    "android_sync_include_unplayed": "1",
    "android_sync_include_started": "1",
    "android_sync_include_played": "0",
    "android_sync_exclude_regex": "",
    "profile_pin": "",
}


def _profile_settings(profile_id: str) -> dict[str, str]:
    values = dict(PROFILE_DEFAULTS)
    values["output_root"] = f"./downloads/profiles/{profile_id}"
    values.update({row.key: row.value for row in AppConfigValue.objects.order_by("key")})
    values.update({row.key: row.value for row in ProfileConfigValue.objects.filter(profile_id=profile_id)})
    return values


def _checked(settings: dict[str, str], key: str) -> bool:
    return str(settings.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}


def _queue_counts(profile_id: str) -> list[dict[str, object]]:
    queue_labels = {
        SERIAL_EPISODE_CHECK_QUEUE: "Updates",
        SERIAL_DOWNLOAD_QUEUE: "Downloads",
        FFMPEG_QUEUE: "FFmpeg",
        TRANSCRIPT_QUEUE: "Transcripts",
        SUMMARY_QUEUE: "Summaries",
        SYNC_QUEUE: "Sync",
    }
    counts = {
        queue: {Job.STATUS_QUEUED: 0, Job.STATUS_RUNNING: 0}
        for queue in queue_labels
    }
    rows = (
        Job.objects.filter(profile_id=profile_id, status__in=[Job.STATUS_QUEUED, Job.STATUS_RUNNING])
        .values("job_type", "status")
        .annotate(total=Count("id"))
    )
    for row in rows:
        queue = queue_name(str(row["job_type"]))
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
        for queue, values in sorted(counts.items(), key=lambda item: queue_labels[item[0]].lower())
    ]

def settings_page(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    settings = _profile_settings(profile_id)
    sources = SourceConfig.objects.filter(profile_id=profile_id).order_by("source_type", "position", "id")
    download_settings = ProfileDownloadSettings.objects.filter(profile_id=profile_id).first()
    if download_settings is None and profile_id == "default":
        legacy = DownloadSettings.objects.filter(pk=1).first()
        if legacy is not None:
            download_settings = ProfileDownloadSettings(profile_id=profile_id, youtube_cookie_text=legacy.youtube_cookie_text)
    profile_name = profile_id if profile_id != "default" else "max"
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
            "profile_initial": (profile_name[:1] or "M").upper(),
            "profiles": _profile_choices(profile_id),
            "manual_upload_filter_checked": _checked(settings, "manual_upload_delete_explicit_content"),
            "android_sync_enabled_checked": _checked(settings, "android_sync_enabled"),
            "android_sync_include_subtitles_checked": _checked(settings, "android_sync_include_subtitles"),
            "android_sync_include_unplayed_checked": _checked(settings, "android_sync_include_unplayed"),
            "android_sync_include_started_checked": _checked(settings, "android_sync_include_started"),
            "android_sync_include_played_checked": _checked(settings, "android_sync_include_played"),
            "pin_status": "PIN is set" if settings.get("profile_pin") else "No PIN set",
            "queue_counts": _queue_counts(profile_id),
        },
    )


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
    job = create_job(profile_id=profile_id, job_type=job_type, payload=payload, idempotency_key=idempotency_key)
    publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.headers.get("accept") == "application/json"
    )
    if wants_json:
        status_query = urlencode({"profile_id": profile_id, "token": completion_token})
        status_url = f"{reverse('worker_message_status')}?{status_query}"
        return JsonResponse({"ok": True, "job_id": job.id, "status": job.status, "status_url": status_url})
    next_url = str(request.POST.get("next") or "")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return HttpResponseRedirect(next_url)
    return HttpResponseRedirect(reverse("jobs") + f"?profile_id={profile_id}")


def worker_message_status(request: HttpRequest) -> JsonResponse:
    profile_id = _profile_id(request)
    token = str(request.GET.get("token") or "").strip()
    if not token:
        return JsonResponse(
            {"finished": False, "ok": False, "error_message": "Missing completion token"},
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
    return JsonResponse({
        "finished": True,
        "ok": source_status != Job.STATUS_FAILED,
        "status": source_status,
        "error_message": str(payload.get("error_message") or ""),
    })


@require_POST
def mark_played(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id)
    item.played = True
    item.played_at = timezone.now()
    item.last_seen_at = timezone.now()
    item.save(update_fields=["played", "played_at", "last_seen_at"])
    return _redirect_back(request)


@require_POST
def mark_unplayed(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id)
    item.played = False
    item.played_at = None
    item.last_seen_at = timezone.now()
    item.save(update_fields=["played", "played_at", "last_seen_at"])
    return _redirect_back(request)


@require_POST
def favorite(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id)
    item.favorite = True
    item.last_seen_at = timezone.now()
    item.save(update_fields=["favorite", "last_seen_at"])
    return _redirect_back(request)


@require_POST
def unfavorite(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id)
    item.favorite = False
    item.last_seen_at = timezone.now()
    item.save(update_fields=["favorite", "last_seen_at"])
    return _redirect_back(request)


@require_POST
def save_position(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id)
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
    update_fields = ["last_position_seconds", "total_listened_seconds", "last_position_updated_at", "last_seen_at"]
    if completed:
        item.played = True
        item.played_at = timezone.now()
        update_fields.extend(["played", "played_at"])
    item.save(update_fields=update_fields)
    return HttpResponse(status=204)


@require_POST
def delete_file(request: HttpRequest, download_id: int) -> HttpResponseRedirect:
    item = get_object_or_404(Download, pk=download_id)
    if item.file_path:
        Path(item.file_path).expanduser().unlink(missing_ok=True)
    item.download_status = "missing"
    item.last_seen_at = timezone.now()
    item.save(update_fields=["download_status", "last_seen_at"])
    return _redirect_back(request)



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
            "url": reverse("player", args=[segment.download_id]) + f"?t={int(segment.start_seconds)}",
        }
        for segment in segments
    ]
    return JsonResponse({"results": results})


@require_POST
def edit_metadata(request: HttpRequest) -> JsonResponse:
    raw_id = str(request.POST.get("id") or "").strip()
    if not raw_id.isdigit():
        return JsonResponse({"ok": False, "error": "Invalid id"}, status=400)
    item = get_object_or_404(Download, pk=int(raw_id))
    title = str(request.POST.get("title") or "").strip()
    source_name = str(request.POST.get("source_name") or "").strip()
    if not title or not source_name:
        return JsonResponse({"ok": False, "error": "Title and source name are required"}, status=400)
    item.title = title
    item.source_name = source_name
    item.last_seen_at = timezone.now()
    item.save(update_fields=["title", "source_name", "last_seen_at"])
    return JsonResponse({"ok": True})

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
    posted_config_keys = {key.removeprefix("config__") for key in request.POST if key.startswith("config__")}
    for checkbox_key in checkbox_keys:
        if checkbox_key in posted_config_keys and f"config__{checkbox_key}" not in request.POST:
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
    return HttpResponseRedirect(reverse("settings") + f"?profile_id={profile_id}")


@require_POST
def add_source(request: HttpRequest) -> HttpResponseRedirect:
    source_type = str(request.POST.get("source_type") or "").strip().lower()
    if source_type not in {SourceConfig.SOURCE_YOUTUBE, SourceConfig.SOURCE_PODCAST}:
        return HttpResponseBadRequest("Invalid source_type")
    profile_id = _profile_id(request)
    position = (SourceConfig.objects.filter(profile_id=profile_id, source_type=source_type).order_by("-position").values_list("position", flat=True).first() or -1) + 1
    SourceConfig.objects.create(
        profile_id=profile_id,
        source_type=source_type,
        position=position,
        name=str(request.POST.get("name") or "").strip(),
        url=str(request.POST.get("url") or "").strip(),
        media_type=str(request.POST.get("media_type") or "audio").strip().lower() if source_type == SourceConfig.SOURCE_YOUTUBE else None,
        enabled=True,
        subtitles=request.POST.get("subtitles", "1") in {"1", "true", "yes", "on"},
        max_downloads=int(request.POST["max_downloads"]) if str(request.POST.get("max_downloads") or "").strip().isdigit() else None,
        delete_explicit_content=request.POST.get("delete_explicit_content") in {"1", "true", "yes", "on"},
        updated_at=timezone.now(),
    )
    return HttpResponseRedirect(reverse("settings") + f"?profile_id={profile_id}")


@require_POST
def update_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    source = get_object_or_404(SourceConfig, pk=source_id)
    source.name = str(request.POST.get("name") or source.name).strip()
    source.url = str(request.POST.get("url") or source.url).strip()
    if source.source_type == SourceConfig.SOURCE_YOUTUBE:
        source.media_type = str(request.POST.get("media_type") or source.media_type or "audio").strip().lower()
    source.subtitles = request.POST.get("subtitles", "1") in {"1", "true", "yes", "on"}
    raw_max = str(request.POST.get("max_downloads") or "").strip()
    source.max_downloads = int(raw_max) if raw_max.isdigit() else None
    source.delete_explicit_content = request.POST.get("delete_explicit_content") in {"1", "true", "yes", "on"}
    source.updated_at = timezone.now()
    source.save(
        update_fields=[
            "name",
            "url",
            "media_type",
            "subtitles",
            "max_downloads",
            "delete_explicit_content",
            "updated_at",
        ]
    )
    return HttpResponseRedirect(reverse("settings") + f"?profile_id={source.profile_id}")


@require_POST
def toggle_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    source = get_object_or_404(SourceConfig, pk=source_id)
    source.enabled = not source.enabled
    source.updated_at = timezone.now()
    source.save(update_fields=["enabled", "updated_at"])
    return HttpResponseRedirect(reverse("settings") + f"?profile_id={source.profile_id}")


@require_POST
def delete_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    source = get_object_or_404(SourceConfig, pk=source_id)
    profile_id = source.profile_id
    source.delete()
    return HttpResponseRedirect(reverse("settings") + f"?profile_id={profile_id}")


@require_POST
def batch_update(request: HttpRequest) -> HttpResponseRedirect:
    ids = [int(value) for value in request.POST.getlist("ids") if str(value).isdigit()]
    action = str(request.POST.get("batch_action") or "").strip()
    now = timezone.now()
    rows = Download.objects.filter(pk__in=ids)
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
            if item.file_path:
                Path(item.file_path).expanduser().unlink(missing_ok=True)
        rows.update(download_status="missing", last_seen_at=now)
    elif action == "edit-metadata":
        return _redirect_back(request)
    elif action == "download":
        profile_id = _profile_id(request)
        for item in rows:
            job = create_job(
                profile_id=profile_id,
                job_type="download_single",
                payload={"source": "django_app", "url": item.item_url or item.media_url or item.source_url, "source_type": item.source_type, "source_name": item.source_name, "media_type": "audio" if item.source_type == "podcast" else "video", "subtitles": True, "redownload": True},
                idempotency_key=f"download_single:{profile_id}:{item.pk}",
            )
            publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    return _redirect_back(request)
