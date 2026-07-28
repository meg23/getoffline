"""Thin JSON controllers for non-browser Get Offline clients."""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import json
from pathlib import Path

from django.contrib.auth import authenticate
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from api.api.auth import api_login_required
from api.playback.service import apply_update, build_update, start
from api.services.library import (
    episode_to_summary,
    human_duration,
    library_filter_counts,
    list_downloads,
    listened_seconds,
    normalize_library_filter,
    recent_jobs,
)
from api.services.profiles import profile_id_for_request
from api.streaming.media import (
    media_response,
    resolve_media_path,
    resolve_subtitle_path,
    subtitle_response,
)
from frontend.queue import publish_job
from models.jobs import create_job
from models.models import Download, Job, SourceConfig


def _safe_login_redirect(request: HttpRequest) -> str:
    next_url = str(request.POST.get("next") or "/")
    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return "/"
    return next_url


@require_POST
def login(request: HttpRequest) -> HttpResponse:
    username = str(request.POST.get("username") or "").strip()
    password = str(request.POST.get("password") or "")
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return JsonResponse(
            {"ok": False, "error": "Invalid username or password."}, status=401
        )
    auth_login(request, user)
    get_token(request)
    return HttpResponseRedirect(_safe_login_redirect(request))


@api_login_required
@require_POST
def logout(request: HttpRequest) -> HttpResponseRedirect:
    auth_logout(request)
    return HttpResponseRedirect("/login/")


def _json_body(request: HttpRequest) -> dict[str, object]:
    if not request.content_type.lower().startswith("application/json"):
        return {}
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _episode_for_frontend(item: Download) -> dict[str, object]:
    data = episode_to_summary(item)
    data.update(
        {
            "display_size": getattr(item, "display_size", "—"),
            "display_type": getattr(item, "display_type", "?"),
            "display_kind": getattr(item, "display_kind", "audio"),
            "status_label": getattr(item, "status_label", "UNPLAYED"),
            "status_class": getattr(item, "status_class", "status-unplayed"),
            "has_subtitles": bool(getattr(item, "has_subtitles", False)),
        }
    )
    return data


def _job_to_dict(job: Job) -> dict[str, object]:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "error_message": job.error_message or "",
        "created_at": job.created_at.isoformat() if job.created_at else "",
        "updated_at": job.updated_at.isoformat() if job.updated_at else "",
    }


@api_login_required
@require_GET
def frontend_library(request: HttpRequest) -> JsonResponse:
    profile_id = profile_id_for_request(request)
    filter_mode = normalize_library_filter(request.GET.get("filter"))
    episodes = list_downloads(profile_id, filter_mode=filter_mode)
    played_count = sum(1 for item in episodes if item.played)
    filter_counts = library_filter_counts(profile_id)
    profile_name = request.user.get_username() or profile_id
    return JsonResponse(
        {
            "downloads": [_episode_for_frontend(item) for item in episodes],
            "jobs": [_job_to_dict(job) for job in recent_jobs(profile_id)],
            "profile_id": profile_id,
            "profile_name": profile_name,
            "profile_initial": (profile_name[:1] or "U").upper(),
            "library_filter_mode": filter_mode,
            "stats": {
                "visible": len(episodes),
                "played": played_count,
                "new": max(len(episodes) - played_count, 0),
                "favorites": sum(1 for item in episodes if item.favorite),
                "listened": human_duration(listened_seconds(profile_id)),
                "filters": filter_counts,
            },
        }
    )


@api_login_required
@require_GET
def frontend_jobs(request: HttpRequest) -> JsonResponse:
    profile_id = profile_id_for_request(request)
    rows = Job.objects.filter(profile_id=profile_id).order_by("-created_at", "-id")[
        :100
    ]
    return JsonResponse(
        {"profile_id": profile_id, "jobs": [_job_to_dict(job) for job in rows]}
    )


@api_login_required
@require_GET
def frontend_player(request: HttpRequest, episode_id: int) -> JsonResponse:
    item = get_object_or_404(
        Download, pk=episode_id, profile_id=profile_id_for_request(request)
    )
    summary = episode_to_summary(item)
    has_subtitles = False
    try:
        has_subtitles = resolve_subtitle_path(item) is not None
    except Http404:
        has_subtitles = False
    try:
        requested_seek = float(request.GET.get("t") or 0.0)
    except (TypeError, ValueError):
        requested_seek = 0.0
    seek = max(float(item.last_position_seconds or 0.0), requested_seek)
    media_ext = (
        item.file_ext or Path(str(item.file_path or "")).suffix.lstrip(".")
    ).lower()
    media_kind = "video" if media_ext in {"mp4", "mkv", "webm", "mov"} else "audio"
    summary["has_subtitles"] = has_subtitles
    return JsonResponse(
        {"item": summary, "seek_seconds": seek, "media_kind": media_kind}
    )


@api_login_required
@require_GET
def subtitle(request: HttpRequest, episode_id: int) -> HttpResponse:
    item = get_object_or_404(
        Download, pk=episode_id, profile_id=profile_id_for_request(request)
    )
    path = resolve_subtitle_path(item)
    if path is None:
        from django.http import Http404

        raise Http404("Subtitle unavailable")
    return subtitle_response(path)


def _dashboard_view(
    name: str, request: HttpRequest, *args: object, **kwargs: object
) -> HttpResponse:
    from api.services import dashboard_actions
    from frontend import views as frontend_views

    # Keep legacy test mocks working while the API owns the implementation.
    # Keep the legacy test patch point dynamic; mypy cannot model module exports.
    setattr(dashboard_actions, "publish_job", frontend_views.publish_job)  # noqa: B010
    legacy = getattr(dashboard_actions, f"_legacy_{name}", None) or getattr(
        dashboard_actions, name
    )
    return legacy(request, *args, **kwargs)


@api_login_required
def dashboard_active_pipeline_status(request: HttpRequest) -> JsonResponse:
    return _dashboard_view("active_pipeline_status", request)


@api_login_required
def dashboard_enqueue_job(request: HttpRequest) -> HttpResponse:
    return _dashboard_view("enqueue_job", request)


@api_login_required
def dashboard_worker_message_status(request: HttpRequest) -> JsonResponse:
    return _dashboard_view("worker_message_status", request)


@api_login_required
def dashboard_batch_update(request: HttpRequest) -> HttpResponse:
    return _dashboard_view("batch_update", request)


@api_login_required
def dashboard_transcript_search(request: HttpRequest) -> JsonResponse:
    return _dashboard_view("transcript_search", request)


@api_login_required
def dashboard_manual_upload(request: HttpRequest) -> JsonResponse:
    return _dashboard_view("manual_upload", request)


@api_login_required
def dashboard_edit_metadata(request: HttpRequest) -> JsonResponse:
    return _dashboard_view("edit_metadata", request)


@api_login_required
def dashboard_mark_played(request: HttpRequest, download_id: int) -> HttpResponse:
    return _dashboard_view("mark_played", request, download_id)


@api_login_required
def dashboard_mark_unplayed(request: HttpRequest, download_id: int) -> HttpResponse:
    return _dashboard_view("mark_unplayed", request, download_id)


@api_login_required
def dashboard_favorite(request: HttpRequest, download_id: int) -> HttpResponse:
    return _dashboard_view("favorite", request, download_id)


@api_login_required
def dashboard_unfavorite(request: HttpRequest, download_id: int) -> HttpResponse:
    return _dashboard_view("unfavorite", request, download_id)


@api_login_required
def dashboard_save_position(request: HttpRequest, download_id: int) -> HttpResponse:
    return _dashboard_view("save_position", request, download_id)


@api_login_required
def dashboard_delete_file(request: HttpRequest, download_id: int) -> HttpResponse:
    return _dashboard_view("delete_file", request, download_id)


@api_login_required
def frontend_settings(request: HttpRequest) -> HttpResponse:
    return _dashboard_view("settings_page", request)


@api_login_required
def settings_save_config(request: HttpRequest) -> HttpResponse:
    return _dashboard_view("save_config", request)


@api_login_required
def settings_add_source(request: HttpRequest) -> HttpResponse:
    return _dashboard_view("add_source", request)


@api_login_required
def settings_save_sources(request: HttpRequest, source_type: str) -> HttpResponse:
    return _dashboard_view("save_sources", request, source_type)


@api_login_required
def settings_update_source(request: HttpRequest, source_id: int) -> HttpResponse:
    return _dashboard_view("update_source", request, source_id)


@api_login_required
def settings_toggle_source(request: HttpRequest, source_id: int) -> HttpResponse:
    return _dashboard_view("toggle_source", request, source_id)


@api_login_required
def settings_delete_source(request: HttpRequest, source_id: int) -> HttpResponse:
    return _dashboard_view("delete_source", request, source_id)


def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True, "service": "api"})


@api_login_required
@require_GET
def search(request: HttpRequest) -> JsonResponse:
    profile_id = profile_id_for_request(request)
    query = str(request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    rows = list_downloads(profile_id, show_all=True)
    lowered = query.lower()
    results = [
        episode_to_summary(item)
        for item in rows
        if lowered in (item.title or "").lower()
        or lowered in (item.description or "").lower()
    ]
    return JsonResponse({"results": results[:50]})


@api_login_required
@require_GET
def podcasts(request: HttpRequest) -> JsonResponse:
    rows = SourceConfig.objects.filter(
        profile_id=profile_id_for_request(request), source_type="podcast"
    ).order_by("position", "id")
    return JsonResponse(
        {
            "podcasts": [
                {"id": row.id, "name": row.name, "url": row.url, "enabled": row.enabled}
                for row in rows
            ]
        }
    )


@api_login_required
@require_GET
def episode_detail(request: HttpRequest, episode_id: int) -> JsonResponse:
    item = get_object_or_404(
        Download, pk=episode_id, profile_id=profile_id_for_request(request)
    )
    return JsonResponse({"episode": episode_to_summary(item)})


@api_login_required
@require_GET
def library(request: HttpRequest) -> JsonResponse:
    rows = list_downloads(
        profile_id_for_request(request), filter_mode=request.GET.get("filter")
    )
    return JsonResponse({"episodes": [episode_to_summary(item) for item in rows]})


@api_login_required
@require_POST
def playback_start(request: HttpRequest) -> JsonResponse:
    data = _json_body(request) or request.POST
    episode_id = data.get("episode_id") or data.get("download_id")
    item = get_object_or_404(
        Download, pk=episode_id, profile_id=profile_id_for_request(request)
    )
    return JsonResponse({"playback": start(item).to_dict()})


@api_login_required
@require_POST
def playback_progress(request: HttpRequest) -> JsonResponse:
    data = _json_body(request) or request.POST
    item = get_object_or_404(
        Download,
        pk=data.get("episode_id") or data.get("download_id"),
        profile_id=profile_id_for_request(request),
    )
    update = build_update(data.get("position_seconds"), data.get("reason"), item)
    if update is None:
        return JsonResponse(
            {"ok": False, "error": "Invalid position_seconds"}, status=400
        )
    state = apply_update(item, update)
    return JsonResponse({"ok": True, "playback": state.to_dict()})


@api_login_required
@require_POST
def playback_complete(request: HttpRequest) -> JsonResponse:
    data = _json_body(request) or request.POST
    data = dict(data)
    data["reason"] = "complete"
    item = get_object_or_404(
        Download,
        pk=data.get("episode_id") or data.get("download_id"),
        profile_id=profile_id_for_request(request),
    )
    update = build_update(data.get("position_seconds"), data.get("reason"), item)
    if update is None:
        return JsonResponse(
            {"ok": False, "error": "Invalid position_seconds"}, status=400
        )
    return JsonResponse({"ok": True, "playback": apply_update(item, update).to_dict()})


@api_login_required
@require_GET
def history(request: HttpRequest) -> JsonResponse:
    rows = list_downloads(profile_id_for_request(request), show_all=True)
    return JsonResponse(
        {
            "episodes": [
                episode_to_summary(item)
                for item in rows
                if item.played or float(item.last_position_seconds or 0.0) > 0
            ]
        }
    )


@api_login_required
@require_POST
def download(request: HttpRequest) -> JsonResponse:
    data = _json_body(request) or request.POST
    url = str(data.get("url") or "").strip()
    if not url:
        return JsonResponse({"ok": False, "error": "Missing url"}, status=400)
    profile_id = profile_id_for_request(request)
    job = create_job(
        profile_id=profile_id,
        job_type="download_single",
        payload={
            "source": "api",
            "url": url,
            "source_type": str(data.get("source_type") or "youtube"),
            "media_type": str(data.get("media_type") or "audio"),
            "subtitles": bool(data.get("subtitles", True)),
            "manual_enqueue": True,
        },
        idempotency_key=str(
            data.get("idempotency_key") or f"download_single:{profile_id}:{url}"
        ),
    )
    publish_job(
        {
            "job_id": job.id,
            "job_type": job.job_type,
            "profile_id": job.profile_id,
            "attempt": 1,
        }
    )
    return JsonResponse(
        {"ok": True, "download": {"job_id": job.id, "status": job.status}}
    )


@api_login_required
@require_GET
def downloads(request: HttpRequest) -> JsonResponse:
    return library(request)


@api_login_required
@require_GET
def user(request: HttpRequest) -> JsonResponse:
    return JsonResponse(
        {
            "user": {
                "username": request.user.get_username(),
                "profile_id": profile_id_for_request(request),
            }
        }
    )


@api_login_required
@require_GET
def csrf(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"csrf_token": get_token(request)})


@api_login_required
@require_GET
def stream(request: HttpRequest, episode_id: int) -> HttpResponse:
    item = get_object_or_404(
        Download, pk=episode_id, profile_id=profile_id_for_request(request)
    )
    return media_response(resolve_media_path(item), request.headers.get("Range", ""))
