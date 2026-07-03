"""Shared yt-dlp helpers used by the Django/RabbitMQ workers."""

import importlib
import os
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

from workers.logger import get_logger
from workers.utils import sanitize_channel_name

_EMOJI_RE = re.compile(r"[🇦-🇿🌀-🫿☀-➿️]+")
_YTDLP_REMOTE_COMPONENT = "ejs:github"
YoutubeDL = None

log = get_logger("ytdlp_helpers")


def _get_youtubedl():
    return YoutubeDL or importlib.import_module("yt_dlp").YoutubeDL


def _normalize_ytdlp_message(message: str) -> str:
    text = str(message or "").strip()
    if text.startswith("[youtube] "):
        return text[len("[youtube] ") :].strip()
    return text


class YoutubeDlQuietLogger:
    def __init__(self, run_stats: dict[str, int] | None = None):
        self.run_stats = run_stats if run_stats is not None else {}

    def _count(self, key: str):
        self.run_stats[key] = self.run_stats.get(key, 0) + 1

    def _record_message(self, message: str):
        lower = message.lower()
        if "[download] downloading item " in lower:
            self._count("playlist_item_announced")
        if "unavailable" in lower:
            self._count("messages_unavailable")
        if "private" in lower:
            self._count("messages_private")
        if "sign in" in lower or "age-restricted" in lower:
            self._count("messages_auth")

    def debug(self, msg):
        if not msg:
            return
        message = _normalize_ytdlp_message(msg)
        if not message:
            return
        self._record_message(message)
        if message.startswith("[debug]"):
            log.debug("%s", message)
        else:
            log.info("%s", message)

    def warning(self, msg):
        if msg:
            message = _normalize_ytdlp_message(msg)
            if message:
                self._record_message(message)
                self._count("warnings")
                log.warning("%s", message)

    def error(self, msg):
        if msg:
            message = _normalize_ytdlp_message(msg)
            if message:
                self._record_message(message)
                self._count("errors")
                log.error("%s", message)


def _resolve_quickjs_binary(js_runtime_path: str | None = None) -> str | None:
    candidate = str(js_runtime_path or "qjs").strip() or "qjs"
    if os.sep in candidate or (os.altsep and os.altsep in candidate):
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        return None
    return shutil.which(candidate)


def _prepend_runtime_to_path(runtime_binary: str) -> None:
    runtime_dir = str(Path(runtime_binary).parent)
    path_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    if runtime_dir not in path_parts:
        os.environ["PATH"] = os.pathsep.join([runtime_dir, *path_parts])


def enable_youtube_quickjs_remote_component(
    ydl_opts: dict, context_label: str, js_runtime_path: str | None = None
):
    """Configure yt-dlp's YouTube EJS remote component to use QuickJS when available."""
    quickjs_binary = _resolve_quickjs_binary(js_runtime_path)
    if not quickjs_binary:
        configured = str(js_runtime_path or "qjs").strip() or "qjs"
        log.warning(
            "QuickJS executable %r was not found; skipping yt-dlp EJS remote component for %s. If challenge solving fails, install the quickjs package or set the JavaScript runtime path in Settings.",
            configured,
            context_label,
        )
        return

    _prepend_runtime_to_path(quickjs_binary)
    ydl_opts["js_runtimes"] = {"quickjs": {"path": quickjs_binary}}

    existing_value = ydl_opts.get("remote_components")
    if isinstance(existing_value, list):
        components = existing_value
    elif isinstance(existing_value, str) and existing_value.strip():
        components = [part.strip() for part in existing_value.split(",") if part.strip()]
    else:
        components = []

    if _YTDLP_REMOTE_COMPONENT not in components:
        components.append(_YTDLP_REMOTE_COMPONENT)

    ydl_opts["remote_components"] = components
    log.info(
        "Enabled yt-dlp remote component %s for %s (quickjs runtime: %s)",
        _YTDLP_REMOTE_COMPONENT,
        context_label,
        quickjs_binary,
    )


def apply_ytdlp_player_js_variant_workaround(ydl_opts: dict):
    """Work around yt-dlp issue #16256 by forcing youtube:player_js_variant=main."""
    extractor_args = ydl_opts.get("extractor_args")
    if not isinstance(extractor_args, dict):
        extractor_args = {}

    youtube_args = extractor_args.get("youtube")
    if not isinstance(youtube_args, dict):
        youtube_args = {}

    youtube_args["player_js_variant"] = ["main"]
    extractor_args["youtube"] = youtube_args
    ydl_opts["extractor_args"] = extractor_args


def clean_log_title(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown title"
    text = _EMOJI_RE.sub("", text)
    text = " ".join(text.split())
    return text or "unknown title"


def extract_youtube_video_id(url: str | None) -> str | None:
    candidate = str(url or "").strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if host.endswith("youtu.be"):
        value = path.strip("/")
        return value or None
    if "youtube.com" in host:
        if path.startswith("/watch"):
            query_values = parse_qs(parsed.query or "")
            video_id = str((query_values.get("v") or [""])[0]).strip()
            return video_id or None
        if path.startswith("/shorts/") or path.startswith("/embed/"):
            parts = [segment for segment in path.split("/") if segment]
            if len(parts) >= 2:
                return parts[1].strip() or None
    return None


def resolve_youtube_source_name(url: str, cookie_file: str | None = None) -> str:
    source_url = str(url or "").strip()
    if not source_url:
        raise ValueError("Missing YouTube URL")
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": YoutubeDlQuietLogger(),
    }
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file
    enable_youtube_quickjs_remote_component(ydl_opts, "source-name resolution")
    apply_ytdlp_player_js_variant_workaround(ydl_opts)

    with _get_youtubedl()(ydl_opts) as ydl:
        info = ydl.extract_info(source_url, download=False)

    if info and isinstance(info, dict):
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            for entry in entries:
                if isinstance(entry, dict):
                    info = entry
                    break
        for key in ("channel", "uploader", "uploader_id"):
            value = str(info.get(key) or "").strip()
            if value:
                return sanitize_channel_name(value)
        title = str(info.get("title") or "").strip()
        if title:
            return sanitize_channel_name(title)
    return "youtube-single"
