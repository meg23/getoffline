"""Thin JSON controllers for non-browser Get Offline clients."""
# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from backend.playback.service import apply_update, build_update, start
from backend.services.library import episode_to_summary, list_downloads
from backend.services.profiles import profile_id_for_request
from backend.streaming.media import media_response, resolve_media_path
from models.jobs import create_job
from models.models import Download, SourceConfig
from app.queue import publish_job


def _json_body(request: HttpRequest) -> dict[str, object]:
    if not request.body:
        return {}
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@login_required
@require_GET
def search(request: HttpRequest) -> JsonResponse:
    profile_id = profile_id_for_request(request)
    query = str(request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": []})
    rows = list_downloads(profile_id, show_all=True)
    lowered = query.lower()
    results = [episode_to_summary(item) for item in rows if lowered in (item.title or "").lower() or lowered in (item.description or "").lower()]
    return JsonResponse({"results": results[:50]})


@login_required
@require_GET
def podcasts(request: HttpRequest) -> JsonResponse:
    rows = SourceConfig.objects.filter(profile_id=profile_id_for_request(request), source_type="podcast").order_by("position", "id")
    return JsonResponse({"podcasts": [{"id": row.id, "name": row.name, "url": row.url, "enabled": row.enabled} for row in rows]})


@login_required
@require_GET
def episode_detail(request: HttpRequest, episode_id: int) -> JsonResponse:
    item = get_object_or_404(Download, pk=episode_id, profile_id=profile_id_for_request(request))
    return JsonResponse({"episode": episode_to_summary(item)})


@login_required
@require_GET
def library(request: HttpRequest) -> JsonResponse:
    rows = list_downloads(profile_id_for_request(request), show_all=request.GET.get("filter") == "all")
    return JsonResponse({"episodes": [episode_to_summary(item) for item in rows]})


@login_required
@require_POST
def playback_start(request: HttpRequest) -> JsonResponse:
    data = _json_body(request) or request.POST
    episode_id = data.get("episode_id") or data.get("download_id")
    item = get_object_or_404(Download, pk=episode_id, profile_id=profile_id_for_request(request))
    return JsonResponse({"playback": start(item).to_dict()})


@login_required
@require_POST
def playback_progress(request: HttpRequest) -> JsonResponse:
    data = _json_body(request) or request.POST
    item = get_object_or_404(Download, pk=data.get("episode_id") or data.get("download_id"), profile_id=profile_id_for_request(request))
    update = build_update(data.get("position_seconds"), data.get("reason"), item)
    if update is None:
        return JsonResponse({"ok": False, "error": "Invalid position_seconds"}, status=400)
    state = apply_update(item, update)
    return JsonResponse({"ok": True, "playback": state.to_dict()})


@login_required
@require_POST
def playback_complete(request: HttpRequest) -> JsonResponse:
    data = _json_body(request) or request.POST
    data = dict(data)
    data["reason"] = "complete"
    item = get_object_or_404(Download, pk=data.get("episode_id") or data.get("download_id"), profile_id=profile_id_for_request(request))
    update = build_update(data.get("position_seconds"), data.get("reason"), item)
    if update is None:
        return JsonResponse({"ok": False, "error": "Invalid position_seconds"}, status=400)
    return JsonResponse({"ok": True, "playback": apply_update(item, update).to_dict()})


@login_required
@require_GET
def history(request: HttpRequest) -> JsonResponse:
    rows = list_downloads(profile_id_for_request(request), show_all=True)
    return JsonResponse({"episodes": [episode_to_summary(item) for item in rows if item.played or float(item.last_position_seconds or 0.0) > 0]})


@login_required
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
        idempotency_key=str(data.get("idempotency_key") or f"download_single:{profile_id}:{url}"),
    )
    publish_job({"job_id": job.id, "job_type": job.job_type, "profile_id": job.profile_id, "attempt": 1})
    return JsonResponse({"ok": True, "download": {"job_id": job.id, "status": job.status}})


@login_required
@require_GET
def downloads(request: HttpRequest) -> JsonResponse:
    return library(request)


@login_required
@require_GET
def user(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"user": {"username": request.user.get_username(), "profile_id": profile_id_for_request(request)}})


@login_required
@require_GET
def stream(request: HttpRequest, episode_id: int) -> HttpResponse:
    item = get_object_or_404(Download, pk=episode_id, profile_id=profile_id_for_request(request))
    return media_response(resolve_media_path(item), request.headers.get("Range", ""))
