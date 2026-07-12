"""Authenticated media/subtitle streaming helpers."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse

from models.models import Download
from backend.services.settings import profile_output_root

MEDIA_RANGE_CHUNK_SIZE = 64 * 1024
MEDIA_INITIAL_RANGE_SIZE = 1024 * 1024


def safe_path(raw_path: str | None) -> Path:
    if not raw_path:
        raise Http404("File unavailable")
    path = Path(raw_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise Http404("File unavailable")
    return path


def resolve_media_path(item: Download) -> Path:
    candidates: list[Path] = []
    if item.file_path_relative:
        candidates.append(profile_output_root(item.profile_id) / str(item.file_path_relative))
    if item.file_path:
        candidates.append(Path(str(item.file_path)))
    for candidate in candidates:
        try:
            return safe_path(str(candidate))
        except Http404:
            continue
    raise Http404("File unavailable")


def srt_to_vtt(content: str) -> str:
    lines = content.replace("\ufeff", "").splitlines()
    timestamp_re = re.compile(r"^(\d{2}:\d{2}:\d{2}),(\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}),(\d{3})(.*)$")
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


def resolve_subtitle_path(item: Download) -> Path | None:
    media_path = resolve_media_path(item)
    candidates: list[Path] = []
    if item.subtitle_path:
        candidates.append(Path(str(item.subtitle_path)))
    if item.subtitle_path_relative:
        candidates.append(profile_output_root(item.profile_id) / str(item.subtitle_path_relative))
    candidates.extend([media_path.with_suffix(".srt"), media_path.with_suffix(".vtt")])
    root = profile_output_root(item.profile_id)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_file() and resolved.suffix.lower() in {".srt", ".vtt"}:
            return resolved
    return None


def file_range_iterator(path: Path, start: int, length: int):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(MEDIA_RANGE_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def media_response(path: Path, range_header: str = "") -> HttpResponse:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    file_size = path.stat().st_size
    if range_header.startswith("bytes="):
        start_text, _, end_text = range_header.removeprefix("bytes=").partition("-")
        try:
            if start_text:
                start = int(start_text); requested_end = int(end_text) if end_text else None
            else:
                suffix_length = int(end_text) if end_text else file_size
                start = max(file_size - suffix_length, 0); requested_end = file_size - 1
        except ValueError:
            start, requested_end = 0, None
        start = max(0, min(start, file_size - 1))
        end = min(start + MEDIA_INITIAL_RANGE_SIZE - 1, file_size - 1) if requested_end is None else max(start, min(requested_end, file_size - 1))
        length = end - start + 1
        response = StreamingHttpResponse(file_range_iterator(path, start, length), status=206, content_type=content_type)
        response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        response["Content-Length"] = str(length)
    else:
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)
    response["Accept-Ranges"] = "bytes"
    return response


def subtitle_response(path: Path) -> HttpResponse:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".srt":
        text = srt_to_vtt(text)
    elif not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text
    return HttpResponse(text, content_type="text/vtt; charset=utf-8")
