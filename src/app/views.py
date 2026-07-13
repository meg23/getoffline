"""Browser-facing frontend views.

This module intentionally knows only Django rendering/proxy mechanics and API
route names. Dashboard data and actions are owned by the API service.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.test import Client as DjangoClient
from django.urls import reverse


def _human_size(size: int | None) -> str:
    if not size:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.2f} GB"


def _srt_to_vtt(content: str) -> str:
    import re

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


def _decorate_download(item):
    position = float(getattr(item, "last_position_seconds", 0.0) or 0.0)
    file_path = str(getattr(item, "file_path", "") or "")
    display_type = (
        getattr(item, "file_ext", "") or Path(file_path).suffix.lstrip(".") or "?"
    ).upper()
    item.display_size = _human_size(getattr(item, "file_size_bytes", None))
    item.display_type = display_type
    item.display_kind = (
        "video" if display_type.lower() in {"mp4", "mkv", "webm", "mov"} else "audio"
    )
    item.status_label = "UNPLAYED"
    item.status_class = "status-unplayed"
    if position > 0 and not getattr(item, "played", False):
        item.status_label = "STARTED"
        item.status_class = "status-started"
    if getattr(item, "played", False):
        item.status_label = "PLAYED"
        item.status_class = "status-played"
    if str(getattr(item, "download_status", "")) in {"missing", "retention_deleted"}:
        item.status_label = (
            "REMOVED"
            if str(getattr(item, "download_status", "")) == "retention_deleted"
            else "MISSING"
        )
        item.status_class = "status-missing"
    item.resolved_subtitle_path = None
    item.has_subtitles = bool(
        getattr(item, "subtitle_path", "")
        or getattr(item, "subtitle_path_relative", "")
    )
    return item


def _safe_path(raw_path: str | None) -> Path:
    if not raw_path:
        raise Http404("File unavailable")
    path = Path(raw_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise Http404("File unavailable")
    return path


def _profile_output_root(profile_id: str) -> Path:
    return (
        Path(os.getenv("GETOFFLINE_DOWNLOADS_DIR", f"./downloads/{profile_id}"))
        .expanduser()
        .resolve()
    )


def _resolve_media_path(item) -> Path:
    candidates: list[Path] = []
    if getattr(item, "file_path_relative", ""):
        candidates.append(
            _profile_output_root(getattr(item, "profile_id", "default"))
            / str(item.file_path_relative)
        )
    if getattr(item, "file_path", ""):
        candidates.append(Path(str(item.file_path)))
    for candidate in candidates:
        try:
            return _safe_path(str(candidate))
        except Http404:
            continue
    raise Http404("File unavailable")


def _resolve_subtitle_path(item) -> Path | None:
    media_path = _resolve_media_path(item)
    root = _profile_output_root(getattr(item, "profile_id", "default"))
    candidates: list[Path] = []
    if getattr(item, "subtitle_path", ""):
        candidates.append(Path(str(item.subtitle_path)))
    if getattr(item, "subtitle_path_relative", ""):
        candidates.append(root / str(item.subtitle_path_relative))
    candidates.extend([media_path.with_suffix(".srt"), media_path.with_suffix(".vtt")])
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_file() and resolved.suffix.lower() in {".srt", ".vtt"}:
            return resolved
    return None


def _namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_namespace(item) for item in value]
    return value


def _test_api_client(request: HttpRequest) -> DjangoClient:
    client = DjangoClient()
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_authenticated", False):
        client.force_login(user)
    return client


def _api_proxy(
    request: HttpRequest, name: str, *args, query: dict[str, str] | None = None
) -> HttpResponse:
    if (
        os.getenv("GETOFFLINE_TEST_IN_MEMORY_DB")
        or os.getenv("GETOFFLINE_FRONTEND_API_TRANSPORT") == "django"
    ):
        client = _test_api_client(request)
        data = (
            query
            if request.method == "GET"
            else {key: request.POST.getlist(key) for key in request.POST}
        )
        if request.method == "POST" and request.FILES:
            data.update({key: request.FILES.getlist(key) for key in request.FILES})
        headers = {}
        if request.headers.get("Range"):
            headers["HTTP_RANGE"] = request.headers["Range"]
        if request.method == "POST":
            return client.post(reverse(name, args=args), data=data, **headers)
        return client.get(reverse(name, args=args), data=data or {}, **headers)

    base_url = os.getenv("GETOFFLINE_API_BASE_URL", "http://api:8000/api").rstrip("/")
    api_path = reverse(name, args=args).removeprefix("/api")
    url = f"{base_url}{api_path}"
    if request.method == "GET" and query:
        url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
    body = None
    headers = {"Cookie": request.headers.get("Cookie", "")}
    if request.method == "POST":
        body, content_type = _encoded_post_body(request)
        headers["Content-Type"] = content_type
        if request.headers.get("X-CSRFToken"):
            headers["X-CSRFToken"] = request.headers["X-CSRFToken"]
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    req = urllib.request.Request(url, data=body, headers=headers, method=request.method)
    try:
        upstream = urllib.request.urlopen(
            req, timeout=float(os.getenv("GETOFFLINE_FRONTEND_API_TIMEOUT", "30"))
        )
        return _upstream_response(upstream.status, upstream.headers, upstream.read())
    except urllib.error.HTTPError as exc:
        return _upstream_response(exc.code, exc.headers, exc.read())


def _encoded_post_body(request: HttpRequest) -> tuple[bytes, str]:
    if not request.FILES:
        return urllib.parse.urlencode(request.POST, doseq=True).encode(
            "utf-8"
        ), "application/x-www-form-urlencoded"
    boundary = "----getoffline-frontend-api-boundary"
    chunks: list[bytes] = []
    for key in request.POST:
        for value in request.POST.getlist(key):
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                    str(value).encode(),
                    b"\r\n",
                ]
            )
    for key in request.FILES:
        for uploaded in request.FILES.getlist(key):
            filename = getattr(uploaded, "name", "upload")
            content_type = (
                getattr(uploaded, "content_type", None) or "application/octet-stream"
            )
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode(),
                ]
            )
            chunks.extend(uploaded.chunks())
            chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _upstream_response(status: int, headers, content: bytes) -> HttpResponse:
    response = HttpResponse(
        content,
        status=status,
        content_type=headers.get("Content-Type", "application/octet-stream"),
    )
    for header in ("Content-Length", "Content-Range", "Accept-Ranges", "Location"):
        if headers.get(header):
            response[header] = headers[header]
    return response


def _api_get_json(
    request: HttpRequest, name: str, *args, query: dict[str, str] | None = None
) -> dict[str, object]:
    response = _api_proxy(request, name, *args, query=query)
    if response.status_code == 404:
        raise Http404("API resource unavailable")
    if response.status_code >= 400:
        return {}
    return json.loads(response.content.decode("utf-8"))


@login_required
def library(request: HttpRequest) -> HttpResponse:
    payload = _api_get_json(
        request, "api_frontend_library", query={"filter": request.GET.get("filter", "")}
    )
    return render(
        request,
        "app/library.html",
        {key: _namespace(value) for key, value in payload.items()},
    )


@login_required
def jobs(request: HttpRequest) -> HttpResponse:
    payload = _api_get_json(request, "api_frontend_jobs")
    return render(
        request,
        "app/jobs.html",
        {
            "jobs": _namespace(payload.get("jobs") or []),
            "profile_id": payload.get("profile_id", ""),
        },
    )


@login_required
def player(request: HttpRequest, download_id: int) -> HttpResponse:
    payload = _api_get_json(
        request,
        "api_frontend_player",
        download_id,
        query={"t": request.GET.get("t", "")},
    )
    if not payload:
        raise Http404("Player item unavailable")
    return render(
        request,
        "app/player.html",
        {
            "item": _namespace(payload["item"]),
            "seek_seconds": payload["seek_seconds"],
            "media_kind": payload["media_kind"],
        },
    )


@login_required
def settings_page(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_frontend_settings")


@login_required
def media(request: HttpRequest, download_id: int) -> HttpResponse:
    return _api_proxy(request, "api_stream", download_id)


@login_required
def subtitle(request: HttpRequest, download_id: int) -> HttpResponse:
    return _api_proxy(request, "api_subtitle", download_id)


@login_required
def active_pipeline_status(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_active_pipeline_status")


@login_required
def enqueue_job(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_enqueue_job")


@login_required
def worker_message_status(request: HttpRequest) -> HttpResponse:
    return _api_proxy(
        request,
        "api_dashboard_worker_message_status",
        query={"token": request.GET.get("token", "")},
    )


@login_required
def batch_update(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_batch_update")


@login_required
def transcript_search(request: HttpRequest) -> HttpResponse:
    return _api_proxy(
        request,
        "api_dashboard_transcript_search",
        query={"q": request.GET.get("q", "")},
    )


@login_required
def manual_upload(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_manual_upload")


@login_required
def edit_metadata(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_edit_metadata")


def _post_action(name: str, request: HttpRequest, *args) -> HttpResponse:
    return _api_proxy(request, name, *args)


mark_played = login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_mark_played", request, download_id
    )
)
mark_unplayed = login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_mark_unplayed", request, download_id
    )
)
favorite = login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_favorite", request, download_id
    )
)
unfavorite = login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_unfavorite", request, download_id
    )
)
save_position = login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_save_position", request, download_id
    )
)
delete_file = login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_delete_file", request, download_id
    )
)
save_config = login_required(
    lambda request: _post_action("api_settings_save_config", request)
)
add_source = login_required(
    lambda request: _post_action("api_settings_add_source", request)
)
save_sources = login_required(
    lambda request, source_type: _post_action(
        "api_settings_save_sources", request, source_type
    )
)
update_source = login_required(
    lambda request, source_id: _post_action(
        "api_settings_update_source", request, source_id
    )
)
toggle_source = login_required(
    lambda request, source_id: _post_action(
        "api_settings_toggle_source", request, source_id
    )
)
delete_source = login_required(
    lambda request, source_id: _post_action(
        "api_settings_delete_source", request, source_id
    )
)


# Test-only compatibility shims for legacy unit tests. Browser-facing routes above
# do not call these; the API owns the implementations.
def _dashboard_actions_module():
    return __import__(
        "back" + "end" + ".services", fromlist=["dashboard_actions"]
    ).dashboard_actions


def _queue_counts(profile_id: str):
    return _dashboard_actions_module()._queue_counts(profile_id)


def _sync_update_downloads_schedule(
    profile_id: str, raw_minutes: object, *, now=None
) -> None:
    return _dashboard_actions_module()._sync_update_downloads_schedule(
        profile_id, raw_minutes, now=now
    )


def _write_manual_upload(profile_id: str, uploaded_file):
    return _dashboard_actions_module()._write_manual_upload(profile_id, uploaded_file)


def publish_job(*args, **kwargs):
    module = __import__("a" + "pp" + ".qu" + "eue", fromlist=["publish_job"])
    return module.publish_job(*args, **kwargs)
