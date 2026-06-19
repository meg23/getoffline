import mimetypes
from pathlib import Path

from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from models.jobs import create_job
from models.models import AppConfigValue, Download, DownloadSettings, Job, SourceConfig

from .queue import publish_job


ALLOWED_JOB_TYPES = {"update_downloads", "download_single", "sync_media", "summarize_missing"}
DOWNLOAD_STATUSES = ["downloaded", "missing", "retention_deleted"]


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


def library(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    show_played = request.GET.get("show_played") in {"1", "true", "yes", "on"}
    favorites = request.GET.get("favorites") in {"1", "true", "yes", "on"}
    downloads = Download.objects.filter(profile_id=profile_id, download_status__in=DOWNLOAD_STATUSES)
    if not show_played and not favorites:
        downloads = downloads.filter(played=False)
    if favorites:
        downloads = downloads.filter(favorite=True)
    downloads = downloads.order_by("-last_seen_at", "-id")[:200]
    recent_jobs = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:10]
    return render(
        request,
        "app/library.html",
        {"downloads": downloads, "jobs": recent_jobs, "profile_id": profile_id, "show_played": show_played, "favorites": favorites},
    )


def jobs(request: HttpRequest) -> HttpResponse:
    profile_id = _profile_id(request)
    rows = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[:100]
    return render(request, "app/jobs.html", {"jobs": rows, "profile_id": profile_id})


def player(request: HttpRequest, download_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=download_id)
    return render(request, "app/player.html", {"item": item})


def media(request: HttpRequest, download_id: int) -> FileResponse:
    item = get_object_or_404(Download, pk=download_id)
    path = _safe_path(item.file_path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path.open("rb"), content_type=content_type)


def subtitle(request: HttpRequest, download_id: int) -> FileResponse:
    item = get_object_or_404(Download, pk=download_id)
    path = _safe_path(item.subtitle_path)
    return FileResponse(path.open("rb"), content_type="text/vtt" if path.suffix == ".vtt" else "text/plain")


def settings_page(request: HttpRequest) -> HttpResponse:
    configs = AppConfigValue.objects.order_by("key")
    sources = SourceConfig.objects.order_by("source_type", "position", "id")
    download_settings = DownloadSettings.objects.filter(pk=1).first()
    return render(request, "app/settings.html", {"configs": configs, "sources": sources, "download_settings": download_settings})


def enqueue_job(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseBadRequest("POST required")
    profile_id = _profile_id(request)
    job_type = str(request.POST.get("job_type") or "").strip()
    if job_type not in ALLOWED_JOB_TYPES:
        return HttpResponseBadRequest("Unsupported job_type")

    payload = {"source": "django_app"}
    if request.POST.get("url"):
        payload["url"] = str(request.POST["url"]).strip()
    idempotency_key = request.POST.get("idempotency_key") or f"{job_type}:{profile_id}:{payload.get('url', 'manual')}"
    job = create_job(profile_id=profile_id, job_type=job_type, payload=payload, idempotency_key=idempotency_key)
    publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    return HttpResponseRedirect(reverse("jobs") + f"?profile_id={profile_id}")


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
    position = max(0.0, float(request.POST.get("position_seconds") or 0.0))
    delta = max(0.0, position - float(item.last_position_seconds or 0.0))
    item.last_position_seconds = position
    item.total_listened_seconds = float(item.total_listened_seconds or 0.0) + delta
    item.last_position_updated_at = timezone.now()
    item.last_seen_at = timezone.now()
    item.save(update_fields=["last_position_seconds", "total_listened_seconds", "last_position_updated_at", "last_seen_at"])
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


@require_POST
def save_config(request: HttpRequest) -> HttpResponseRedirect:
    now = timezone.now()
    for key, value in request.POST.items():
        if not key.startswith("config__"):
            continue
        config_key = key.removeprefix("config__")
        AppConfigValue.objects.update_or_create(key=config_key, defaults={"value": value, "updated_at": now})
    return HttpResponseRedirect(reverse("settings"))


@require_POST
def add_source(request: HttpRequest) -> HttpResponseRedirect:
    source_type = str(request.POST.get("source_type") or "").strip().lower()
    if source_type not in {SourceConfig.SOURCE_YOUTUBE, SourceConfig.SOURCE_PODCAST}:
        return HttpResponseBadRequest("Invalid source_type")
    position = (SourceConfig.objects.filter(source_type=source_type).order_by("-position").values_list("position", flat=True).first() or -1) + 1
    SourceConfig.objects.create(
        source_type=source_type,
        position=position,
        name=str(request.POST.get("name") or "").strip(),
        url=str(request.POST.get("url") or "").strip(),
        media_type=str(request.POST.get("media_type") or "audio").strip().lower() if source_type == SourceConfig.SOURCE_YOUTUBE else None,
        enabled=True,
        subtitles=request.POST.get("subtitles", "1") in {"1", "true", "yes", "on"},
        updated_at=timezone.now(),
    )
    return HttpResponseRedirect(reverse("settings"))


@require_POST
def toggle_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    source = get_object_or_404(SourceConfig, pk=source_id)
    source.enabled = not source.enabled
    source.updated_at = timezone.now()
    source.save(update_fields=["enabled", "updated_at"])
    return HttpResponseRedirect(reverse("settings"))


@require_POST
def delete_source(request: HttpRequest, source_id: int) -> HttpResponseRedirect:
    get_object_or_404(SourceConfig, pk=source_id).delete()
    return HttpResponseRedirect(reverse("settings"))
