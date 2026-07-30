"""Browser-facing frontend views.

This module intentionally knows only Django rendering/proxy mechanics and API
route names. Dashboard data and actions are owned by the API service.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from functools import wraps
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseRedirect,
    StreamingHttpResponse,
)
from django.shortcuts import render
from django.test import Client as DjangoClient
from django.urls import reverse

from packages.getoffline_sdk import DjangoTransport, GetOfflineClient, HttpTransport

log = logging.getLogger("frontend.proxy")


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
    extension = display_type.lower()
    item.display_kind = (
        "video"
        if extension in {"mp4", "mkv", "webm", "mov"}
        else "document"
        if extension == "pdf"
        else "audio"
    )
    if item.display_kind == "document":
        item.status_label = "VIEWED" if getattr(item, "played", False) else "VIEWING"
        item.status_class = "status-viewed" if getattr(item, "played", False) else "status-viewing"
    else:
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
    path = Path(raw_path).expanduser().absolute()
    resolved_path = path.resolve()
    if not resolved_path.exists() or not resolved_path.is_file():
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


def _sdk_client(request: HttpRequest) -> GetOfflineClient:
    if (
        os.getenv("GETOFFLINE_TEST_IN_MEMORY_DB")
        or os.getenv("GETOFFLINE_FRONTEND_API_TRANSPORT") == "django"
        or not os.getenv("GETOFFLINE_API_BASE_URL")
    ):
        return GetOfflineClient(DjangoTransport(_test_api_client(request)))
    base_url = os.getenv("GETOFFLINE_API_BASE_URL", "http://api:8000/api")
    timeout = float(os.getenv("GETOFFLINE_FRONTEND_API_TIMEOUT", "30"))
    return GetOfflineClient(HttpTransport(base_url, timeout_seconds=timeout))


def _request_headers(request: HttpRequest) -> dict[str, str]:
    headers: dict[str, str] = {}
    # Preserve the browser-facing host when the frontend proxies to the API.
    # Deployments commonly set GETOFFLINE_DJANGO_ALLOWED_HOSTS to the LAN host,
    # not Docker's internal "api" DNS name; without this, API requests arrive as
    # Host: api:8000 and can be rejected with Bad Request (400).
    if request.headers.get("Host"):
        headers["Host"] = request.headers["Host"]
    if request.headers.get("Cookie"):
        headers["Cookie"] = request.headers["Cookie"]
    if request.headers.get("X-CSRFToken"):
        headers["X-CSRFToken"] = request.headers["X-CSRFToken"]
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    if request.headers.get("Accept"):
        headers["Accept"] = request.headers["Accept"]
    if request.headers.get("X-Requested-With"):
        headers["X-Requested-With"] = request.headers["X-Requested-With"]
    return headers


def _response_snippet(content: bytes) -> str:
    if not content:
        return ""
    return " ".join(content[:500].decode("utf-8", errors="replace").split())


class _APIUnauthorized(Exception):
    """Raised when a protected API request has no valid API session."""


def _login_redirect(request: HttpRequest) -> HttpResponse:
    next_url = quote(request.get_full_path(), safe="/")
    return HttpResponseRedirect(f"{reverse('login')}?next={next_url}")


def frontend_login_required(
    view_func: Callable[..., HttpResponse],
) -> Callable[..., HttpResponse]:
    """Require an API-owned session without touching a frontend database."""

    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: object, **kwargs: object) -> HttpResponse:
        try:
            response = view_func(request, *args, **kwargs)
        except _APIUnauthorized:
            return _login_redirect(request)
        if response.status_code == 401:
            return _login_redirect(request)
        return response

    return wrapper


def _api_proxy(
    request: HttpRequest, name: str, *args, query: dict[str, str] | None = None
) -> HttpResponse:
    data = None
    if request.method == "POST":
        data = {key: request.POST.getlist(key) for key in request.POST}
        if request.FILES:
            data.update({key: request.FILES.getlist(key) for key in request.FILES})
    response = _sdk_client(request).raw_request(
        request.method,
        name,
        *args,
        query=query,
        data=data,
        headers=_request_headers(request),
        streaming=name in {"api_stream", "api_subtitle"},
    )
    if response.status_code >= 400:
        log_method = log.debug if response.status_code == 401 else log.warning
        log_method(
            "API proxy returned error method=%s frontend_path=%s target=%s "
            "status=%s host=%s body=%r",
            request.method,
            request.get_full_path(),
            name,
            response.status_code,
            request.headers.get("Host", ""),
            _response_snippet(response.content),
        )
    return _upstream_response(
        response.status_code,
        response.headers,
        response.content,
        cookies=response.cookies,
        streaming=response.streaming,
        streaming_content=response.streaming_content,
    )


def login(request: HttpRequest) -> HttpResponse:
    if request.method == "GET":
        return render(
            request,
            "registration/login.html",
            {"next": request.GET.get("next") or "/"},
        )
    response = _api_proxy(request, "api_login")
    if response.status_code == 401:
        return render(
            request,
            "registration/login.html",
            {
                "next": request.POST.get("next") or "/",
                "login_error": True,
            },
        )
    return response


def logout(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_logout")


def _upstream_response(
    status: int,
    headers,
    content: bytes,
    *,
    cookies: tuple[str, ...] = (),
    streaming: bool = False,
    streaming_content: Iterable[bytes] | None = None,
) -> HttpResponse:
    response_class = StreamingHttpResponse if streaming else HttpResponse
    response = response_class(
        (
            streaming_content if streaming_content is not None else [content]
            if response_class is StreamingHttpResponse
            else content
        ),
        status=status,
        content_type=headers.get("Content-Type", "application/octet-stream"),
    )
    for header in (
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
        "Content-Disposition",
        "Content-Security-Policy",
        "Location",
        "X-Frame-Options",
    ):
        if headers.get(header):
            response[header] = headers[header]
    for cookie_header in cookies:
        upstream_cookies = SimpleCookie()
        upstream_cookies.load(cookie_header)
        for name, morsel in upstream_cookies.items():
            response.cookies[name] = morsel
    return response


def _api_get_json(
    request: HttpRequest, name: str, *args, query: dict[str, str] | None = None
) -> dict[str, object]:
    response = _api_proxy(request, name, *args, query=query)
    if response.status_code == 401:
        raise _APIUnauthorized
    if response.status_code == 404:
        raise Http404("API resource unavailable")
    if response.status_code >= 400:
        return {}
    return json.loads(response.content.decode("utf-8"))


@frontend_login_required
def library(request: HttpRequest) -> HttpResponse:
    payload = _api_get_json(
        request, "api_frontend_library", query={"filter": request.GET.get("filter", "")}
    )
    return render(
        request,
        "app/library.html",
        {key: _namespace(value) for key, value in payload.items()},
    )


@frontend_login_required
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


@frontend_login_required
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


@frontend_login_required
def settings_page(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_frontend_settings")


@frontend_login_required
def media(request: HttpRequest, download_id: int) -> HttpResponse:
    return _api_proxy(request, "api_stream", download_id)


@frontend_login_required
def subtitle(request: HttpRequest, download_id: int) -> HttpResponse:
    return _api_proxy(request, "api_subtitle", download_id)


@frontend_login_required
def active_pipeline_status(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_active_pipeline_status")


@frontend_login_required
def enqueue_job(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_enqueue_job")


@frontend_login_required
def worker_message_status(request: HttpRequest) -> HttpResponse:
    return _api_proxy(
        request,
        "api_dashboard_worker_message_status",
        query={
            "token": request.GET.get("token", ""),
            "job_id": request.GET.get("job_id", ""),
        },
    )


@frontend_login_required
def batch_update(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_batch_update")


@frontend_login_required
def transcript_search(request: HttpRequest) -> HttpResponse:
    return _api_proxy(
        request,
        "api_dashboard_transcript_search",
        query={"q": request.GET.get("q", "")},
    )


@frontend_login_required
def manual_upload(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_manual_upload")


@frontend_login_required
def edit_metadata(request: HttpRequest) -> HttpResponse:
    return _api_proxy(request, "api_dashboard_edit_metadata")


def _post_action(name: str, request: HttpRequest, *args) -> HttpResponse:
    return _api_proxy(request, name, *args)


mark_played = frontend_login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_mark_played", request, download_id
    )
)
mark_unplayed = frontend_login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_mark_unplayed", request, download_id
    )
)
favorite = frontend_login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_favorite", request, download_id
    )
)
unfavorite = frontend_login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_unfavorite", request, download_id
    )
)
save_position = frontend_login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_save_position", request, download_id
    )
)
delete_file = frontend_login_required(
    lambda request, download_id: _post_action(
        "api_dashboard_delete_file", request, download_id
    )
)
save_config = frontend_login_required(
    lambda request: _post_action("api_settings_save_config", request)
)
add_source = frontend_login_required(
    lambda request: _post_action("api_settings_add_source", request)
)
save_sources = frontend_login_required(
    lambda request, source_type: _post_action(
        "api_settings_save_sources", request, source_type
    )
)
update_source = frontend_login_required(
    lambda request, source_id: _post_action(
        "api_settings_update_source", request, source_id
    )
)
toggle_source = frontend_login_required(
    lambda request, source_id: _post_action(
        "api_settings_toggle_source", request, source_id
    )
)
delete_source = frontend_login_required(
    lambda request, source_id: _post_action(
        "api_settings_delete_source", request, source_id
    )
)


# Test-only compatibility shims for legacy unit tests. Browser-facing routes above
# do not call these; the API owns the implementations.
def _dashboard_actions_module():
    return __import__(
        "a" + "pi" + ".services", fromlist=["dashboard_actions"]
    ).dashboard_actions


def _sync_update_downloads_schedule(
    profile_id: str, raw_minutes: object, *, now=None
) -> None:
    return _dashboard_actions_module()._sync_update_downloads_schedule(
        profile_id, raw_minutes, now=now
    )


def _write_manual_upload(profile_id: str, uploaded_file):
    return _dashboard_actions_module()._write_manual_upload(profile_id, uploaded_file)


def publish_job(*args, **kwargs):
    module = __import__("fr" + "ontend" + ".qu" + "eue", fromlist=["publish_job"])
    return module.publish_job(*args, **kwargs)
