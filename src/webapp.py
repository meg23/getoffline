import html
import json
import mimetypes
import os
import hashlib
import posixpath
import re
import gc
import resource
import shutil
import tracemalloc
import sqlite3
import sys
import threading
import traceback
import time
from collections import Counter
from datetime import datetime, timezone
from io import StringIO
from email.parser import BytesParser
from email.policy import default as email_policy
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from android_sync import AndroidSyncItem, config_from_defaults, delete_items_from_android, sync_items_to_android
from logger import get_logger
from summarization import ensure_local_summary_model, summarize_segments
from summary_tasks import clear_all_summaries, generate_missing_summaries
from subtitles import create_subtitles
from database import (
    resolve_download_artifact_path,
    add_source_config,
    delete_source_config,
    delete_download_entry,
    get_stored_config,
    get_download_position_seconds,
    get_total_listened_seconds,
    init_database,
    materialize_youtube_cookie_file,
    mark_all_downloads_played,
    mark_download_favorite,
    mark_download_played,
    set_source_enabled,
    update_source_config,
    update_download_settings,
    update_stored_defaults,
    update_download_position_seconds,
    update_download_positions_batch,
    close_cached_descriptors,
    upsert_download,
)


MEDIA_EXTENSIONS = {
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

VIDEO_EXTENSIONS = {"mp4", "mkv", "webm", "mov"}

DEFAULT_AUTO_UPDATE_MINUTES = 20
DISCONNECT_LOG_WINDOW_SECONDS = 30.0
SQLITE_PLAYBACK_TIMEOUT_SECONDS = 0.1
PROGRESS_FLUSH_COALESCE_SECONDS = 30.0
PROGRESS_FLUSH_POLL_SECONDS = 1.0
PROGRESS_MIN_DELTA_SECONDS = 5.0
DESCRIPTOR_CLEANUP_INTERVAL_SECONDS = 180
MEMORY_DIAGNOSTICS_INTERVAL_SECONDS = 60
HEAPDUMP_TOP_ALLOCATIONS = 250
IDLE_RSS_LOG_INTERVAL_SECONDS = 300
DEBUG_MEMORY_ENABLED = str(os.getenv("DEBUG_MEMORY", "")).strip().lower() in {"1", "true", "yes", "on"}
MEMORY_CEILING_MB = float(os.getenv("GETOFFLINE_MEMORY_CEILING_MB", "0") or "0")

log = get_logger("webapp")
_DISCONNECT_LOG_LOCK = threading.Lock()
_LAST_DISCONNECT_LOGGED_AT: Dict[str, float] = {}


@dataclass
class MediaRow:
    row_id: int
    source_type: str
    source_name: str
    item_url: Optional[str]
    title: str
    file_path: str
    file_ext: Optional[str]
    file_size_bytes: Optional[int]
    upload_date: Optional[str]
    played: bool
    favorite: bool = False
    played_at: Optional[str] = None
    last_position_seconds: float = 0.0
    subtitle_path: Optional[str] = None
    summary_text: Optional[str] = None
    raw_metadata_json: Optional[str] = None


@dataclass
class UpdateStatus:
    lock: threading.Lock = field(default_factory=threading.Lock)
    is_running: bool = False
    last_started_at: Optional[float] = None
    last_finished_at: Optional[float] = None
    last_result: str = "idle"
    last_error: Optional[str] = None
    last_items_count: int = 0


@dataclass
class AndroidSyncStatus:
    lock: threading.Lock = field(default_factory=threading.Lock)
    is_running: bool = False
    last_started_at: Optional[float] = None
    last_finished_at: Optional[float] = None
    last_result: str = "idle"
    last_error: Optional[str] = None
    last_copied_count: int = 0
    last_skipped_count: int = 0


@dataclass
class AppState:
    output_root: Path
    database_path: Path
    config: Dict
    update_runner: Callable[[Dict, List[str]], None]
    update_status: UpdateStatus = field(default_factory=UpdateStatus)
    android_sync_status: AndroidSyncStatus = field(default_factory=AndroidSyncStatus)
    pending_progress_lock: threading.Lock = field(default_factory=threading.Lock)
    pending_progress: Dict[int, Tuple[float, bool]] = field(default_factory=dict)
    pending_progress_event: threading.Event = field(default_factory=threading.Event)
    progress_metrics_lock: threading.Lock = field(default_factory=threading.Lock)
    progress_received_count: int = 0
    progress_flush_count: int = 0
    progress_last_log_at: float = 0.0
    progress_last_reason: str = "unknown"
    progress_last_forced: bool = False


def _default_update_runner(config: Dict, downloaded_items: List[str]) -> None:
    from podcasts import download_podcasts
    from youtube import download_youtube_items

    download_youtube_items(config, downloaded_items)
    download_podcasts(config, downloaded_items)


def _icon_sprite() -> str:
    return """
    <svg aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden" focusable="false">
      <symbol id="bi-play-fill" viewBox="0 0 16 16"><path d="M11.596 8.697 6.233 11.86c-.54.318-1.233-.066-1.233-.697V4.837c0-.63.692-1.015 1.233-.697l5.363 3.163c.535.315.535 1.079 0 1.394z"/></symbol>
      <symbol id="bi-check2-circle" viewBox="0 0 16 16"><path d="M8 15A7 7 0 1 1 8 1a7 7 0 0 1 0 14zm3.354-8.646a.5.5 0 0 0-.708-.708L7.5 8.793 6.354 7.646a.5.5 0 1 0-.708.708l1.5 1.5a.5.5 0 0 0 .708 0l3.5-3.5z"/></symbol>
      <symbol id="bi-arrow-counterclockwise" viewBox="0 0 16 16"><path d="M8 3a5 5 0 1 1-4.546 2.914.5.5 0 1 1 .908.418A4 4 0 1 0 8 4h-.5a.5.5 0 0 1 0-1H8z"/><path d="M8 1.5a.5.5 0 0 1 .5.5v2.5H6a.5.5 0 0 1 0-1h1.793A5.5 5.5 0 1 0 13.5 9a.5.5 0 0 1 1 0A6.5 6.5 0 1 1 8 2V2a.5.5 0 0 1 .5-.5z"/></symbol>
      <symbol id="bi-arrow-repeat" viewBox="0 0 16 16"><path d="M2 2.5a.5.5 0 0 1 .5-.5h2a.5.5 0 0 1 0 1H3.707A5.5 5.5 0 0 1 13 6a.5.5 0 0 1-1 0 4.5 4.5 0 0 0-7.795-3.089L5.5 4.207a.5.5 0 0 1-.708.708l-2-2A.5.5 0 0 1 2 2.5z"/><path d="M14 13.5a.5.5 0 0 1-.5.5h-2a.5.5 0 0 1 0-1h.793A5.5 5.5 0 0 1 3 10a.5.5 0 0 1 1 0 4.5 4.5 0 0 0 7.795 3.089l-1.295-1.296a.5.5 0 1 1 .708-.707l2 2a.5.5 0 0 1 .146.414z"/></symbol>
      <symbol id="bi-download" viewBox="0 0 16 16"><path d="M.5 9.9a.5.5 0 0 1 .5.5v2.6a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2.6a.5.5 0 0 1 1 0v2.6a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2v-2.6a.5.5 0 0 1 .5-.5"/><path d="M7.646 11.854a.5.5 0 0 0 .708 0l3-3a.5.5 0 0 0-.708-.708L8.5 10.293V1.5a.5.5 0 0 0-1 0v8.793L5.354 8.146a.5.5 0 1 0-.708.708z"/></symbol>
      <symbol id="bi-eye" viewBox="0 0 16 16"><path d="M16 8s-3-5-8-5-8 5-8 5 3 5 8 5 8-5 8-5zM1.173 8a13.133 13.133 0 0 1 1.66-1.995C4.12 4.724 5.88 4 8 4s3.879.724 5.168 2.005A13.133 13.133 0 0 1 14.828 8c-.058.087-.122.183-.195.288-.335.48-.83 1.12-1.465 1.707C11.879 11.276 10.12 12 8 12s-3.879-.724-5.168-2.005A13.134 13.134 0 0 1 1.172 8z"/><path d="M8 5.5A2.5 2.5 0 1 0 8 10.5 2.5 2.5 0 0 0 8 5.5z"/></symbol>
      <symbol id="bi-eye-slash" viewBox="0 0 16 16"><path d="M13.359 11.238C12.124 12.33 10.384 13 8 13c-5 0-8-5-8-5a16.79 16.79 0 0 1 3.168-3.646L1.146 2.354a.5.5 0 1 1 .708-.708l13 13a.5.5 0 0 1-.708.708l-.787-.787z"/><path d="M11.297 9.176 6.824 4.703A3 3 0 0 1 11.297 9.176z"/><path d="M5.34 7.218 8.782 10.66A3 3 0 0 1 5.34 7.218z"/><path d="M7.646 3.007C7.764 3.002 7.882 3 8 3c5 0 8 5 8 5a17.362 17.362 0 0 1-2.363 2.955l-.723-.723A16.74 16.74 0 0 0 14.828 8c-.058-.087-.122-.183-.195-.288-.335-.48-.83-1.12-1.465-1.707C11.879 4.724 10.12 4 8 4c-.076 0-.152.001-.227.003l-.127-.996z"/></symbol>
      <symbol id="bi-gear" viewBox="0 0 16 16"><path d="M9.605 1.05c-.413-1.4-2.397-1.4-2.81 0l-.094.319a1.464 1.464 0 0 1-2.105.872l-.29-.17c-1.257-.736-2.66.667-1.924 1.924l.17.29c.446.764.003 1.74-.872 2.105l-.319.094c-1.4.413-1.4 2.397 0 2.81l.319.094c.875.365 1.318 1.34.872 2.105l-.17.29c-.736 1.257.667 2.66 1.924 1.924l.29-.17c.764-.446 1.74-.003 2.105.872l.094.319c.413 1.4 2.397 1.4 2.81 0l.094-.319c.365-.875 1.34-1.318 2.105-.872l.29.17c1.257.736 2.66-.667 1.924-1.924l-.17-.29a1.464 1.464 0 0 1 .872-2.105l.319-.094c1.4-.413 1.4-2.397 0-2.81l-.319-.094a1.464 1.464 0 0 1-.872-2.105l.17-.29c.736-1.257-.667-2.66-1.924-1.924l-.29.17a1.464 1.464 0 0 1-2.105-.872l-.094-.319zM8 10.5A2.5 2.5 0 1 1 8 5.5a2.5 2.5 0 0 1 0 5z"/></symbol>
      <symbol id="bi-plus-lg" viewBox="0 0 16 16"><path fill-rule="evenodd" d="M8 2.5a.5.5 0 0 1 .5.5v4.5H13a.5.5 0 0 1 0 1H8.5V13a.5.5 0 0 1-1 0V8.5H3a.5.5 0 0 1 0-1h4.5V3a.5.5 0 0 1 .5-.5"/></symbol>
      <symbol id="bi-search" viewBox="0 0 16 16"><path d="M11.742 10.344a6.5 6.5 0 1 0-1.397 1.398h-.001l3.85 3.85a1 1 0 0 0 1.415-1.414l-3.867-3.834zm-5.242.656a5 5 0 1 1 0-10 5 5 0 0 1 0 10"/></symbol>
      <symbol id="bi-heart" viewBox="0 0 16 16"><path d="m8 2.748-.717-.737C5.6.281 2.514.878 1.4 3.053c-.523 1.023-.641 2.5.314 4.385.92 1.815 2.834 3.989 6.286 6.357 3.452-2.368 5.365-4.542 6.286-6.357.955-1.886.838-3.362.314-4.385C13.486.878 10.4.28 8.717 2.01z"/></symbol>
      <symbol id="bi-heart-fill" viewBox="0 0 16 16"><path fill-rule="evenodd" d="M8 1.314C12.438-3.248 23.534 4.735 8 15-7.534 4.736 3.562-3.248 8 1.314"/></symbol>
      <symbol id="bi-trash" viewBox="0 0 16 16"><path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5m2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5m3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0z"/><path d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1 0-2H5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1zM4.118 4 4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4zM2.5 3h11z"/></symbol>
    </svg>
    """


def _icon_use(icon_id: str) -> str:
    return f'<svg class="bi" aria-hidden="true" focusable="false"><use href="#{icon_id}"></use></svg>'


def _human_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "unknown"

    size = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num_bytes} B"




def _human_duration(total_seconds: float) -> str:
    seconds = max(0, int(total_seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    parts = []
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)

def _is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTENSIONS


def _normalize_stem(value: str) -> str:
    normalized = re.sub(r"\.{2,}", ".", str(value or "")).rstrip(". ")
    normalized = re.sub(r"^(?:manual-\d{8}-\d{6}(?:-\d+)?-)+", "", normalized, flags=re.IGNORECASE)
    return normalized or "item"


def _import_dropped_media_file(state: AppState, file_name: str, payload: bytes) -> None:
    if not file_name:
        raise ValueError("Missing filename")
    suffix = Path(file_name).suffix.lower()
    if suffix not in MEDIA_EXTENSIONS:
        raise ValueError("Unsupported media type")
    if not payload:
        raise ValueError("Empty file payload")

    destination_root = (state.output_root.expanduser().resolve() / "manual")
    destination_root.mkdir(parents=True, exist_ok=True)
    stem = _normalize_stem(Path(file_name).stem)
    destination_name = f"{stem}{suffix}"
    destination_path = destination_root / destination_name
    counter = 1
    while destination_path.exists():
        destination_path = destination_root / f"{stem}-{counter}{suffix}"
        counter += 1

    destination_path.write_bytes(payload)
    stat = destination_path.stat()
    checksum = hashlib.sha1(payload).hexdigest()
    now_iso = datetime.now(timezone.utc).isoformat()
    item_uid = f"manual-{checksum}-{int(stat.st_size)}"
    metadata = {
        "source_type": "manual",
        "source_name": "Manual Uploads",
        "source_url": None,
        "item_uid": item_uid,
        "item_id": item_uid,
        "item_url": None,
        "media_url": None,
        "title": stem,
        "description": "Imported via browser drag-and-drop",
        "uploader": "local",
        "channel": "Manual Uploads",
        "extractor": "browser-drop",
        "playlist_id": None,
        "playlist_title": None,
        "upload_date": now_iso[:10],
        "duration_seconds": None,
        "file_path": str(destination_path),
        "file_ext": suffix.lstrip("."),
        "file_size_bytes": int(stat.st_size),
        "expected_bytes": int(stat.st_size),
        "format_id": None,
        "format_note": "manual import",
        "audio_codec": None,
        "video_codec": None,
        "resolution": None,
        "fps": None,
        "subtitle_enabled": False,
        "subtitle_path": None,
        "download_status": "downloaded",
        "error_message": None,
        "raw_metadata": {
            "ingested_at": now_iso,
            "ingest_method": "drag-and-drop",
            "original_filename": file_name,
            "sha1": checksum,
        },
        "storage_root": str(destination_root),
    }
    upsert_download(str(state.database_path), metadata)
    _postprocess_imported_media(state, item_uid=item_uid, media_path=destination_path)
    return None


def _import_dropped_media_stream(state: AppState, file_name: str, stream, total_bytes: int) -> None:
    if not file_name:
        raise ValueError("Missing filename")
    suffix = Path(file_name).suffix.lower()
    if suffix not in MEDIA_EXTENSIONS:
        raise ValueError("Unsupported media type")
    if total_bytes <= 0:
        raise ValueError("Empty file payload")

    destination_root = (state.output_root.expanduser().resolve() / "manual")
    destination_root.mkdir(parents=True, exist_ok=True)
    stem = _normalize_stem(Path(file_name).stem)
    destination_path = destination_root / f"{stem}{suffix}"
    counter = 1
    while destination_path.exists():
        destination_path = destination_root / f"{stem}-{counter}{suffix}"
        counter += 1

    hasher = hashlib.sha1()
    bytes_written = 0
    with destination_path.open("wb") as out:
        remaining = total_bytes
        while remaining > 0:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            out.write(chunk)
            hasher.update(chunk)
            chunk_len = len(chunk)
            bytes_written += chunk_len
            remaining -= chunk_len
    if bytes_written <= 0:
        destination_path.unlink(missing_ok=True)
        raise ValueError("Empty file payload")

    now_iso = datetime.now(timezone.utc).isoformat()
    item_uid = f"manual-{hasher.hexdigest()}-{bytes_written}"
    metadata = {
        "source_type": "manual",
        "source_name": "Manual Uploads",
        "item_uid": item_uid,
        "item_id": item_uid,
        "title": stem,
        "description": "Imported via browser drag-and-drop",
        "uploader": "local",
        "channel": "Manual Uploads",
        "extractor": "browser-drop",
        "upload_date": now_iso[:10],
        "file_path": str(destination_path),
        "file_ext": suffix.lstrip("."),
        "file_size_bytes": int(bytes_written),
        "expected_bytes": int(bytes_written),
        "format_note": "manual import",
        "subtitle_enabled": False,
        "download_status": "downloaded",
        "raw_metadata": {
            "ingested_at": now_iso,
            "ingest_method": "drag-and-drop",
            "original_filename": file_name,
            "sha1": hasher.hexdigest(),
        },
        "storage_root": str(destination_root),
    }
    upsert_download(str(state.database_path), metadata)
    _postprocess_imported_media(state, item_uid=item_uid, media_path=destination_path)


def _postprocess_imported_media(state: AppState, item_uid: str, media_path: Path) -> None:
    defaults = (state.config or {}).get("defaults") or {}
    subtitle_mode = str(defaults.get("subtitle_transcription_mode") or "subprocess")
    subtitle_offset = defaults.get("subtitle_time_offset_seconds")
    try:
        subtitle_path = create_subtitles(
            media_file=Path(media_path),
            subtitle_offset_seconds=subtitle_offset,
            entry_subtitles_enabled=True,
            logger=log,
            context_name="manual-upload",
            context_label="manual",
            subtitle_transcription_mode=subtitle_mode,
        )
    except Exception as exc:
        log.warning("Post-import subtitle generation failed item_uid=%s error=%s", item_uid, exc)
        subtitle_path = None

    if subtitle_path:
        with sqlite3.connect(str(state.database_path)) as conn:
            output_root = state.output_root.expanduser().resolve()
            subtitle_relative = None
            try:
                subtitle_relative = str(Path(subtitle_path).resolve().relative_to(output_root))
            except ValueError:
                subtitle_relative = None
            conn.execute(
                """
                UPDATE downloads
                SET subtitle_enabled = 1,
                    subtitle_path = ?,
                    subtitle_path_relative = ?,
                    last_seen_at = ?
                WHERE source_type = 'manual' AND source_name = 'Manual Uploads' AND item_uid = ?
                """,
                (
                    str(subtitle_path),
                    subtitle_relative,
                    datetime.now(timezone.utc).isoformat(),
                    str(item_uid),
                ),
            )
            conn.commit()
        try:
            generate_missing_summaries(
                str(state.database_path),
                limit=1,
                model_name=str(defaults.get("summary_model") or "qwen2.5:0.5b"),
                timeout_seconds=int(defaults.get("summary_timeout_seconds") or 90),
            )
        except Exception as exc:
            log.warning("Post-import summary generation failed item_uid=%s error=%s", item_uid, exc)


def _extract_multipart_file(content_type: str, body: bytes, field_name: str) -> Tuple[str, bytes]:
    if "boundary=" not in content_type:
        raise ValueError("Missing multipart boundary")
    message_bytes = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + body
    )
    msg = BytesParser(policy=email_policy).parsebytes(message_bytes)
    for part in msg.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        if part.get_param("name", header="content-disposition") != field_name:
            continue
        filename = str(part.get_filename() or "").strip()
        payload = part.get_payload(decode=True) or b""
        return filename, payload
    raise ValueError(f"Missing {field_name}")


def _update_download_metadata(db_path: Path, row_id: int, title: str, source_name: str) -> bool:
    cleaned_title = str(title or "").strip()
    cleaned_source_name = str(source_name or "").strip()
    if not cleaned_title or not cleaned_source_name:
        return False
    now_iso = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        cur = conn.execute(
            "UPDATE downloads SET title = ?, source_name = ?, last_seen_at = ? WHERE id = ?",
            (cleaned_title, cleaned_source_name, now_iso, int(row_id)),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0


def _is_sqlite_lock_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "database is locked" in text or "database table is locked" in text


def _is_sqlite_open_error(exc: Exception) -> bool:
    return "unable to open database file" in str(exc or "").lower()


def _log_sqlite_lock(operation: str, exc: Exception) -> None:
    if _is_sqlite_lock_error(exc):
        log.warning("SQLite lock while %s: %s", operation, exc)


def _safe_resolve_path(path: Path) -> Path:
    expanded = path.expanduser()
    try:
        return expanded.resolve(strict=False)
    except OSError:
        return expanded.absolute()


def _sqlite_open_diagnostic_context(db_path: Path) -> str:
    expanded = db_path.expanduser()
    parent = expanded.parent
    resolved = _safe_resolve_path(expanded)
    return (
        f"db={expanded} resolved={resolved} exists={expanded.exists()} "
        f"parent_exists={parent.exists()} parent_writable={os.access(parent, os.W_OK)} "
        f"cwd={Path.cwd()} pid={os.getpid()}"
    )


def _fallback_database_path(db_path: Path, output_root: Optional[Path]) -> Optional[Path]:
    candidate_root = (output_root or db_path.parent).expanduser()
    candidate_db_path = _safe_resolve_path(candidate_root / "downloads.sqlite3")
    requested_db_path = _safe_resolve_path(db_path)
    if candidate_db_path == requested_db_path:
        return None

    try:
        init_database(str(candidate_db_path))
    except sqlite3.OperationalError as exc:
        if _is_sqlite_open_error(exc):
            return None
        raise

    log.warning("Switching to fallback database path after open failure: %s", candidate_db_path)
    return candidate_db_path


def _is_playback_completion_reason(reason: str) -> bool:
    value = str(reason or "").strip().lower()
    return value in {"ended", "mini-ended"}


def _is_async_request(handler) -> bool:
    requested_with = str(handler.headers.get("X-Requested-With") or "").strip().lower()
    return requested_with == "fetch"


def _resolve_safe_media_path(output_root: Path, candidate_path: str) -> Optional[Path]:
    root = output_root.expanduser().resolve()
    raw = Path(candidate_path).expanduser()

    candidates = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(root / raw)
        candidates.append(raw)

    for candidate in candidates:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if not resolved.exists() or not resolved.is_file() or not _is_media_file(resolved):
            continue
        return resolved
    return None


def _repair_downloaded_file_paths(db_path: Path, output_root: Path) -> None:
    root = output_root.expanduser().resolve()
    try:
        with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
            stale_rows = conn.execute(
                """
                SELECT id, file_path, file_path_relative
                FROM downloads
                WHERE download_status = 'downloaded' AND (COALESCE(file_path, '') != '' OR COALESCE(file_path_relative, '') != '')
                """
            ).fetchall()

            updates = []
            for row_id, file_path, file_path_relative in stale_rows:
                resolved_reference = resolve_download_artifact_path(str(root), file_path, file_path_relative)
                if resolved_reference and _resolve_safe_media_path(root, resolved_reference):
                    continue
                if not file_path:
                    continue

                raw = Path(file_path).expanduser()
                candidate_bases = [raw] if raw.is_absolute() else [root / raw, raw]

                repaired_path = None
                for base in candidate_bases:
                    normalized_name = f"{_normalize_stem(base.stem)}{base.suffix}"
                    normalized_candidate = base.with_name(normalized_name).resolve()
                    try:
                        normalized_candidate.relative_to(root)
                    except ValueError:
                        continue
                    if normalized_candidate.exists() and normalized_candidate.is_file() and _is_media_file(normalized_candidate):
                        repaired_path = str(normalized_candidate)
                        break

                if repaired_path:
                    try:
                        repaired_relative = str(Path(repaired_path).resolve().relative_to(root))
                    except ValueError:
                        repaired_relative = None
                    updates.append((repaired_path, repaired_relative, int(row_id)))

            if updates:
                conn.executemany("UPDATE downloads SET file_path = ?, file_path_relative = ? WHERE id = ?", updates)
                conn.commit()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock("repairing downloaded file paths", exc)
        if not _is_sqlite_lock_error(exc):
            raise


def _infer_media_type_for_redownload(row: MediaRow) -> str:
    ext = str(row.file_ext or "").strip().lower().lstrip(".")
    if not ext:
        ext = Path(str(row.file_path or "")).suffix.lower().lstrip(".")
    return "video" if ext in VIDEO_EXTENSIONS else "audio"


def fetch_downloaded_media_rows(db_path: Path, output_root: Optional[Path] = None) -> List[MediaRow]:
    try:
        init_database(str(db_path))
    except sqlite3.OperationalError as exc:
        if _is_sqlite_open_error(exc):
            fallback = _fallback_database_path(db_path, output_root)
            if fallback is None:
                log.warning("Unable to open database while preparing downloaded media rows (db=%s): %s", db_path, exc)
                return []
            db_path = fallback
        else:
            raise
    repair_root = output_root or db_path.parent
    _repair_downloaded_file_paths(db_path, repair_root)

    try:
        with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
            rows = conn.execute(
                """
                SELECT d.id, d.source_type, d.source_name, d.item_url, COALESCE(d.title, ''), COALESCE(d.file_path, ''), COALESCE(d.file_path_relative, ''),
                       file_ext, file_size_bytes, upload_date, COALESCE(played, 0), COALESCE(favorite, 0),
                       played_at, COALESCE(last_position_seconds, 0), subtitle_path, COALESCE(subtitle_path_relative, ''), COALESCE(ms.summary_text, ''), COALESCE(d.raw_metadata_json, '')
                FROM downloads d
                LEFT JOIN media_summaries ms ON ms.download_id = d.id
                WHERE d.download_status = 'downloaded'
                ORDER BY d.last_seen_at DESC, d.id DESC
                """
            ).fetchall()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock("reading downloaded media rows", exc)
        if _is_sqlite_lock_error(exc):
            return []
        raise

    return [
        MediaRow(
            row_id=row[0],
            source_type=row[1],
            source_name=row[2],
            item_url=row[3],
            title=row[4],
            file_path=resolve_download_artifact_path(str(repair_root), row[5], row[6]) or row[5] or row[6],
            file_ext=row[7],
            file_size_bytes=row[8],
            upload_date=row[9],
            played=bool(row[10]),
            favorite=bool(row[11]),
            played_at=row[12],
            last_position_seconds=float(row[13] or 0.0),
            subtitle_path=resolve_download_artifact_path(str(repair_root), row[14], row[15]) or row[14] or row[15] or None,
            summary_text=row[16] or None,
            raw_metadata_json=row[17] or None,
        )
        for row in rows
    ]


def fetch_downloaded_media_row_by_id(db_path: Path, row_id: int) -> Optional[MediaRow]:
    try:
        init_database(str(db_path))
    except sqlite3.OperationalError as exc:
        if _is_sqlite_open_error(exc):
            log.warning(
                "Unable to open database while loading media row id=%s: %s (%s)",
                row_id,
                exc,
                _sqlite_open_diagnostic_context(db_path),
            )
            return None
        raise
    try:
        with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
            row = conn.execute(
                """
                SELECT d.id, d.source_type, d.source_name, d.item_url, COALESCE(d.title, ''), COALESCE(d.file_path, ''), COALESCE(d.file_path_relative, ''),
                       file_ext, file_size_bytes, upload_date, COALESCE(played, 0), COALESCE(favorite, 0),
                       played_at, COALESCE(last_position_seconds, 0), subtitle_path, COALESCE(subtitle_path_relative, ''), COALESCE(ms.summary_text, ''), COALESCE(d.raw_metadata_json, '')
                FROM downloads d
                LEFT JOIN media_summaries ms ON ms.download_id = d.id
                WHERE d.id = ? AND d.download_status = 'downloaded'
                LIMIT 1
                """,
                (int(row_id),),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock("reading downloaded media row by id", exc)
        if _is_sqlite_lock_error(exc):
            return None
        raise

    if row is None:
        return None

    return MediaRow(
        row_id=row[0],
        source_type=row[1],
        source_name=row[2],
        item_url=row[3],
        title=row[4],
        file_path=resolve_download_artifact_path(str(db_path.parent), row[5], row[6]) or row[5] or row[6],
        file_ext=row[7],
        file_size_bytes=row[8],
        upload_date=row[9],
        played=bool(row[10]),
        favorite=bool(row[11]),
        played_at=row[12],
        last_position_seconds=float(row[13] or 0.0),
        subtitle_path=resolve_download_artifact_path(str(db_path.parent), row[14], row[15]) or row[14] or row[15] or None,
        summary_text=row[16] or None,
        raw_metadata_json=row[17] or None,
    )


def _format_vtt_timestamp(value: float) -> str:
    value = max(0.0, float(value))
    hours = int(value // 3600)
    value -= hours * 3600
    minutes = int(value // 60)
    value -= minutes * 60
    seconds = int(value)
    millis = int(round((value - seconds) * 1000))

    if millis == 1000:
        millis = 0
        seconds += 1
    if seconds == 60:
        seconds = 0
        minutes += 1
    if minutes == 60:
        minutes = 0
        hours += 1

    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _parse_srt_timestamp(value: str) -> Optional[float]:
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    sec_parts = parts[2].split(",")
    if len(sec_parts) != 2:
        return None

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(sec_parts[0])
        millis = int(sec_parts[1])
    except ValueError:
        return None

    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def _parse_vtt_timecode(value: str) -> Optional[float]:
    token = str(value or "").strip()
    if not token:
        return None
    token = token.replace(",", ".")
    parts = token.split(":")
    if len(parts) == 3:
        hh, mm, ss = parts
    elif len(parts) == 2:
        hh = "0"
        mm, ss = parts
    else:
        return None
    try:
        hours = int(hh)
        minutes = int(mm)
        seconds = float(ss)
    except ValueError:
        return None
    return max(0.0, hours * 3600 + minutes * 60 + seconds)


def _srt_to_vtt(content: str) -> str:
    lines = content.replace("\ufeff", "").splitlines()
    timestamp_re = re.compile(
        r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})(.*)$"
    )

    out_lines = ["WEBVTT", ""]
    for line in lines:
        match = timestamp_re.match(line)
        if not match:
            if line.strip().isdigit():
                continue
            out_lines.append(line)
            continue

        start_raw, end_raw, tail = match.groups()
        start = _parse_srt_timestamp(start_raw)
        end = _parse_srt_timestamp(end_raw)
        if start is None or end is None:
            continue
        out_lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}{tail}")

    return "\n".join(out_lines).strip() + "\n"


def _resolve_safe_subtitle_path(output_root: Path, row: MediaRow, media_path: Path) -> Optional[Path]:
    candidate_paths = []
    subtitle_path = getattr(row, "subtitle_path", None)
    if subtitle_path:
        candidate_paths.append(Path(subtitle_path))
    candidate_paths.append(media_path.with_suffix(".srt"))
    candidate_paths.append(media_path.with_suffix(".vtt"))

    root = output_root.expanduser().resolve()
    for candidate in candidate_paths:
        resolved = candidate.expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if not resolved.is_file() or resolved.suffix.lower() not in {".srt", ".vtt"}:
            continue
        return resolved
    return None


def _subtitle_segments_from_path(subtitle_path: Path) -> List[Tuple[float, float, str]]:
    subtitle_text = subtitle_path.read_text(encoding="utf-8", errors="replace")
    if subtitle_path.suffix.lower() == ".srt":
        subtitle_text = _srt_to_vtt(subtitle_text)
    lines = subtitle_text.splitlines()
    segments: List[Tuple[float, float, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" not in line:
            i += 1
            continue
        time_bits = line.split("-->")
        if len(time_bits) != 2:
            i += 1
            continue
        start = _parse_vtt_timecode(time_bits[0].strip())
        end = _parse_vtt_timecode(time_bits[1].strip().split(" ")[0])
        i += 1
        cue_lines = []
        while i < len(lines) and lines[i].strip():
            cue_lines.append(lines[i].strip())
            i += 1
        cue_text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", " ".join(cue_lines)))).strip()
        if cue_text:
            segments.append((start, end, cue_text))
    return segments


def _search_transcript_segments(db_path: Path, row: MediaRow, subtitle_path: Path, query_text: str) -> List[Dict[str, object]]:
    like_term = f"%{query_text.lower()}%"
    results: List[Tuple[float, float, str]] = []
    _ensure_transcript_index_for_row(db_path, row, subtitle_path)
    with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
        existing = conn.execute(
            """
            SELECT start_seconds, COALESCE(end_seconds, start_seconds), text
            FROM transcript_segments
            WHERE download_id = ? AND subtitle_path = ? AND lower(text) LIKE ?
            ORDER BY start_seconds ASC
            LIMIT 50
            """,
            (row.row_id, str(subtitle_path), like_term),
        ).fetchall()
        if existing:
            results = [(float(r[0]), float(r[1]), str(r[2])) for r in existing]

    return [{"start_seconds": s, "end_seconds": e, "text": t} for s, e, t in results]


def _search_transcripts_index(db_path: Path, query_text: str, limit: int = 50) -> List[Dict[str, object]]:
    if not query_text:
        return []
    like_term = f"%{query_text.lower()}%"
    with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
        rows = conn.execute(
            """
            SELECT ts.download_id, COALESCE(d.title, ''), ts.start_seconds, ts.text
            FROM transcript_segments ts
            JOIN downloads d ON d.id = ts.download_id
            WHERE lower(ts.text) LIKE ?
            ORDER BY ts.download_id DESC, ts.start_seconds ASC
            LIMIT ?
            """,
            (like_term, int(limit)),
        ).fetchall()
    return [
        {"row_id": int(row_id), "title": str(title), "start_seconds": float(start_seconds), "text": str(text)}
        for row_id, title, start_seconds, text in rows
    ]


def _ensure_transcript_index_for_row(db_path: Path, row: MediaRow, subtitle_path: Path) -> int:
    with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments WHERE download_id = ? AND subtitle_path = ?",
            (row.row_id, str(subtitle_path)),
        ).fetchone()[0]
        if existing_count:
            return int(existing_count)

        parsed_segments = _subtitle_segments_from_path(subtitle_path)
        if not parsed_segments:
            return 0
        conn.executemany(
            """
            INSERT OR IGNORE INTO transcript_segments (download_id, subtitle_path, start_seconds, end_seconds, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(row.row_id, str(subtitle_path), s, e, t) for s, e, t in parsed_segments],
        )
        conn.commit()
        return len(parsed_segments)


def _ensure_summary_for_row(db_path: Path, row: MediaRow, subtitle_path: Path) -> Optional[str]:
    with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
        existing = conn.execute(
            "SELECT summary_text FROM media_summaries WHERE download_id = ?",
            (row.row_id,),
        ).fetchone()
        if existing and existing[0]:
            return str(existing[0])
        segment_rows = conn.execute(
            """
            SELECT text FROM transcript_segments
            WHERE download_id = ? AND subtitle_path = ?
            ORDER BY start_seconds
            """,
            (row.row_id, str(subtitle_path)),
        ).fetchall()
    segment_texts = [str(item[0]) for item in segment_rows if item and item[0]]
    if not segment_texts:
        return None
    summary_model = str(state.config.get("defaults", {}).get("summary_model") or "qwen2.5:0.5b")
    summary_timeout_seconds = int((state.config.get("defaults", {}) or {}).get("summary_timeout_seconds") or 90)
    result = summarize_segments(
        segment_texts,
        model_name=summary_model,
        mode="subprocess",
        timeout_seconds=max(1, summary_timeout_seconds),
    )
    summary_text = str(result.get("summary_text") or "").strip()
    if not summary_text:
        log.warning("Summary generation returned empty output for row id=%s", row.row_id)
        return None
    model_name = str(result.get("model_name") or "unknown")
    log.info(
        "Generated summary for row id=%s using model=%s segments=%s chars=%s",
        row.row_id,
        model_name,
        len(segment_texts),
        len(summary_text),
    )
    with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
        conn.execute(
            """
            INSERT INTO media_summaries (download_id, summary_text, model_name, source_segment_count, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(download_id) DO UPDATE SET
              summary_text = excluded.summary_text,
              model_name = excluded.model_name,
              source_segment_count = excluded.source_segment_count,
              updated_at = excluded.updated_at
            """,
            (
                row.row_id,
                summary_text,
                model_name,
                len(segment_texts),
                str(result.get("updated_at") or datetime.now(timezone.utc).isoformat()),
            ),
        )
        conn.commit()
    return summary_text


def _index_transcripts_on_startup(state: AppState) -> None:
    try:
        rows = fetch_downloaded_media_rows(state.database_path, state.output_root)
    except Exception as exc:
        log.warning("Transcript startup indexing skipped (rows unavailable): %s", exc)
        return
    indexed_rows = 0
    indexed_segments = 0
    unindexed_candidates = 0
    log.info("Scanning for downloaded but unindexed transcripts...")
    for row in rows:
        media_path = _resolve_safe_media_path(state.output_root, row.file_path)
        if media_path is None:
            continue
        subtitle_path = _resolve_safe_subtitle_path(state.output_root, row, media_path)
        if subtitle_path is None:
            continue
        try:
            with sqlite3.connect(str(state.database_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
                existing_count = conn.execute(
                    "SELECT COUNT(*) FROM transcript_segments WHERE download_id = ? AND subtitle_path = ?",
                    (row.row_id, str(subtitle_path)),
                ).fetchone()[0]
        except Exception:
            existing_count = 0
        if not existing_count:
            unindexed_candidates += 1
        try:
            loaded = _ensure_transcript_index_for_row(state.database_path, row, subtitle_path)
        except Exception:
            continue
        try:
            _ensure_summary_for_row(state.database_path, row, subtitle_path)
        except Exception as exc:
            log.debug("Summary generation skipped for row id=%s: %s", row.row_id, exc)
        if loaded:
            indexed_rows += 1
            indexed_segments += loaded
    if indexed_rows:
        log.info("Transcript startup indexing complete: rows=%s segments=%s", indexed_rows, indexed_segments)
    else:
        log.info("Transcript startup indexing found no new rows to index.")
    log.info("Downloaded unindexed transcript candidates detected: %s", unindexed_candidates)


def _format_timestamp(ts: Optional[float]) -> str:
    if ts is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _snapshot_status(status: UpdateStatus) -> Dict[str, str]:
    with status.lock:
        return {
            "is_running": "yes" if status.is_running else "no",
            "last_started_at": _format_timestamp(status.last_started_at),
            "last_finished_at": _format_timestamp(status.last_finished_at),
            "last_result": status.last_result,
            "last_error": status.last_error or "none",
            "last_items_count": str(status.last_items_count),
        }


def _run_update_job(state: AppState) -> None:
    downloaded_items: List[str] = []
    with state.update_status.lock:
        state.update_status.is_running = True
        state.update_status.last_started_at = time.time()
        state.update_status.last_result = "running"
        state.update_status.last_error = None
        state.update_status.last_items_count = 0

    try:
        state.update_runner(state.config, downloaded_items)
        _index_transcripts_on_startup(state)
        trigger_android_sync(state, force=True)
        with state.update_status.lock:
            state.update_status.last_result = "ok"
            state.update_status.last_items_count = len(downloaded_items)
    except Exception as exc:
        with state.update_status.lock:
            state.update_status.last_result = "failed"
            state.update_status.last_error = str(exc)
    finally:
        with state.update_status.lock:
            state.update_status.is_running = False
            state.update_status.last_finished_at = time.time()


def trigger_background_update(state: AppState) -> bool:
    with state.update_status.lock:
        if state.update_status.is_running:
            return False

    thread = threading.Thread(target=_run_update_job, args=(state,), daemon=True)
    thread.start()
    return True


def _snapshot_android_sync_status(status: AndroidSyncStatus) -> Dict[str, str]:
    with status.lock:
        return {
            "is_running": "yes" if status.is_running else "no",
            "last_started_at": _format_timestamp(status.last_started_at),
            "last_finished_at": _format_timestamp(status.last_finished_at),
            "last_result": status.last_result,
            "last_error": status.last_error or "none",
            "last_copied_count": str(status.last_copied_count),
            "last_skipped_count": str(status.last_skipped_count),
        }



def _metadata_dict(raw_metadata_json: Optional[str]) -> Optional[dict]:
    if not raw_metadata_json:
        return None
    try:
        metadata = json.loads(raw_metadata_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata


def _thumbnail_score(thumbnail: dict) -> tuple:
    try:
        width = max(0, int(float(str(thumbnail.get("width") or "0").strip())))
    except (TypeError, ValueError):
        width = 0
    try:
        height = max(0, int(float(str(thumbnail.get("height") or "0").strip())))
    except (TypeError, ValueError):
        height = 0
    area = width * height
    if area <= 0:
        area = max(width, height)
    url = str(thumbnail.get("url") or "")
    return (area, max(width, height), len(url))


def _extract_artwork_url_from_metadata(raw_metadata_json: Optional[str]) -> Optional[str]:
    metadata = _metadata_dict(raw_metadata_json)
    if metadata is None:
        return None
    for key in ("artwork_url", "image_url", "thumbnail", "thumbnail_url"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    thumbnails = metadata.get("thumbnails")
    candidates = []
    if isinstance(thumbnails, list):
        for thumbnail in thumbnails:
            if isinstance(thumbnail, dict) and str(thumbnail.get("url") or "").strip():
                candidates.append(thumbnail)
    if candidates:
        best_thumbnail = max(candidates, key=_thumbnail_score)
        return str(best_thumbnail.get("url") or "").strip()
    return None


def _extract_artwork_path_from_metadata(raw_metadata_json: Optional[str], output_root: Path) -> Optional[Path]:
    metadata = _metadata_dict(raw_metadata_json)
    if metadata is None:
        return None
    root = output_root.expanduser().resolve()
    for key in ("artwork_path", "thumbnail_path"):
        value = str(metadata.get(key) or "").strip()
        if not value:
            continue
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.exists() and resolved.is_file():
            return resolved
    return None


def _resolve_safe_media_reference_path(output_root: Path, candidate_path: str) -> Optional[Path]:
    root = output_root.expanduser().resolve()
    raw = Path(candidate_path).expanduser()
    candidates = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(root / raw)
        candidates.append(raw)

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        return resolved
    return None


def _android_delete_item_from_row(row: MediaRow, output_root: Path) -> Optional[AndroidSyncItem]:
    media_path = (
        _resolve_safe_media_reference_path(output_root, row.file_path)
        if getattr(row, "file_path", None)
        else None
    )
    if media_path is None:
        return None
    subtitle_path = None
    subtitle_value = getattr(row, "subtitle_path", None)
    if subtitle_value:
        try:
            candidate_subtitle = Path(str(subtitle_value)).expanduser().resolve(strict=False)
            candidate_subtitle.relative_to(output_root.expanduser().resolve())
        except (OSError, ValueError):
            candidate_subtitle = None
        if candidate_subtitle is not None and candidate_subtitle.suffix.lower() in {".srt", ".vtt"}:
            subtitle_path = candidate_subtitle
    return AndroidSyncItem(
        row_id=row.row_id,
        title=row.title or media_path.stem,
        source_name=row.source_name or row.source_type or "GetOffline",
        file_path=media_path,
        subtitle_path=subtitle_path,
        position_seconds=max(0.0, float(getattr(row, "last_position_seconds", 0.0) or 0.0)),
    )


def _run_android_delete_job(state: AppState, rows: List[MediaRow]) -> None:
    defaults = state.config.get("defaults") or {}
    delete_config = config_from_defaults(defaults)
    delete_config.enabled = True
    items = []
    for row in rows:
        item = _android_delete_item_from_row(row, state.output_root)
        if item is not None:
            items.append(item)
    if not items:
        log.info("Android delete skipped after played mark: no Android delete items selected")
        return
    result = delete_items_from_android(items, delete_config)
    log.info(
        "Android delete after played mark completed: result=%s deleted=%s failed=%s device=%s",
        result.message,
        result.copied,
        result.failed,
        result.device_serial or "none",
    )


def _trigger_android_delete_for_rows(state: AppState, rows: List[MediaRow]) -> bool:
    if not rows:
        return False
    thread = threading.Thread(
        target=_run_android_delete_job,
        args=(state, list(rows)),
        daemon=True,
    )
    thread.start()
    return True


def _mark_download_played_from_webapp(state: AppState, row_id: int, played: bool = True) -> bool:
    row = fetch_downloaded_media_row_by_id(state.database_path, row_id) if played else None
    updated = mark_download_played(str(state.database_path), row_id, played=played)
    if updated and played and row is not None:
        _trigger_android_delete_for_rows(state, [row])
    return updated


def _mark_all_downloads_played_from_webapp(state: AppState) -> int:
    unplayed_rows = [
        row
        for row in fetch_downloaded_media_rows(state.database_path, state.output_root)
        if not row.played
    ]
    updated = mark_all_downloads_played(str(state.database_path))
    if updated:
        _trigger_android_delete_for_rows(state, unplayed_rows)
    return updated


def _android_sync_items_from_rows(
    rows: List[MediaRow],
    output_root: Path,
    max_items: int,
    *,
    include_unplayed: bool = True,
    include_started: bool = True,
    include_played: bool = False,
    exclude_regex: str = "",
) -> List[AndroidSyncItem]:
    items: List[AndroidSyncItem] = []
    exclude_pattern = None
    if exclude_regex:
        try:
            exclude_pattern = re.compile(str(exclude_regex), re.IGNORECASE)
        except re.error as exc:
            log.warning("Android sync exclusion regex ignored because it is invalid: %s", exc)
    for row in rows:
        position_seconds = max(0.0, float(getattr(row, "last_position_seconds", 0.0) or 0.0))
        is_started = bool(position_seconds > 0 and not row.played)
        if row.played:
            if not include_played:
                continue
        elif is_started:
            if not include_started:
                continue
        elif not include_unplayed:
            continue
        exclusion_text = " ".join(
            str(value or "")
            for value in (
                getattr(row, "title", ""),
                getattr(row, "source_name", ""),
                getattr(row, "source_type", ""),
                getattr(row, "file_path", ""),
                getattr(row, "item_url", ""),
            )
        )
        if exclude_pattern is not None and exclude_pattern.search(exclusion_text):
            continue
        media_path = _resolve_safe_media_path(output_root, row.file_path) if row.file_path else None
        if media_path is None:
            continue
        subtitle_path = _resolve_safe_subtitle_path(output_root, row, media_path)
        items.append(
            AndroidSyncItem(
                row_id=row.row_id,
                title=row.title or media_path.stem,
                source_name=row.source_name or row.source_type or "GetOffline",
                file_path=media_path,
                subtitle_path=subtitle_path,
                position_seconds=position_seconds,
                artwork_url=_extract_artwork_url_from_metadata(getattr(row, "raw_metadata_json", None)),
                artwork_path=_extract_artwork_path_from_metadata(getattr(row, "raw_metadata_json", None), output_root),
            )
        )
        if len(items) >= max(1, int(max_items or 1)):
            break
    return items


def _run_android_sync_job(state: AppState, force: bool = False) -> None:
    defaults = state.config.get("defaults") or {}
    sync_config = config_from_defaults(defaults)
    if force:
        sync_config.enabled = True
    with state.android_sync_status.lock:
        state.android_sync_status.is_running = True
        state.android_sync_status.last_started_at = time.time()
        state.android_sync_status.last_result = "running"
        state.android_sync_status.last_error = None
        state.android_sync_status.last_copied_count = 0
        state.android_sync_status.last_skipped_count = 0

    try:
        log.info(
            "Android sync job starting: force=%s enabled=%s max_items=%s destination=%s",
            "yes" if force else "no",
            "yes" if sync_config.enabled else "no",
            sync_config.max_items,
            sync_config.destination,
        )
        rows = fetch_downloaded_media_rows(state.database_path, state.output_root)
        items = _android_sync_items_from_rows(
            rows,
            state.output_root,
            sync_config.max_items,
            include_unplayed=sync_config.include_unplayed,
            include_started=sync_config.include_started,
            include_played=sync_config.include_played,
            exclude_regex=sync_config.exclude_regex,
        )
        log.info("Android sync job selected %s local item(s) from %s downloaded row(s)", len(items), len(rows))
        result = sync_items_to_android(items, sync_config)
        log.info(
            "Android sync job completed: result=%s copied=%s skipped=%s failed=%s device=%s",
            result.message,
            result.copied,
            result.skipped,
            result.failed,
            result.device_serial or "none",
        )
        with state.android_sync_status.lock:
            state.android_sync_status.last_result = result.message
            state.android_sync_status.last_error = "; ".join(result.errors[:3]) if result.errors else None
            state.android_sync_status.last_copied_count = result.copied
            state.android_sync_status.last_skipped_count = result.skipped
    except Exception as exc:
        log.exception("Android sync job failed unexpectedly: %s", exc)
        with state.android_sync_status.lock:
            state.android_sync_status.last_result = "failed"
            state.android_sync_status.last_error = str(exc)
    finally:
        with state.android_sync_status.lock:
            state.android_sync_status.is_running = False
            state.android_sync_status.last_finished_at = time.time()


def trigger_android_sync(state: AppState, *, force: bool = False) -> bool:
    defaults = state.config.get("defaults") or {}
    sync_config = config_from_defaults(defaults)
    if not force and not sync_config.enabled:
        log.info("Android sync trigger ignored: disabled")
        return False
    with state.android_sync_status.lock:
        if state.android_sync_status.is_running:
            log.info("Android sync trigger ignored: already running")
            return False

    thread = threading.Thread(target=_run_android_sync_job, args=(state, force), daemon=True)
    thread.start()
    log.info("Android sync trigger accepted: force=%s", "yes" if force else "no")
    return True


def _enqueue_progress_update(state: AppState, row_id: int, position_seconds: float, reason: str = "unknown", forced: bool = False) -> None:
    safe_seconds = max(0.0, float(position_seconds or 0.0))
    completion = _is_playback_completion_reason(reason)
    if completion:
        safe_seconds = 0.0

    with state.pending_progress_lock:
        existing = state.pending_progress.get(int(row_id))
        is_timeupdate = str(reason or "").strip().lower() in {"timeupdate", "mini-timeupdate"}
        if existing and not completion and not forced and is_timeupdate:
            if abs(float(existing[0]) - safe_seconds) < PROGRESS_MIN_DELTA_SECONDS:
                return
        if existing and existing[1] and not completion:
            # Keep completion resets sticky until they are flushed to storage.
            safe_seconds = float(existing[0])
        else:
            state.pending_progress[int(row_id)] = (safe_seconds, completion)
        queue_size = len(state.pending_progress)
        state.pending_progress_event.set()

    now = time.monotonic()
    with state.progress_metrics_lock:
        state.progress_received_count += 1
        state.progress_last_reason = str(reason or "unknown")
        state.progress_last_forced = bool(forced)
        received_count = state.progress_received_count
        should_log = (now - state.progress_last_log_at) >= 2.0
        if should_log:
            state.progress_last_log_at = now

    if should_log:
        log.info(
            "Progress enqueue stats: received=%s queue_size=%s latest_id=%s latest_seconds=%.3f reason=%s forced=%s",
            received_count,
            queue_size,
            int(row_id),
            safe_seconds,
            str(reason or "unknown"),
            "yes" if forced else "no",
        )


def _flush_pending_progress_updates(state: AppState) -> int:
    if str(os.getenv("GETOFFLINE_ENABLE_PROGRESS_PERSISTENCE", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
        with state.pending_progress_lock:
            dropped = len(state.pending_progress)
            state.pending_progress.clear()
            state.pending_progress_event.clear()
        if dropped:
            log.info("Progress persistence disabled; dropped %s pending updates.", dropped)
        return 0

    with state.pending_progress_lock:
        pending = dict(state.pending_progress)
        state.pending_progress.clear()
        state.pending_progress_event.clear()

    if not pending:
        return 0

    flush_started_at = time.monotonic()
    batch_payload: Dict[int, float] = {}
    for row_id, progress_payload in pending.items():
        seconds = float(progress_payload[0]) if isinstance(progress_payload, tuple) else float(progress_payload)
        batch_payload[int(row_id)] = seconds

    updated_count = update_download_positions_batch(str(state.database_path), batch_payload)
    if updated_count < len(batch_payload):
        log.warning(
            "Progress batch update partial: attempted=%s updated=%s",
            len(batch_payload),
            updated_count,
        )

    with state.progress_metrics_lock:
        state.progress_flush_count += 1
        flush_count = state.progress_flush_count

    elapsed_ms = (time.monotonic() - flush_started_at) * 1000.0
    log.info(
        "Progress flush #%s: attempted=%s updated=%s flush_ms=%.1f",
        flush_count,
        len(pending),
        updated_count,
        elapsed_ms,
    )
    return updated_count


def _progress_flush_loop(state: AppState, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        has_pending = state.pending_progress_event.wait(PROGRESS_FLUSH_POLL_SECONDS)
        if stop_event.is_set():
            break
        if not has_pending:
            continue

        # Coalesce frequent /progress events to reduce write churn.
        time.sleep(PROGRESS_FLUSH_COALESCE_SECONDS)
        if stop_event.is_set():
            break

        try:
            _flush_pending_progress_updates(state)
        except Exception as exc:
            log.warning("Progress flush loop error: %s", exc)

    try:
        _flush_pending_progress_updates(state)
    except Exception as exc:
        log.warning("Final progress flush error: %s", exc)


def _auto_update_interval_seconds(state: AppState) -> int:
    defaults = state.config.get("defaults") or {}
    try:
        minutes = int(defaults.get("auto_update_minutes") or DEFAULT_AUTO_UPDATE_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_AUTO_UPDATE_MINUTES
    return max(1, minutes) * 60


def _auto_update_loop(state: AppState, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        if stop_event.wait(_auto_update_interval_seconds(state)):
            break
        trigger_background_update(state)


def _android_sync_loop(state: AppState, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        interval_seconds = _auto_update_interval_seconds(state)
        if stop_event.wait(interval_seconds):
            break
        log.info("Android sync periodic check running after %ss interval", interval_seconds)
        trigger_android_sync(state)


def _descriptor_cleanup_loop(state: AppState, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        if stop_event.wait(DESCRIPTOR_CLEANUP_INTERVAL_SECONDS):
            break
        closed_count = close_cached_descriptors()
        if closed_count:
            log.info("Descriptor cleanup: disposed %s cached database engine(s)", closed_count)


def _run_single_youtube_download(state: AppState, single_config: Dict) -> None:
    from youtube import download_youtube_items

    downloaded_items: List[str] = []
    with state.update_status.lock:
        state.update_status.is_running = True
        state.update_status.last_started_at = time.time()
        state.update_status.last_result = "running"
        state.update_status.last_error = None
        state.update_status.last_items_count = 0

    try:
        download_youtube_items(single_config, downloaded_items)
        with state.update_status.lock:
            state.update_status.last_result = "ok"
            state.update_status.last_items_count = len(downloaded_items)
    except Exception as exc:
        with state.update_status.lock:
            state.update_status.last_result = "failed"
            state.update_status.last_error = str(exc)
    finally:
        with state.update_status.lock:
            state.update_status.is_running = False
            state.update_status.last_finished_at = time.time()


def trigger_single_youtube_download(
    state: AppState,
    *,
    url: str,
    media_type: str,
    force_redownload: bool = False,
    subtitles_enabled: Optional[bool] = None,
) -> bool:
    from youtube import resolve_youtube_source_name

    with state.update_status.lock:
        if state.update_status.is_running:
            return False

    cookie_path = materialize_youtube_cookie_file(str(state.database_path))
    source_name = resolve_youtube_source_name(url, cookie_path)
    stored = get_stored_config(str(state.database_path))
    single_config = {
        "defaults": dict(stored["defaults"]),
        "download_settings": dict(stored["download_settings"]),
        "youtube": [
            {
                "name": source_name,
                "url": url,
                "type": media_type,
                "enabled": True,
                "subtitles": media_type == "audio" if subtitles_enabled is None else bool(subtitles_enabled),
                "redownload": bool(force_redownload),
            }
        ],
        "podcasts": [],
    }

    thread = threading.Thread(target=_run_single_youtube_download, args=(state, single_config), daemon=True)
    thread.start()
    return True


def _render_index(
    rows: List[MediaRow],
    output_root: Path,
    database_path: Path,
    status: Dict[str, str],
    show_played: bool = False,
    favorites_only: bool = False,
    android_status: Optional[Dict[str, str]] = None,
) -> str:
    cards = []
    visible_rows = []
    for row in rows:
        path = Path(row.file_path)
        safe = _resolve_safe_media_path(output_root, row.file_path) if row.file_path else None
        file_exists = safe is not None

        row_is_favorite = bool(getattr(row, "favorite", False))
        if favorites_only and not row_is_favorite:
            continue

        position_seconds = max(0.0, float(getattr(row, "last_position_seconds", 0.0) or 0.0))
        is_started = bool(position_seconds > 0 and not row.played)

        visible_rows.append(row)

        title_text = row.title or path.name or "Unknown title"
        title = html.escape(title_text)
        summary_hover = str(getattr(row, "summary_text", "") or "").strip() or title_text
        title_hover = html.escape(summary_hover)
        channel = html.escape(row.source_name or "?")
        source_kind = html.escape((row.source_type or "?").strip())
        size = html.escape(_human_size(row.file_size_bytes))
        raw_ext = (row.file_ext or path.suffix.lstrip(".")) or "?"
        ext = html.escape(raw_ext)
        media_kind = "video" if str(raw_ext).lower() in {"mp4", "mkv", "webm", "mov"} else "audio"
        has_subtitles = False
        if file_exists and media_kind == "audio" and safe is not None:
            has_subtitles = _resolve_safe_subtitle_path(output_root, row, safe) is not None

        status_label = "UNPLAYED"
        status_class = "status-unplayed"
        status_title = "Never played"
        if is_started:
            status_label = "STARTED"
            status_class = "status-started"
            status_title = "Playback started"
        if row.played:
            status_label = "PLAYED"
            status_class = "status-played"
            status_title = "Playback completed"
        if not file_exists:
            status_label = "MISSING"
            status_class = "status-missing"
            status_title = "File missing locally"
        play_or_download_href = f"/play?id={row.row_id}" if file_exists else f"/redownload?id={row.row_id}"
        play_or_download_label = "Play this item" if file_exists else "Redownload this item"
        resume_seconds = 0.0
        if file_exists:
            try:
                resume_seconds = get_download_position_seconds(str(database_path), row.row_id)
            except Exception:
                resume_seconds = 0.0

        cards.append(
            f"""
            <tr data-row-id="{row.row_id}" data-played="{'1' if row.played else '0'}" data-favorite="{'1' if row_is_favorite else '0'}" data-file-exists="{'1' if file_exists else '0'}">
                <td class="channel-col" data-label="Channel" title="{channel}">{channel}</td>
                <td class="title-cell episode-col" data-label="Episode"><a class="episode-link" href="{play_or_download_href}" aria-label="{play_or_download_label}" data-summary="{title_hover}" data-play-link="1" data-row-id="{row.row_id}" data-title="{title}" data-source="{channel}" data-kind="{media_kind}" data-has-subtitles="{'1' if has_subtitles else '0'}" data-resume-seconds="{max(0.0, float(resume_seconds)):.3f}">{title}</a></td>
                <td data-label="Source"><span class="pill status-new" title="Source: {source_kind}">{source_kind}</span></td>
                <td data-label="Type"><span class="pill">{ext}</span></td>
                <td data-label="Size">{size}</td>
                <td data-label="Status"><span class="pill {status_class}" title="{status_title}">{status_label}</span></td>
                <td class="selection-cell" data-label="Select">
                  <input type="checkbox" class="row-selector" name="ids" value="{row.row_id}" aria-label="Select {title}" />
                </td>
            </tr>
            """
        )

    table_rows = "\n".join(cards) if cards else "<tr><td colspan='7'>No media items found yet.</td></tr>"
    sync_running = status["is_running"] == "yes"
    android_status = android_status or {"is_running": "no", "last_result": "idle", "last_copied_count": "0", "last_skipped_count": "0"}
    android_running = android_status.get("is_running") == "yes"
    android_button_disabled = "disabled" if android_running else ""
    button_disabled = "disabled" if sync_running else ""
    sync_icon_class = " is-spinning" if sync_running else ""
    sync_icon_href = "#bi-arrow-repeat" if sync_running else "#bi-download"
    total_items = len(visible_rows)
    played_items = sum(1 for item in visible_rows if item.played)
    favorite_items = sum(1 for item in visible_rows if bool(getattr(item, "favorite", False)))
    unplayed_items = max(total_items - played_items, 0)
    try:
        init_database(str(database_path))
        total_listened = _human_duration(get_total_listened_seconds(str(database_path)))
    except sqlite3.OperationalError as exc:
        if _is_sqlite_open_error(exc):
            fallback = _fallback_database_path(database_path, output_root)
            if fallback is None:
                log.warning("Unable to open database while rendering summary stats (db=%s): %s", database_path, exc)
                total_listened = _human_duration(0.0)
            else:
                total_listened = _human_duration(get_total_listened_seconds(str(fallback)))
        else:
            raise
    toggle_show_played = not show_played
    query_bits = []
    if toggle_show_played:
        query_bits.append("show_played=1")
    if favorites_only:
        query_bits.append("favorites=1")
    toggle_href = "/" + ("?" + "&".join(query_bits) if query_bits else "")
    toggle_label = "Show everything" if toggle_show_played else "Show default"
    toggle_icon = "bi-eye" if toggle_show_played else "bi-eye-slash"
    toggle_favorites_only = not favorites_only
    fav_query_bits = []
    if show_played:
        fav_query_bits.append("show_played=1")
    if toggle_favorites_only:
        fav_query_bits.append("favorites=1")
    favorites_href = "/" + ("?" + "&".join(fav_query_bits) if fav_query_bits else "")
    favorites_label = "Show favorites" if toggle_favorites_only else "Show all"
    favorites_icon = "bi-heart" if toggle_favorites_only else "bi-heart-fill"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GetOffline Media Library</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --surface: #ffffff;
      --surface-2: #f3f6ff;
      --text: #17213a;
      --muted: #5d6780;
      --accent: #2f62f2;
      --accent-2: #1f4fe0;
      --ok-bg: #dbf8e8;
      --ok-text: #0f7a43;
      --new-bg: #e7ecff;
      --new-text: #3147aa;
      --border: #dbe3f3;
      --shadow: 0 10px 30px rgba(40, 65, 120, .08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      background: linear-gradient(180deg, #f8faff 0%, #f3f6fc 100%);
      color: var(--text);
      padding: 1rem;
    }}
    .container {{ max-width: 1280px; margin: 0 auto; }}
    .hero {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: var(--shadow);
      padding: 1.1rem 1.1rem .95rem;
      margin-bottom: 1rem;
    }}
    h1 {{ margin: 0 0 .35rem 0; font-size: clamp(1.5rem, 2.8vw, 2.1rem); }}
    .meta {{ color: var(--muted); margin: 0; font-size: .95rem; }}
    .meta code {{ color: #263a78; background: #eef3ff; padding: .12rem .4rem; border-radius: 6px; }}

    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: .65rem;
      margin: .85rem 0 .2rem;
    }}
    .summary-card {{
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: .55rem .65rem;
    }}
    .summary-label {{ color: var(--muted); font-size: .76rem; text-transform: uppercase; letter-spacing: .06em; }}
    .summary-value {{ font-weight: 700; margin-top: .15rem; }}

    .panel {{
      margin: 0 0 1rem;
      padding: 1rem;
      border: 1px solid var(--border);
      border-radius: 12px;
      background: var(--surface);
      box-shadow: var(--shadow);
      display: grid;
      gap: .35rem;
    }}
    .toolbar {{ margin-bottom: .35rem; }}
    .toolbar-actions {{
      display: flex;
      gap: .6rem;
      align-items: center;
      flex-wrap: wrap;
    }}
    .toolbar-form {{ margin: 0; }}
    .toolbar-spacer {{ flex: 1 1 auto; }}
    .batch-toolbar-form {{ margin-left: auto; display: inline-flex; align-items: center; gap: .45rem; flex-wrap: nowrap; }}
    .batch-select {{ min-width: 10rem; border: 1px solid #c9d5ef; border-radius: 8px; padding: .22rem .42rem; font: inherit; color: #243251; background: #fff; }}
    .batch-apply {{ border: 1px solid #c9d5ef; border-radius: 8px; padding: .24rem .65rem; font: inherit; background: #eef3ff; color: #2c3e74; cursor: pointer; }}
    .batch-apply:hover {{ background: #dfe8ff; }}
    .batch-apply:disabled {{ opacity: .55; cursor: not-allowed; }}
    .library-filter-wrap {{ display: inline-flex; align-items: stretch; width: min(22.5rem, 100%); border: 1px solid #c9d5ef; border-radius: 12px; background: #fff; overflow: hidden; }}
    .library-filter-input {{ border: 0; border-right: 1px solid #dbe3f3; border-radius: 0; padding: .5rem .7rem; font: inherit; min-width: 12rem; flex: 1 1 12rem; }}
    .library-filter-select {{
      border: 0;
      border-right: 1px solid #dbe3f3;
      border-radius: 0;
      padding: .5rem 1.85rem .5rem .65rem;
      font: inherit;
      background: #fff;
      color: #243251;
      min-width: 8.4rem;
      appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 16 16'%3E%3Cpath fill='none' stroke='%23243251' stroke-linecap='round' stroke-linejoin='round' stroke-width='1.8' d='m3.25 6.5 4.75 4.75L12.75 6.5'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right .6rem center;
    }}
    .library-filter-clear {{
      border: 0;
      border-radius: 0;
      padding: .5rem .6rem;
      min-width: 2.35rem;
      font: inherit;
      font-size: 1rem;
      line-height: 1;
      background: #eef3ff;
      color: #2c3e74;
      cursor: pointer;
      font-weight: 700;
      text-align: center;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }}
    .library-filter-clear:hover {{ background: #dfe8ff; }}
    .library-filter-input:focus, .library-filter-select:focus, .library-filter-clear:focus {{ outline: none; }}
    .library-filter-wrap:focus-within {{ box-shadow: 0 0 0 2px rgba(47, 98, 242, .22); border-color: #2f62f2; }}
    .quick-add-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(6, 10, 24, .62);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 30;
      padding: 1rem;
    }}
    .quick-add-backdrop.is-open {{ display: flex; }}
    .quick-add-modal {{
      width: min(520px, 100%);
      background: #fff;
      border: 1px solid #dbe3f3;
      border-radius: 14px;
      box-shadow: 0 24px 70px rgba(12, 22, 52, .3);
      padding: 1rem;
    }}
    .quick-add-modal h2 {{ margin: 0 0 .75rem 0; font-size: 1.1rem; color: #1d2b52; }}
    .quick-add-form {{ display: grid; gap: .7rem; }}
    .quick-add-form label {{ font-size: .9rem; color: #2f3f66; font-weight: 600; }}
    .quick-add-input, .quick-add-select {{
      width: 100%;
      border: 1px solid #c9d5ef;
      border-radius: 10px;
      padding: .55rem .75rem;
      font: inherit;
      background: #fff;
      color: #243251;
      box-sizing: border-box;
    }}
    .quick-add-actions {{ display: flex; justify-content: flex-end; gap: .5rem; margin-top: .2rem; }}
    .quick-add-search-row {{ display: grid; grid-template-columns: 1fr auto; gap: .5rem; }}
    .quick-add-results {{
      margin-top: .2rem;
      display: grid;
      gap: .5rem;
      max-height: 300px;
      overflow-y: auto;
      padding-right: .2rem;
    }}
    .quick-add-result {{
      border: 1px solid #d8e1f6;
      border-radius: 10px;
      padding: .45rem;
      display: grid;
      grid-template-columns: 120px 1fr auto;
      gap: .6rem;
      align-items: center;
      background: #fbfcff;
    }}
    .quick-add-thumb {{ width: 120px; height: 68px; object-fit: cover; border-radius: 8px; background: #edf2ff; }}
    .quick-add-meta-title {{ font-weight: 600; color: #253559; }}
    .quick-add-meta-sub {{ font-size: .82rem; color: #5f6d90; margin-top: .15rem; }}
    .quick-add-empty {{ color: #5f6d90; font-size: .88rem; }}


    table {{
      width: 100%;
      table-layout: fixed;
      border-collapse: separate;
      border-spacing: 0;
      overflow: hidden;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: var(--surface);
      box-shadow: var(--shadow);
    }}
    thead th {{
      background: var(--surface-2);
      color: #3f4e75;
      text-align: left;
      font-weight: 600;
      letter-spacing: .02em;
      font-size: .9rem;
      padding: .7rem .75rem;
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 1;
    }}
    td {{
      border-bottom: 1px solid #edf1fa;
      padding: .7rem .75rem;
      vertical-align: middle;
      color: var(--text);
    }}
    tbody tr:nth-child(even) td {{ background: #fbfcff; }}
    tr:last-child td {{ border-bottom: none; }}
    .title-cell {{ font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.25; }}
    .col-channel {{ width: 16%; }}
    .col-episode {{ width: 42%; }}
    .col-source {{ width: 10%; }}
    .col-type {{ width: 7%; }}
    .col-size {{ width: 9%; }}
    .col-status {{ width: 10%; }}
    .col-select {{ width: 6%; }}
    td[data-label="Type"], td[data-label="Size"], td[data-label="Status"],
    thead th:nth-child(4), thead th:nth-child(5), thead th:nth-child(6) {{
      text-align: left;
    }}
    td[data-label="Select"], thead th:nth-child(7) {{
      text-align: right;
    }}
    th.channel-col, td.channel-col {{ padding-right: .2rem; }}
    th.episode-col, td.episode-col {{ padding-left: .2rem; }}
    .pill {{
      display: inline-block;
      padding: .18rem .5rem;
      border-radius: 999px;
      background: #eef3ff;
      color: #43507b;
      font-size: .78rem;
      text-transform: uppercase;
      letter-spacing: .04em;
    }}
    .status-played {{ background: var(--ok-bg); color: var(--ok-text); }}
    .status-unplayed {{ background: var(--new-bg); color: var(--new-text); }}
    .status-started {{ background: #e2f3ff; color: #114e78; }}
    .status-missing {{ background: #fde7e9; color: #96253b; }}

    .episode-link {{ color: inherit; text-decoration: none; }}
    .episode-link:hover {{ color: var(--accent); text-decoration: underline; }}
    .summary-tooltip {{
      position: fixed;
      z-index: 9999;
      pointer-events: none;
      background: rgba(20, 28, 46, 0.96);
      color: #eef3ff;
      border: 1px solid rgba(121, 149, 214, 0.55);
      border-radius: 10px;
      box-shadow: 0 10px 30px rgba(11, 18, 35, 0.28);
      padding: .55rem .7rem;
      font-size: .88rem;
      line-height: 1.35;
      max-width: min(34rem, 80vw);
      opacity: 0;
      transform: translateY(2px);
      transition: opacity .08s ease-out, transform .08s ease-out;
      white-space: normal;
    }}
    .summary-tooltip.is-visible {{ opacity: 1; transform: translateY(0); }}
    .selection-cell {{ text-align: right; }}
    .row-selector {{ width: 1.05rem; height: 1.05rem; cursor: pointer; accent-color: var(--accent); }}
    .select-all-selector {{ display: inline-block; vertical-align: middle; }}
    .actions {{ white-space: nowrap; display: flex; align-items: center; justify-content: flex-end; gap: .6rem; }}
    .icon-button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 2.4rem;
      width: 2.4rem;
      min-width: 2.4rem;
      height: 2.4rem;
      min-height: 2.4rem;
      aspect-ratio: 1 / 1;
      box-sizing: border-box;
      border-radius: 999px;
      border: 1px solid #c9d5ef;
      background: #eef3ff;
      color: #2c3e74;
      text-decoration: none;
      font-size: 1.15rem;
      line-height: 1;
      font-weight: 700;
      padding: 0;
      cursor: pointer;
      transition: background .15s ease, border-color .15s ease, color .15s ease;
    }}
    .icon-button:hover {{ color: #fff; background: var(--accent); border-color: var(--accent); }}
    .icon-button:disabled {{ opacity: .5; cursor: not-allowed; }}
    .icon-button .bi {{ width: 1.1rem; height: 1.1rem; fill: currentColor; }}
    .icon-button .is-spinning {{ animation: spin 1s linear infinite; transform-origin: center; }}
    .icon-button-primary {{
      color: #fff;
      border-color: #3f6ff1;
      background: linear-gradient(180deg, #4f7fff, #3f6ff1);
    }}
    .icon-button-primary:hover {{
      color: #fff;
      border-color: #2f62f2;
      background: linear-gradient(180deg, #4675f4, #2f62f2);
    }}
    .icon-button-active {{ color: #fff; background: #df3f6b; border-color: #df3f6b; }}
    .icon-button-active:hover {{ color: #fff; background: #c53057; border-color: #c53057; }}

    .mini-player-backdrop {{
      position: fixed;
      inset: 0;
      background: transparent;
      pointer-events: none;
      z-index: 45;
      padding: clamp(.75rem, 2.2vw, 1.6rem);
    }}
    .mini-player-backdrop.is-open {{
      background: rgba(0, 0, 0, .9);
      backdrop-filter: blur(1px);
      pointer-events: auto;
    }}
    .mini-player {{
      position: fixed;
      right: 1rem;
      bottom: 1rem;
      width: min(380px, calc(100vw - 2rem));
      max-height: min(86vh, 720px);
      overflow: auto;
      border: 1px solid #2f406d;
      border-radius: 14px;
      background: #0f1831;
      box-shadow: 0 22px 70px rgba(0, 0, 0, 0.5);
      color: #e8efff;
      padding: .85rem;
      display: none;
      gap: .65rem;
      z-index: 50;
      pointer-events: auto;
    }}
    .mini-player.is-visible {{ display: grid; }}
    .mini-player.is-maximized {{
      right: auto;
      bottom: auto;
      left: 50%;
      top: 50%;
      transform: translate(-50%, -50%);
      width: min(1100px, calc(100vw - 2rem));
      max-height: min(94vh, 920px);
    }}
    .mini-player-header {{
      display: grid;
      grid-template-columns: 1fr auto auto;
      align-items: start;
      gap: .35rem .5rem;
    }}
    .mini-player-title {{ grid-column: 1; font-weight: 700; color: #f0f4ff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .mini-player-source {{ grid-column: 1; color: #b4c2e3; font-size: .85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .mini-player-open {{
      grid-row: 1;
      grid-column: 2;
      align-self: center;
      justify-self: end;
      font-size: .82rem;
      color: #d2ddff;
      border: 1px solid #3a4e84;
      border-radius: 999px;
      padding: .25rem .65rem;
      background: #1a2748;
      cursor: pointer;
    }}
    .mini-player-open:hover {{ background: #23355f; }}
    .mini-player-close {{
      grid-row: 1;
      grid-column: 3;
      justify-self: end;
      width: 1.7rem;
      height: 1.7rem;
      border-radius: 999px;
      border: 1px solid #3a4e84;
      background: #1a2748;
      color: #d2ddff;
      font-size: 1.15rem;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
    }}
    .mini-player-close:hover {{ background: #23355f; }}
    .mini-player-media {{ width: 100%; border-radius: 12px; background: #000; }}
    #mini-player-video {{ aspect-ratio: 16 / 9; max-height: min(74vh, 780px); background: #000; }}
    #mini-player-audio {{ border-radius: 999px; }}
    .mini-player-transcript-wrap {{ margin-top: .2rem; display: none; }}
    .mini-player.is-maximized .mini-player-transcript-wrap {{ display: block; }}
    .mini-player-transcript {{
      max-height: 220px;
      overflow-y: auto;
      border: 1px solid #2d3f6d;
      border-radius: 10px;
      background: #111c37;
      padding: .55rem;
      display: none;
    }}
    .mini-player.is-maximized .mini-player-transcript.is-visible {{ display: block; }}
    .mini-player-transcript-line {{
      display: block;
      width: 100%;
      text-align: left;
      color: #cbd8fb;
      background: transparent;
      border: none;
      border-radius: 8px;
      margin: 0;
      padding: .33rem .42rem;
      cursor: pointer;
      line-height: 1.35;
    }}
    .mini-player-transcript-line:hover {{ background: #1e2f55; }}
    .mini-player-transcript-line.active {{ background: #2a427f; color: #f2f6ff; }}

    @keyframes spin {{
      from {{ transform: rotate(0deg); }}
      to {{ transform: rotate(360deg); }}
    }}

    @media (max-width: 1200px) {{
      .summary-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}

    @media (max-width: 980px) {{
      .summary-grid {{ grid-template-columns: 1fr; }}
      .actions {{ white-space: normal; justify-content: flex-start; }}
      .mini-player-backdrop {{ padding: .5rem; }}
      .mini-player {{ right: .5rem; bottom: .5rem; width: calc(100vw - 1rem); max-height: 95vh; }}
      .mini-player.is-maximized {{ width: calc(100vw - 1rem); }}
      table {{ table-layout: auto; }}
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      tr {{ border-bottom: 1px solid var(--border); padding: .4rem 0; }}
      td {{ border: none; display: flex; gap: .6rem; align-items: center; }}
      tbody tr:nth-child(even) td {{ background: transparent; }}
      td::before {{
        content: attr(data-label);
        min-width: 70px;
        font-size: .75rem;
        text-transform: uppercase;
        color: var(--muted);
        letter-spacing: .05em;
      }}
    }}
  </style>
</head>
<body>
  {_icon_sprite()}
  <div class="container">
    <div class="hero">
      <h1>GetOffline</h1>
      <div id="summary-grid" class="summary-grid">
        <div class="summary-card">
          <div class="summary-label">Visible Items</div>
          <div id="summary-visible-items" class="summary-value">{total_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">Played</div>
          <div id="summary-played-items" class="summary-value">{played_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">New</div>
          <div id="summary-new-items" class="summary-value">{unplayed_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">Favorites</div>
          <div id="summary-favorite-items" class="summary-value">{favorite_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">Listened</div>
          <div id="summary-listened-items" class="summary-value">{total_listened}</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="toolbar toolbar-actions">
      <form id="sync-form" method="post" action="/update" class="toolbar-form">
        <button id="sync-button" class="icon-button icon-button-primary" type="submit" title="Sync downloads" aria-label="Sync downloads" {button_disabled}><svg class="bi{sync_icon_class}" aria-hidden="true" focusable="false"><use href="{sync_icon_href}"></use></svg></button>
      </form>
      <button id="quick-add-open" class="icon-button" type="button" title="Add YouTube link or search" aria-label="Add YouTube link or search">{_icon_use("bi-plus-lg")}</button>
      <button id="transcript-search-open" class="icon-button" type="button" title="Search transcript text" aria-label="Search transcript text">{_icon_use("bi-search")}</button>
        <a class="icon-button" href="/settings" title="Settings" aria-label="Settings">{_icon_use("bi-gear")}</a>
        <div class="library-filter-wrap" role="group" aria-label="Library filters">
          <input id="library-filter" class="library-filter-input" type="search" placeholder="Filter by artist or title..." aria-label="Filter by artist or title" autocomplete="off" />
          <select id="library-filter-mode" class="library-filter-select" aria-label="Filter mode">
            <option value="unplayed" selected>Unplayed</option>
            <option value="played">Played</option>
            <option value="favorites">Favorites</option>
            <option value="all">All</option>
          </select>
          <button id="library-filter-clear" class="library-filter-clear" type="button" title="Clear filters" aria-label="Clear filters">×</button>
        </div>
        <span class="toolbar-spacer" aria-hidden="true"></span>
        <form id="batch-form" method="post" action="/batch-update" class="batch-toolbar-form">
          <select id="batch-action" class="batch-select" name="batch_action" aria-label="Batch action">
            <option value="">Choose action</option>
            <option value="played">played</option>
            <option value="unplayed">unplayed</option>
            <option value="favorite">favorite</option>
            <option value="unfavorite">unfavorite</option>
            <option value="edit-metadata">edit</option>
            <option value="delete">delete</option>
            <option value="download">download</option>
          </select>
          <button id="batch-apply" class="batch-apply" type="submit" title="Apply batch action" aria-label="Apply batch action" disabled>Apply</button>
        </form>
      </div>
    </div>
    <div id="transcript-search-backdrop" class="quick-add-backdrop" aria-hidden="true">
      <div class="quick-add-modal" role="dialog" aria-modal="true" aria-labelledby="transcript-search-title">
        <h2 id="transcript-search-title">Search transcripts</h2>
        <input id="transcript-search-input" class="quick-add-input" type="search" placeholder="Search words in transcripts..." />
        <div id="transcript-search-results" class="quick-add-results" aria-live="polite"></div>
        <div class="quick-add-actions"><button id="transcript-search-close" type="button">Close</button></div>
      </div>
    </div>
    <div id="summary-tooltip" class="summary-tooltip" role="tooltip" aria-hidden="true"></div>
    <div id="metadata-edit-backdrop" class="quick-add-backdrop" aria-hidden="true">
      <div class="quick-add-modal" role="dialog" aria-modal="true" aria-labelledby="metadata-edit-title">
        <h2 id="metadata-edit-title">Edit</h2>
        <form id="metadata-edit-form" class="quick-add-form">
          <input id="metadata-edit-id" name="id" type="hidden" />
          <div>
            <label for="metadata-edit-item-title">Title</label>
            <input id="metadata-edit-item-title" class="quick-add-input" name="title" type="text" required />
          </div>
          <div>
            <label for="metadata-edit-source-name">Source name</label>
            <input id="metadata-edit-source-name" class="quick-add-input" name="source_name" type="text" required />
          </div>
          <div class="quick-add-actions">
            <button id="metadata-edit-cancel" type="button">Cancel</button>
            <button class="primary" type="submit">Save</button>
          </div>
        </form>
      </div>
    </div>

    <div id="quick-add-backdrop" class="quick-add-backdrop" aria-hidden="true">
      <div class="quick-add-modal" role="dialog" aria-modal="true" aria-labelledby="quick-add-title">
        <h2 id="quick-add-title">Add single YouTube link</h2>
        <form id="quick-add-form" method="post" action="/quick-add-youtube" class="quick-add-form">
          <div>
            <label for="quick-add-search">Search YouTube (press Enter)</label>
            <input id="quick-add-search" class="quick-add-input" type="search" name="q" placeholder="Search videos..." autocomplete="off" />
          </div>
          <div id="quick-add-results" class="quick-add-results" aria-live="polite"></div>
          <div>
            <label for="quick-add-url">YouTube URL</label>
            <input id="quick-add-url" class="quick-add-input" type="url" name="url" placeholder="https://www.youtube.com/watch?v=..." required />
          </div>
          <div>
            <label for="quick-add-media-type">Download type</label>
            <select id="quick-add-media-type" class="quick-add-select" name="media_type">
              <option value="video" selected>video</option>
              <option value="audio">audio</option>
            </select>
          </div>
          <div class="quick-add-actions">
            <button id="quick-add-cancel" type="button">Cancel</button>
            <button type="submit" class="primary">Add</button>
          </div>
        </form>
      </div>
    </div>

    <div id="drag-drop-upload-hint" style="display:none;position:fixed;inset:1.5rem;z-index:2000;border:3px dashed #0d6efd;border-radius:16px;background:rgba(13,110,253,.12);color:#0d6efd;font-weight:700;align-items:center;justify-content:center;text-align:center;padding:2rem;">
      Drop media file to import into Downloads folder
    </div>
    <div id="upload-progress-wrap" style="display:none;position:fixed;inset:0;z-index:2200;background:rgba(15,23,42,.45);align-items:center;justify-content:center;padding:1rem;">
      <div style="background:#111827;color:#fff;padding:1rem 1rem;border-radius:12px;min-width:min(520px,95vw);box-shadow:0 12px 40px rgba(0,0,0,.35);">
        <div id="upload-progress-label" style="font-size:.95rem;margin-bottom:.55rem;font-weight:600;">Uploading…</div>
        <progress id="upload-progress-bar" max="100" value="0" style="width:100%;height:16px;"></progress>
      </div>
    </div>

    <table id="downloads-table">
      <colgroup>
        <col class="col-channel" />
        <col class="col-episode" />
        <col class="col-source" />
        <col class="col-type" />
        <col class="col-size" />
        <col class="col-status" />
        <col class="col-select" />
      </colgroup>
      <thead><tr><th class="channel-col">Channel</th><th class="episode-col">Episode</th><th>Source</th><th>Type</th><th>Size</th><th>Status</th><th><input type="checkbox" id="select-all-rows" class="row-selector select-all-selector" aria-label="Select all rows" /></th></tr></thead>
      <tbody id="downloads-table-body">{table_rows}</tbody>
    </table>

    <section id="mini-player-backdrop" class="mini-player-backdrop" aria-hidden="true">
    <section id="mini-player" class="mini-player" aria-live="polite">
      <div class="mini-player-header">
        <div class="mini-player-title" id="mini-player-title"></div>
        <div class="mini-player-source" id="mini-player-source"></div>
        <button id="mini-player-close" class="mini-player-close" type="button" aria-label="Close mini player">&times;</button>
        <button id="mini-player-open" class="mini-player-open" type="button" aria-label="Maximize player">Maximize</button>
      </div>
      <audio id="mini-player-audio" class="mini-player-media" controls preload="metadata"></audio>
      <video id="mini-player-video" class="mini-player-media" controls preload="metadata"></video>
      <section class="mini-player-transcript-wrap">
        <div id="mini-player-transcript" class="mini-player-transcript" aria-live="polite"></div>
      </section>
    </section>
  </section>
  </div>
  <script>
    (() => {{
      const escapeHtml = (value) => String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');

      const syncForm = document.getElementById('sync-form');
      const syncButton = document.getElementById('sync-button');
      const summaryTooltip = document.getElementById('summary-tooltip');
      let syncReloadTimer = null;
      let syncStatusPollTimer = null;
      let deferredLibraryRefreshTimer = null;
      let suppressSyncAutoReload = false;
      const syncStatusPollIntervalMs = 1500;
      const mediaSettingsStorageKey = 'getofflineMediaElementSettings';

      const isMediaPlaybackActive = () => {{
        return Array.from(document.querySelectorAll('audio,video')).some((media) => {{
          if (!media) return false;
          return !media.paused && !media.ended && media.readyState > 2;
        }});
      }};

      const readStoredMediaSettings = () => {{
        const raw = window.localStorage.getItem(mediaSettingsStorageKey);
        if (!raw) return null;
        try {{
          const parsed = JSON.parse(raw);
          if (!parsed || typeof parsed !== 'object') return null;
          const volume = Number(parsed.volume);
          return {{
            volume: Number.isFinite(volume) ? Math.min(1, Math.max(0, volume)) : null,
            muted: !!parsed.muted,
          }};
        }} catch (_) {{
          return null;
        }}
      }};

      const applyStoredMediaSettings = (media) => {{
        if (!media) return;
        const stored = readStoredMediaSettings();
        if (!stored) return;
        if (stored.volume !== null) media.volume = stored.volume;
        media.muted = !!stored.muted;
      }};

      const persistMediaSettings = (media) => {{
        if (!media) return;
        window.localStorage.setItem(mediaSettingsStorageKey, JSON.stringify({{
          volume: Number(media.volume),
          muted: !!media.muted,
        }}));
      }};

      const dragDropHint = document.getElementById('drag-drop-upload-hint');
      const uploadProgressWrap = document.getElementById('upload-progress-wrap');
      const uploadProgressBar = document.getElementById('upload-progress-bar');
      const uploadProgressLabel = document.getElementById('upload-progress-label');
      let dragCounter = 0;
      const setDragOverlay = (isVisible) => {{
        if (!dragDropHint) return;
        dragDropHint.style.display = isVisible ? 'flex' : 'none';
      }};
      const containsFiles = (event) => {{
        const types = event?.dataTransfer?.types;
        return !!(types && Array.from(types).includes('Files'));
      }};
      window.addEventListener('dragenter', (event) => {{
        if (!containsFiles(event)) return;
        event.preventDefault();
        dragCounter += 1;
        setDragOverlay(true);
      }});
      window.addEventListener('dragover', (event) => {{
        if (!containsFiles(event)) return;
        event.preventDefault();
      }});
      window.addEventListener('dragleave', (event) => {{
        if (!containsFiles(event)) return;
        event.preventDefault();
        dragCounter = Math.max(0, dragCounter - 1);
        if (dragCounter === 0) setDragOverlay(false);
      }});
      window.addEventListener('drop', async (event) => {{
        if (!containsFiles(event)) return;
        event.preventDefault();
        dragCounter = 0;
        setDragOverlay(false);
        const files = Array.from(event.dataTransfer.files || []);
        if (!files.length) return;
        const file = files[0];
        try {{
          const resp = await new Promise((resolve, reject) => {{
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/import-media', true);
            xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
            xhr.setRequestHeader('X-Upload-Filename', encodeURIComponent(file.name || 'upload.bin'));
            if (uploadProgressWrap) uploadProgressWrap.style.display = 'flex';
            if (uploadProgressBar) uploadProgressBar.value = 0;
            if (uploadProgressLabel) uploadProgressLabel.textContent = `Uploading ${{file.name}}… 0%`;

            xhr.upload.onprogress = (progressEvent) => {{
              if (!progressEvent.lengthComputable) return;
              const pct = Math.min(100, Math.round((progressEvent.loaded / progressEvent.total) * 100));
              if (uploadProgressBar) uploadProgressBar.value = pct;
              if (uploadProgressLabel) uploadProgressLabel.textContent = `Uploading ${{file.name}}… ${{pct}}%`;
            }};
            xhr.onload = () => resolve({{ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status }});
            xhr.onerror = () => reject(new Error('Network error'));
            xhr.send(file);
          }});
          if (!resp.ok) throw new Error(`HTTP ${{resp.status}}`);
          if (uploadProgressBar) uploadProgressBar.value = 100;
          if (uploadProgressLabel) uploadProgressLabel.textContent = `Upload complete: ${{file.name}}`;
          window.location.reload();
        }} catch (err) {{
          if (uploadProgressLabel) uploadProgressLabel.textContent = `Upload failed: ${{file.name}}`;
          window.alert(`Failed to import file: ${{err}}`);
        }} finally {{
          window.setTimeout(() => {{
            if (uploadProgressWrap) uploadProgressWrap.style.display = 'none';
          }}, 1200);
        }}
      }});

      const clearSyncReloadTimer = () => {{
        if (syncReloadTimer) {{
          window.clearTimeout(syncReloadTimer);
          syncReloadTimer = null;
        }}
      }};

      const clearSyncStatusPollTimer = () => {{
        if (syncStatusPollTimer) {{
          window.clearTimeout(syncStatusPollTimer);
          syncStatusPollTimer = null;
        }}
      }};

      const setSyncButtonRunning = () => {{
        if (!syncButton) return;
        syncButton.disabled = true;
        const icon = syncButton.querySelector('.bi');
        const iconUse = syncButton.querySelector('use');
        if (icon) icon.classList.add('is-spinning');
        if (iconUse) iconUse.setAttribute('href', '#bi-arrow-repeat');
      }};

      const setSyncButtonIdle = () => {{
        if (!syncButton) return;
        syncButton.disabled = false;
        const icon = syncButton.querySelector('.bi');
        const iconUse = syncButton.querySelector('use');
        if (icon) icon.classList.remove('is-spinning');
        if (iconUse) iconUse.setAttribute('href', '#bi-download');
      }};

      let rowSelectors = [];

      const getVisibleRowSelectors = () => rowSelectors.filter((input) => {{
        const row = input.closest('tr[data-row-id]');
        return !!row && row.style.display !== 'none';
      }});

      const refreshLibraryViewWithoutReload = () => {{
        return fetch(window.location.pathname + window.location.search, {{ cache: 'no-store' }})
          .then((response) => response.ok ? response.text() : null)
          .then((htmlText) => {{
            if (!htmlText) return;
            const parsed = new window.DOMParser().parseFromString(htmlText, 'text/html');
            const nextSummary = parsed.getElementById('summary-grid');
            const nextTableBody = parsed.getElementById('downloads-table-body');
            const currentSummary = document.getElementById('summary-grid');
            const currentTableBody = document.getElementById('downloads-table-body');

            if (currentSummary && nextSummary) currentSummary.innerHTML = nextSummary.innerHTML;
            if (currentTableBody && nextTableBody) currentTableBody.innerHTML = nextTableBody.innerHTML;
            bindBatchControls();
            bindPlayLinks();
            applyLibraryFilter();
          }})
          .catch(() => {{}})
          .finally(() => {{
            setSyncButtonIdle();
          }});
      }};

      const scheduleDeferredLibraryRefresh = () => {{
        if (deferredLibraryRefreshTimer !== null) return;
        const attemptRefresh = () => {{
          if (isMediaPlaybackActive()) {{
            deferredLibraryRefreshTimer = window.setTimeout(attemptRefresh, 1000);
            return;
          }}
          deferredLibraryRefreshTimer = null;
          refreshLibraryViewWithoutReload();
        }};
        deferredLibraryRefreshTimer = window.setTimeout(attemptRefresh, 1000);
      }};

      const getCurrentViewFlags = () => {{
        const params = new window.URLSearchParams(window.location.search);
        return {{
          showPlayed: params.get('show_played') === '1',
          favoritesOnly: params.get('favorites') === '1',
        }};
      }};


      const libraryFilterInput = document.getElementById('library-filter');
      const libraryFilterMode = document.getElementById('library-filter-mode');
      const libraryFilterClear = document.getElementById('library-filter-clear');

      const getFilterText = () => String((libraryFilterInput && libraryFilterInput.value) || '').trim().toLowerCase();
      const getFilterMode = () => String((libraryFilterMode && libraryFilterMode.value) || 'unplayed');

      const applyLibraryFilter = () => {{
        const filterText = getFilterText();
        const mode = getFilterMode();
        const rows = Array.from(document.querySelectorAll('#downloads-table-body tr[data-row-id]'));
        rows.forEach((row) => {{
          const channelText = String((row.querySelector('.channel-col') && row.querySelector('.channel-col').textContent) || '').toLowerCase();
          const titleText = String((row.querySelector('.episode-link') && row.querySelector('.episode-link').textContent) || '').toLowerCase();
          const matchesText = !filterText || channelText.includes(filterText) || titleText.includes(filterText);
          const mode = getFilterMode();
          const matchesMode = mode === 'all' || (mode === 'unplayed' && row.dataset.played !== '1' && row.dataset.fileExists === '1') || (mode === 'played' && row.dataset.played === '1') || (mode === 'favorites' && row.dataset.favorite === '1');
          const isMatch = matchesText && matchesMode;
          row.style.display = isMatch ? '' : 'none';
          if (!isMatch) {{
            const selector = row.querySelector('.row-selector[name="ids"]');
            if (selector) selector.checked = false;
          }}
        }});
        if (libraryFilterClear) {{
          const hasFilters = Boolean(filterText) || mode !== 'unplayed';
          libraryFilterClear.style.opacity = hasFilters ? '1' : '.55';
        }}
        updateBatchState();
        renderVisibleSummaryCounts();
      }};
      const renderVisibleSummaryCounts = () => {{
        const renderedRows = Array.from(document.querySelectorAll('#downloads-table-body tr[data-row-id]')).filter((row) => row.style.display !== 'none');
        const visibleCountEl = document.getElementById('summary-visible-items');
        const playedCountEl = document.getElementById('summary-played-items');
        const newCountEl = document.getElementById('summary-new-items');
        const favoriteCountEl = document.getElementById('summary-favorite-items');
        const playedCount = renderedRows.filter((row) => row.dataset.played === '1').length;
        const favoriteCount = renderedRows.filter((row) => row.dataset.favorite === '1').length;

        if (visibleCountEl) visibleCountEl.textContent = String(renderedRows.length);
        if (playedCountEl) playedCountEl.textContent = String(playedCount);
        if (newCountEl) newCountEl.textContent = String(Math.max(renderedRows.length - playedCount, 0));
        if (favoriteCountEl) favoriteCountEl.textContent = String(favoriteCount);
      }};

      const applyBatchActionLocally = (actionName, selectedRows) => {{
        const flags = getCurrentViewFlags();
        selectedRows.forEach((input) => {{
          const row = input.closest('tr[data-row-id]');
          if (!row) return;

          if (actionName === 'delete') {{
            row.remove();
            return;
          }}

          if (actionName === 'favorite') {{
            row.dataset.favorite = '1';
            return;
          }}

          if (actionName === 'unfavorite') {{
            row.dataset.favorite = '0';
            if (flags.favoritesOnly) row.remove();
            return;
          }}

          if (actionName === 'played') {{
            row.dataset.played = '1';
            if (!flags.showPlayed) row.remove();
            return;
          }}

          if (actionName === 'unplayed') {{
            row.dataset.played = '0';
          }}
        }});
        renderVisibleSummaryCounts();
      }};

      const scheduleSyncReloadWhenSafe = () => {{
        clearSyncReloadTimer();
        const attemptReload = () => {{
          if (suppressSyncAutoReload || document.hidden || isMediaPlaybackActive()) {{
            syncReloadTimer = window.setTimeout(attemptReload, syncStatusPollIntervalMs);
            return;
          }}
          window.location.reload();
        }};
        syncReloadTimer = window.setTimeout(attemptReload, 250);
      }};

      const pollSyncStatusUntilFinished = () => {{
        clearSyncStatusPollTimer();
        fetch('/update-status', {{ cache: 'no-store' }})
          .then((response) => response.ok ? response.json() : null)
          .then((status) => {{
            if (!status) throw new Error('missing status payload');
            if (status.is_running === 'yes') {{
              syncStatusPollTimer = window.setTimeout(pollSyncStatusUntilFinished, syncStatusPollIntervalMs);
              return;
            }}
            setSyncButtonIdle();
            refreshLibraryViewWithoutReload();
            scheduleSyncReloadWhenSafe();
          }})
          .catch(() => {{
            syncStatusPollTimer = window.setTimeout(pollSyncStatusUntilFinished, syncStatusPollIntervalMs);
          }});
      }};

      if (syncForm && syncButton && !syncButton.disabled) {{
        let syncRequestInFlight = false;

        syncForm.addEventListener('submit', (event) => {{
          event.preventDefault();
          if (syncRequestInFlight) return;
          syncRequestInFlight = true;

          setSyncButtonRunning();

          fetch('/update', {{ method: 'POST', keepalive: true }})
            .catch(() => {{}})
            .finally(() => {{
              pollSyncStatusUntilFinished();
            }});
        }});
      }}

      const batchForm = document.getElementById('batch-form');
      const batchAction = document.getElementById('batch-action');
      const batchApply = document.getElementById('batch-apply');
      const selectAllRows = document.getElementById('select-all-rows');
      const metadataEditBackdrop = document.getElementById('metadata-edit-backdrop');
      const metadataEditForm = document.getElementById('metadata-edit-form');
      const metadataEditIdInput = document.getElementById('metadata-edit-id');
      const metadataEditTitleInput = document.getElementById('metadata-edit-item-title');
      const metadataEditSourceInput = document.getElementById('metadata-edit-source-name');
      const metadataEditCancel = document.getElementById('metadata-edit-cancel');

      const updateBatchState = () => {{
        const visibleRowSelectors = getVisibleRowSelectors();
        const selectedCount = visibleRowSelectors.filter((input) => input.checked).length;
        const hasAction = batchAction && batchAction.value;
        if (batchApply) batchApply.disabled = !(selectedCount > 0 && hasAction);
        if (selectAllRows) {{
          selectAllRows.checked = visibleRowSelectors.length > 0 && selectedCount === visibleRowSelectors.length;
          selectAllRows.indeterminate = selectedCount > 0 && selectedCount < rowSelectors.length;
          selectAllRows.indeterminate = selectedCount > 0 && selectedCount < visibleRowSelectors.length;
        }}
      }};

      const buildBatchRequestBody = (selectedRows) => {{
        const formBody = new window.URLSearchParams();
        formBody.set('batch_action', batchAction.value);
        selectedRows.forEach((input) => {{
          formBody.append('ids', input.value);
        }});
        return formBody;
      }};

      const bindBatchControls = () => {{
        rowSelectors = Array.from(document.querySelectorAll('.row-selector[name="ids"]'));

        if (selectAllRows) {{
          selectAllRows.onchange = () => {{
            const checked = !!selectAllRows.checked;
            getVisibleRowSelectors().forEach((input) => {{ input.checked = checked; }});
            updateBatchState();
          }};
        }}

        rowSelectors.forEach((input) => {{
          input.onchange = updateBatchState;
        }});

        if (batchAction) batchAction.onchange = updateBatchState;
        updateBatchState();
      }};

      if (batchForm) {{
        batchForm.addEventListener('submit', (event) => {{
          const selectedRows = getVisibleRowSelectors().filter((input) => input.checked);
          if (!batchAction || !batchAction.value || selectedRows.length === 0) {{
            event.preventDefault();
            return;
          }}
          if (batchAction.value === 'edit-metadata') {{
            event.preventDefault();
            if (selectedRows.length !== 1) {{
              window.alert('Select exactly one row to edit metadata.');
              return;
            }}
            const row = selectedRows[0]?.closest('tr[data-row-id]');
            const title = row?.querySelector('.episode-link')?.textContent || '';
            const source = row?.querySelector('.channel-col')?.textContent || '';
            if (metadataEditIdInput) metadataEditIdInput.value = selectedRows[0].value;
            if (metadataEditTitleInput) metadataEditTitleInput.value = title.trim();
            if (metadataEditSourceInput) metadataEditSourceInput.value = source.trim();
            if (metadataEditBackdrop) {{
              metadataEditBackdrop.classList.add('is-open');
              metadataEditBackdrop.setAttribute('aria-hidden', 'false');
            }}
            return;
          }}

          event.preventDefault();
          if (batchApply) batchApply.disabled = true;
          const requestedAction = batchAction.value;

          fetch('/batch-update', {{
            method: 'POST',
            body: buildBatchRequestBody(selectedRows).toString(),
            headers: {{
              'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
              'X-Requested-With': 'fetch',
            }},
            keepalive: true,
          }})
            .catch(() => {{}})
            .finally(() => {{
              applyBatchActionLocally(requestedAction, selectedRows);
              if (batchAction) batchAction.value = '';
              if (isMediaPlaybackActive()) {{
                scheduleDeferredLibraryRefresh();
              }} else {{
                refreshLibraryViewWithoutReload();
              }}
            }});
        }});
      }}
      if (metadataEditCancel && metadataEditBackdrop) {{
        metadataEditCancel.addEventListener('click', () => {{
          metadataEditBackdrop.classList.remove('is-open');
          metadataEditBackdrop.setAttribute('aria-hidden', 'true');
        }});
      }}
      if (metadataEditBackdrop) {{
        metadataEditBackdrop.addEventListener('click', (event) => {{
          if (event.target !== metadataEditBackdrop) return;
          metadataEditBackdrop.classList.remove('is-open');
          metadataEditBackdrop.setAttribute('aria-hidden', 'true');
        }});
      }}
      if (metadataEditForm) {{
        metadataEditForm.addEventListener('submit', async (event) => {{
          event.preventDefault();
          const body = new URLSearchParams();
          if (metadataEditIdInput) body.set('id', metadataEditIdInput.value);
          if (metadataEditTitleInput) body.set('title', metadataEditTitleInput.value);
          if (metadataEditSourceInput) body.set('source_name', metadataEditSourceInput.value);
          const response = await fetch('/edit-metadata', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
            body: body.toString(),
          }});
          if (!response.ok) {{
            window.alert('Failed to update metadata.');
            return;
          }}
          window.location.reload();
        }});
      }}
      bindBatchControls();
      if (libraryFilterInput) libraryFilterInput.addEventListener('input', applyLibraryFilter);
      if (libraryFilterMode) libraryFilterMode.addEventListener('change', applyLibraryFilter);
      if (libraryFilterClear) {{
        libraryFilterClear.addEventListener('click', () => {{
          if (libraryFilterInput) libraryFilterInput.value = '';
          if (libraryFilterMode) libraryFilterMode.value = 'unplayed';
          applyLibraryFilter();
        }});
      }}
      applyLibraryFilter();

      const miniBackdrop = document.getElementById('mini-player-backdrop');
      const miniPlayer = document.getElementById('mini-player');
      const miniTitle = document.getElementById('mini-player-title');
      const miniSource = document.getElementById('mini-player-source');
      const miniAudio = document.getElementById('mini-player-audio');
      const miniVideo = document.getElementById('mini-player-video');
      const miniTranscript = document.getElementById('mini-player-transcript');
      const miniOpen = document.getElementById('mini-player-open');
      const miniClose = document.getElementById('mini-player-close');
      let miniLastPersistedSeconds = -9999;
      let miniOpenNavigationPending = false;
      let miniLastActiveCue = null;
      let miniTranscriptReady = false;

      function updatePlayLinkResumeHint(rowId, seconds) {{
        const safe = Math.max(0, Number(seconds || 0));
        document.querySelectorAll('a[data-play-link="1"][data-row-id="' + String(rowId) + '"]').forEach((link) => {{
          link.dataset.resumeSeconds = safe.toFixed(3);
        }});
      }}

      function postMiniProgress(state, seconds, force, reason) {{
        if (!state || !state.rowId) return;
        const safe = Math.max(0, Number(seconds || 0));
        updatePlayLinkResumeHint(state.rowId, safe);
        if (!force && Math.abs(safe - miniLastPersistedSeconds) < 5.0) return;
        miniLastPersistedSeconds = safe;

        const body = new URLSearchParams();
        body.set('id', String(state.rowId));
        body.set('position_seconds', safe.toFixed(3));
        body.set('reason', String(reason || 'mini-timeupdate'));
        body.set('forced', force ? '1' : '0');

        if (force && navigator.sendBeacon) {{
          const blob = new Blob([body.toString()], {{ type: 'application/x-www-form-urlencoded' }});
          if (navigator.sendBeacon('/progress', blob)) return;
        }}

        fetch('/progress', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
          body: body.toString(),
        }}).catch(() => {{}});
      }}

      function hideMiniTranscript() {{
        miniLastActiveCue = null;
        miniTranscriptReady = false;
        if (!miniTranscript) return;
        miniTranscript.classList.remove('is-visible');
        miniTranscript.textContent = '';
      }}

      function detachMiniHandlers(el) {{
        if (!el || !el._miniPersistentHandlers) return;
        const prev = el._miniPersistentHandlers;
        if (prev.timeupdate) el.removeEventListener('timeupdate', prev.timeupdate);
        if (prev.pause) el.removeEventListener('pause', prev.pause);
        if (prev.play) el.removeEventListener('play', prev.play);
        if (prev.volumechange) el.removeEventListener('volumechange', prev.volumechange);
        if (prev.ended) el.removeEventListener('ended', prev.ended);
        if (prev.subtitleTrack && prev.subtitleLoad) prev.subtitleTrack.removeEventListener('load', prev.subtitleLoad);
        if (prev.textTrack && prev.cuechange) prev.textTrack.removeEventListener('cuechange', prev.cuechange);
        delete el._miniPersistentHandlers;
      }}

      function clearMiniMedia() {{
        hideMiniTranscript();
        [miniAudio, miniVideo].forEach((el) => {{
          if (!el) return;
          detachMiniHandlers(el);
          try {{ el.pause(); }} catch (_) {{}}
          el.removeAttribute('src');
          while (el.firstChild) el.removeChild(el.firstChild);
          el.load();
          el.style.display = 'none';
        }});
      }}

      function syncMiniTranscriptFromTrack(player) {{
        if (!miniTranscript || !player || !player.textTracks || player.textTracks.length === 0) return false;
        const track = player.textTracks[0];
        if (!track) return false;

        track.mode = 'hidden';
        const cues = Array.from(track.cues || []);
        if (!cues.length) return false;

        miniTranscript.textContent = '';
        cues.forEach((cue, idx) => {{
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'mini-player-transcript-line';
          btn.dataset.idx = String(idx);
          btn.textContent = (cue.text || '').replace(/\\s+/g, ' ').trim();
          btn.addEventListener('click', () => {{
            player.currentTime = Math.max(0, cue.startTime || 0);
            player.play().catch(() => {{}});
          }});
          miniTranscript.appendChild(btn);
        }});

        const existing = player._miniPersistentHandlers;
        if (existing && existing.textTrack && existing.cuechange) {{
          existing.textTrack.removeEventListener('cuechange', existing.cuechange);
        }}

        const onCueChange = () => {{
          const activeCue = track.activeCues && track.activeCues.length ? track.activeCues[0] : null;
          if (activeCue === miniLastActiveCue) return;
          miniLastActiveCue = activeCue;

          const activeIndex = cues.indexOf(activeCue);
          miniTranscript.querySelectorAll('.mini-player-transcript-line').forEach((line, idx) => {{
            if (idx === activeIndex) {{
              line.classList.add('active');
              line.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
            }} else {{
              line.classList.remove('active');
            }}
          }});
        }};

        track.addEventListener('cuechange', onCueChange);
        if (existing) {{
          existing.textTrack = track;
          existing.cuechange = onCueChange;
        }}
        onCueChange();
        miniTranscript.classList.add('is-visible');
        miniTranscriptReady = true;
        return true;
      }}

      function scheduleMiniTranscriptInit(state, player, subtitleTrackEl) {{
        if (!miniTranscript || !state || !state.hasSubtitles || state.kind !== 'audio') {{
          hideMiniTranscript();
          return;
        }}
        miniTranscript.classList.add('is-visible');
        miniTranscript.textContent = 'Loading transcript…';
        let attempts = 0;
        const maxAttempts = 40;
        const timer = window.setInterval(() => {{
          attempts += 1;
          if (syncMiniTranscriptFromTrack(player) || attempts >= maxAttempts) {{
            window.clearInterval(timer);
            if (!miniTranscriptReady) miniTranscript.textContent = 'No subtitle cues available.';
          }}
        }}, 150);
        if (subtitleTrackEl) {{
          const onSubtitleLoad = () => syncMiniTranscriptFromTrack(player);
          subtitleTrackEl.addEventListener('load', onSubtitleLoad);
          if (player._miniPersistentHandlers) {{
            player._miniPersistentHandlers.subtitleTrack = subtitleTrackEl;
            player._miniPersistentHandlers.subtitleLoad = onSubtitleLoad;
          }}
        }}
      }}

      function setMiniExpanded(expanded) {{
        if (!miniPlayer || !miniBackdrop || !miniOpen) return;
        const isExpanded = !!expanded;
        miniPlayer.classList.toggle('is-maximized', isExpanded);
        miniOpen.textContent = isExpanded ? 'Minimize' : 'Maximize';
        miniOpen.setAttribute('aria-label', isExpanded ? 'Minimize player' : 'Maximize player');
        if (isExpanded) {{
          miniBackdrop.classList.add('is-open');
          miniBackdrop.setAttribute('aria-hidden', 'false');
        }} else {{
          miniBackdrop.classList.remove('is-open');
          miniBackdrop.setAttribute('aria-hidden', 'true');
        }}

        if (isExpanded && !miniTranscriptReady) {{
          const raw = localStorage.getItem('getofflineMiniPlayerState');
          let state = null;
          try {{ state = raw ? JSON.parse(raw) : null; }} catch (_) {{ state = null; }}
          if (state && state.kind === 'audio' && state.hasSubtitles) {{
            const active = miniAudio && miniAudio.style.display !== 'none' ? miniAudio : null;
            if (active) {{
              const subtitleTrackEl = active.querySelector('track[kind="subtitles"]');
              scheduleMiniTranscriptInit(state, active, subtitleTrackEl);
            }}
          }}
        }}
      }}

      function renderMiniPlayer(stateInput) {{
        if (!miniPlayer || !miniAudio || !miniVideo || !miniBackdrop) return;
        let state = stateInput || null;
        if (!state) {{
          const raw = localStorage.getItem('getofflineMiniPlayerState');
          if (!raw) return;
          try {{ state = JSON.parse(raw); }} catch (_) {{ return; }}
        }}
        if (!state || !state.rowId || !state.src || !state.kind) return;

        clearMiniMedia();
        if (miniTitle) miniTitle.textContent = state.title || 'Now playing';
        if (miniSource) miniSource.textContent = state.source || '';

        const active = state.kind === 'video' ? miniVideo : miniAudio;
        active.style.display = 'block';
        applyStoredMediaSettings(active);
        const source = document.createElement('source');
        source.src = state.src;
        active.appendChild(source);

        let subtitleTrackEl = null;
        if (state.kind === 'audio' && state.hasSubtitles) {{
          subtitleTrackEl = document.createElement('track');
          subtitleTrackEl.kind = 'subtitles';
          subtitleTrackEl.srclang = 'en';
          subtitleTrackEl.label = 'English';
          subtitleTrackEl.default = true;
          subtitleTrackEl.src = '/subtitle?id=' + state.rowId;
          active.appendChild(subtitleTrackEl);
        }}

        active.addEventListener('loadedmetadata', () => {{
          active.currentTime = Math.max(0, Number(state.currentTime || 0));
          if (!state.paused) active.play().catch(() => {{}});
        }}, {{ once: true }});
        active.addEventListener('loadeddata', () => scheduleMiniTranscriptInit(state, active, subtitleTrackEl), {{ once: true }});
        active.load();

        detachMiniHandlers(active);

        const persist = () => {{
          localStorage.setItem('getofflineMiniPlayerState', JSON.stringify({{
            ...state,
            currentTime: active.currentTime || 0,
            paused: active.paused,
          }}));
        }};
        const timeupdateHandler = () => {{
          persist();
          if (!active.paused) postMiniProgress(state, active.currentTime || 0, false, 'mini-timeupdate');
        }};
        const pauseHandler = () => {{
          persist();
          postMiniProgress(state, active.currentTime || 0, true, 'mini-pause');
        }};
        const playHandler = () => {{
          persist();
        }};
        const volumeHandler = () => {{
          persistMediaSettings(active);
        }};
        const endedHandler = () => {{
          postMiniProgress(state, 0, true, 'mini-ended');
          closeMiniPlayer();
        }};

        active._miniPersistentHandlers = {{
          timeupdate: timeupdateHandler,
          pause: pauseHandler,
          play: playHandler,
          volumechange: volumeHandler,
          ended: endedHandler,
        }};

        active.addEventListener('timeupdate', timeupdateHandler);
        active.addEventListener('pause', pauseHandler);
        active.addEventListener('play', playHandler);
        active.addEventListener('volumechange', volumeHandler);
        active.addEventListener('ended', endedHandler);

        miniPlayer.classList.add('is-visible');
        setMiniExpanded(false);
      }}

      function closeMiniPlayer() {{
        const raw = localStorage.getItem('getofflineMiniPlayerState');
        let state = null;
        try {{ state = raw ? JSON.parse(raw) : null; }} catch (_) {{ state = null; }}
        const active = state && state.kind === 'video' ? miniVideo : miniAudio;
        if (state && active && active.style.display !== 'none') {{
          postMiniProgress(state, active.currentTime || 0, true, 'mini-close');
        }}

        localStorage.removeItem('getofflineMiniPlayerState');
        clearMiniMedia();
        if (miniPlayer) miniPlayer.classList.remove('is-visible');
        setMiniExpanded(false);
      }}

      const bindPlayLinks = () => {{
        const hideSummaryTooltip = () => {{
          if (!summaryTooltip) return;
          summaryTooltip.classList.remove('is-visible');
          summaryTooltip.setAttribute('aria-hidden', 'true');
        }};
        const placeSummaryTooltip = (event) => {{
          if (!summaryTooltip) return;
          const pad = 12;
          const rect = summaryTooltip.getBoundingClientRect();
          let x = event.clientX + 14;
          let y = event.clientY + 14;
          if (x + rect.width > window.innerWidth - pad) x = Math.max(pad, event.clientX - rect.width - 14);
          if (y + rect.height > window.innerHeight - pad) y = Math.max(pad, event.clientY - rect.height - 14);
          summaryTooltip.style.left = x + 'px';
          summaryTooltip.style.top = y + 'px';
        }};
        document.querySelectorAll('a[data-play-link="1"]').forEach((link) => {{
          if (link.dataset.miniPlayerBound === '1') return;
          link.dataset.miniPlayerBound = '1';
          if (link.dataset.summaryBound !== '1') {{
            link.dataset.summaryBound = '1';
            link.addEventListener('mouseenter', (event) => {{
              if (!summaryTooltip) return;
              const summaryText = String(link.dataset.summary || link.dataset.title || '').trim();
              if (!summaryText) return;
              summaryTooltip.textContent = summaryText;
              summaryTooltip.classList.add('is-visible');
              summaryTooltip.setAttribute('aria-hidden', 'false');
              placeSummaryTooltip(event);
            }});
            link.addEventListener('mousemove', placeSummaryTooltip);
            link.addEventListener('mouseleave', hideSummaryTooltip);
            link.addEventListener('blur', hideSummaryTooltip);
          }}
          link.addEventListener('click', (event) => {{
            if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            const rowId = Number(link.dataset.rowId || 0);
            if (!rowId) return;
            const resumeSeconds = Math.max(0, Number(link.dataset.resumeSeconds || 0));
            const state = {{
              rowId,
              title: link.dataset.title || '',
              source: link.dataset.source || '',
              kind: link.dataset.kind || 'audio',
              hasSubtitles: link.dataset.hasSubtitles === '1',
              src: '/media?id=' + rowId,
              playUrl: '/play?id=' + rowId,
              currentTime: resumeSeconds,
              paused: false,
            }};
            localStorage.setItem('getofflineMiniPlayerState', JSON.stringify(state));
            renderMiniPlayer(state);
            hideSummaryTooltip();
          }});
        }});
      }};
      bindPlayLinks();

      if (miniOpen) {{
        miniOpen.addEventListener('click', () => {{
          suppressSyncAutoReload = true;
          if (syncReloadTimer !== null) {{
            window.clearTimeout(syncReloadTimer);
            syncReloadTimer = null;
          }}
          if (miniOpenNavigationPending) {{
            return;
          }}
          miniOpenNavigationPending = true;
          miniOpen.setAttribute('aria-disabled', 'true');
          miniOpen.style.pointerEvents = 'none';
          window.setTimeout(() => {{
            miniOpenNavigationPending = false;
            miniOpen.removeAttribute('aria-disabled');
            miniOpen.style.pointerEvents = '';
          }}, 300);

          const raw = localStorage.getItem('getofflineMiniPlayerState');
          if (!raw) return;
          let state = null;
          try {{ state = JSON.parse(raw); }} catch (_) {{ return; }}
          if (!state || !state.rowId) return;

          const active = state.kind === 'video' ? miniVideo : miniAudio;
          if (active && active.style.display !== 'none') {{
            state.currentTime = active.currentTime || 0;
            state.paused = active.paused;
            postMiniProgress(state, state.currentTime || 0, true, 'mini-open');
            localStorage.setItem('getofflineMiniPlayerState', JSON.stringify(state));
          }}

          const currentlyExpanded = miniPlayer && miniPlayer.classList.contains('is-maximized');
          setMiniExpanded(!currentlyExpanded);
        }});
      }}

      if (miniBackdrop) {{
        miniBackdrop.addEventListener('click', (event) => {{
          if (event.target === miniBackdrop) closeMiniPlayer();
        }});
      }}

      document.addEventListener('keydown', (event) => {{
        if (event.key === 'Escape' && miniBackdrop && miniBackdrop.classList.contains('is-open')) closeMiniPlayer();
      }});

      if (miniClose) {{
        miniClose.addEventListener('click', () => {{
          closeMiniPlayer();
        }});
      }}

      const persistedMiniPlayerRaw = localStorage.getItem('getofflineMiniPlayerState');
      if (persistedMiniPlayerRaw) {{
        try {{
          const persistedMiniPlayerState = JSON.parse(persistedMiniPlayerRaw);
          if (persistedMiniPlayerState && persistedMiniPlayerState.paused === false) renderMiniPlayer(persistedMiniPlayerState);
        }} catch (_) {{}}
      }}


      const openBtn = document.getElementById('quick-add-open');
      const backdrop = document.getElementById('quick-add-backdrop');
      const cancelBtn = document.getElementById('quick-add-cancel');
      const urlInput = document.getElementById('quick-add-url');
      const quickAddForm = document.getElementById('quick-add-form');
      const quickMediaTypeSelect = document.getElementById('quick-add-media-type');

      const quickAddSearchInput = document.getElementById('quick-add-search');
      const quickAddResults = document.getElementById('quick-add-results');
      const transcriptSearchOpen = document.getElementById('transcript-search-open');
      const transcriptSearchBackdrop = document.getElementById('transcript-search-backdrop');
      const transcriptSearchClose = document.getElementById('transcript-search-close');
      const transcriptSearchInput = document.getElementById('transcript-search-input');
      const transcriptSearchResults = document.getElementById('transcript-search-results');

      const closeQuickAddModal = () => {{
        if (!backdrop) return;
        backdrop.classList.remove('is-open');
        backdrop.setAttribute('aria-hidden', 'true');
      }};

      const submitQuickDownload = (url, mediaType, onDone) => {{
        const safeUrl = String(url || '').trim();
        if (!safeUrl) return;

        const formBody = new window.URLSearchParams();
        formBody.set('url', safeUrl);
        formBody.set('media_type', mediaType || 'video');

        setSyncButtonRunning();
        fetch('/quick-add-youtube', {{
          method: 'POST',
          body: formBody.toString(),
          keepalive: true,
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8' }},
        }})
          .catch(() => {{}})
          .finally(() => {{
            if (typeof onDone === 'function') onDone();
            pollSyncStatusUntilFinished();
          }});
      }};

      const renderQuickAddResults = (items) => {{
        if (!quickAddResults) return;
        if (!Array.isArray(items) || items.length === 0) {{
          quickAddResults.innerHTML = '<div class="quick-add-empty">No videos found.</div>';
          return;
        }}

        quickAddResults.innerHTML = items.map((item) => {{
          const title = String(item.title || 'Untitled');
          const channel = String(item.channel || '');
          const duration = String(item.duration || '');
          const url = String(item.url || '');
          const thumbnail = String(item.thumbnail || '');
          const sub = [channel, duration].filter(Boolean).join(' • ');

          return `
            <div class="quick-add-result">
              <img class="quick-add-thumb" src="${{thumbnail}}" alt="${{title}} thumbnail" loading="lazy" referrerpolicy="no-referrer" />
              <div>
                <div class="quick-add-meta-title">${{title}}</div>
                <div class="quick-add-meta-sub">${{sub}}</div>
              </div>
              <button type="button" class="primary" data-download-url="${{url}}">Download</button>
            </div>
          `;
        }}).join('');
      }};

      const runQuickAddSearch = () => {{
        if (!quickAddSearchInput || !quickAddResults) return;
        const q = String(quickAddSearchInput.value || '').trim();
        if (!q) {{
          quickAddResults.innerHTML = '<div class="quick-add-empty">Type a query to search YouTube.</div>';
          return;
        }}

        quickAddResults.innerHTML = '<div class="quick-add-empty">Searching...</div>';
        fetch('/youtube-search?q=' + encodeURIComponent(q), {{ cache: 'no-store' }})
          .then((response) => response.ok ? response.json() : null)
          .then((payload) => {{
            renderQuickAddResults(payload && payload.results ? payload.results : []);
          }})
          .catch(() => {{
            quickAddResults.innerHTML = '<div class="quick-add-empty">Search failed. Try again.</div>';
          }});
      }};

      if (openBtn && backdrop) {{
        openBtn.addEventListener('click', () => {{
          backdrop.classList.add('is-open');
          backdrop.setAttribute('aria-hidden', 'false');
          if (quickAddSearchInput) quickAddSearchInput.focus();
          if (quickAddResults) quickAddResults.innerHTML = '<div class="quick-add-empty">Paste a YouTube URL or press Enter to search.</div>';
        }});
      }}
      if (cancelBtn) cancelBtn.addEventListener('click', closeQuickAddModal);
      [backdrop].forEach((modalBackdrop) => {{
        if (!modalBackdrop) return;
        modalBackdrop.addEventListener('click', (event) => {{
          if (event.target !== modalBackdrop) return;
          if (modalBackdrop === backdrop) closeQuickAddModal();
        }});
      }});
      document.addEventListener('keydown', (event) => {{
        if (event.key !== 'Escape') return;
        if (backdrop && backdrop.classList.contains('is-open')) closeQuickAddModal();
      }});
      if (quickAddSearchInput) {{
        quickAddSearchInput.addEventListener('keydown', (event) => {{
          if (event.key === 'Enter') {{
            event.preventDefault();
            runQuickAddSearch();
          }}
        }});
      }}
      if (transcriptSearchOpen && transcriptSearchBackdrop && transcriptSearchInput && transcriptSearchResults) {{
        const closeTranscriptSearch = () => {{
          transcriptSearchBackdrop.classList.remove('is-open');
          transcriptSearchBackdrop.setAttribute('aria-hidden', 'true');
        }};
        transcriptSearchOpen.addEventListener('click', () => {{
          transcriptSearchBackdrop.classList.add('is-open');
          transcriptSearchBackdrop.setAttribute('aria-hidden', 'false');
          transcriptSearchInput.focus();
          transcriptSearchResults.innerHTML = '<div class="quick-add-empty">Type words to search transcripts.</div>';
        }});
        if (transcriptSearchClose) transcriptSearchClose.addEventListener('click', closeTranscriptSearch);
        transcriptSearchBackdrop.addEventListener('click', (event) => {{
          if (event.target === transcriptSearchBackdrop) closeTranscriptSearch();
        }});
        transcriptSearchInput.addEventListener('input', () => {{
          const q = String(transcriptSearchInput.value || '').trim();
          if (!q) return;
          fetch('/transcript-search?q=' + encodeURIComponent(q), {{ cache: 'no-store' }})
            .then((response) => {{
              if (!response.ok) {{
                throw new Error('transcript-search http ' + response.status);
              }}
              return response.json();
            }})
            .then((payload) => {{
              const results = payload && payload.results ? payload.results : [];
              if (!results.length) {{
                transcriptSearchResults.innerHTML = '<div class="quick-add-empty">No matches found.</div>';
                return;
              }}
              transcriptSearchResults.innerHTML = results.map((item) =>
                '<button type="button" class="quick-add-result" data-row-id="' + item.row_id + '" data-start="' + item.start_seconds + '">' +
                '<strong>' + escapeHtml(item.title || '') + '</strong><div>' + escapeHtml(item.text || '') + '</div></button>'
              ).join('');
              transcriptSearchResults.querySelectorAll('button[data-row-id]').forEach((btn) => {{
                btn.addEventListener('click', () => {{
                  const rowId = btn.getAttribute('data-row-id');
                  const start = btn.getAttribute('data-start');
                  const rawStart = Number(start || 0);
                  const seekStart = Math.max(0, rawStart - 2.0);
                  window.location.href = '/play?id=' + encodeURIComponent(rowId) + '&t=' + encodeURIComponent(seekStart) + '&autoplay=1';
                }});
              }});
            }})
            .catch((error) => {{
              try {{ console.error('Transcript search failed', {{ query: q, error }}); }} catch (_) {{}}
              transcriptSearchResults.innerHTML = '<div class="quick-add-empty">Search failed.</div>';
            }});
        }});
      }}
      if (quickAddResults) {{
        quickAddResults.addEventListener('click', (event) => {{
          const target = event.target;
          if (!(target instanceof HTMLElement)) return;
          const downloadUrl = target.getAttribute('data-download-url');
          if (!downloadUrl) return;
          if (urlInput) urlInput.value = downloadUrl;
          submitQuickDownload(downloadUrl, quickMediaTypeSelect ? quickMediaTypeSelect.value : 'video');
          closeQuickAddModal();
        }});
      }};

      if (quickAddForm) {{
        quickAddForm.addEventListener('submit', (event) => {{
          event.preventDefault();
          closeQuickAddModal();
          submitQuickDownload(urlInput ? urlInput.value : '', quickMediaTypeSelect ? quickMediaTypeSelect.value : 'video');
        }});
      }}
    }})();
  </script>
</body>
</html>"""


def _render_player(row: MediaRow, media_path: Path, resume_seconds: float, has_subtitles: bool) -> str:
    title = html.escape(row.title or media_path.name)
    media_kind = "video" if media_path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} else "audio"
    has_subtitles = has_subtitles and media_kind == "audio"
    source = html.escape(f"{row.source_type}: {row.source_name}")

    resume_value = max(0.0, float(resume_seconds or 0.0))
    subtitles_html = (
        f'<track id="subtitle-track" kind="subtitles" srclang="en" label="English" src="/subtitle?id={row.row_id}" default />'
        if has_subtitles
        else ""
    )
    transcript_html = ""
    if media_kind == "audio" and has_subtitles:
        transcript_html = """
    <section class="transcript-wrap">
      <div id="transcript" class="transcript" aria-live="polite"></div>
    </section>
"""

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      background: #0b1020;
      color: #e9eefc;
      padding: 1.25rem;
    }}
    .wrap {{ max-width: 1100px; margin: 0 auto; }}
    a {{ color: #9dbbff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .meta {{ color: #a9b4d0; margin: .25rem 0 1rem 0; }}
    .player {{
      width: 100%;
      max-width: 1000px;
      background: #000;
      border: 1px solid #2a3761;
      border-radius: 12px;
      box-shadow: 0 20px 60px rgba(0,0,0,.35);
    }}
    .transcript-wrap {{ margin-top: 1rem; max-width: 1000px; }}
    .transcript-wrap h3 {{ margin: 0 0 .45rem 0; font-size: 1rem; color: #b8c4e6; }}
    .transcript {{
      max-height: 260px;
      overflow-y: auto;
      border: 1px solid #2a3761;
      border-radius: 10px;
      background: #0f1730;
      padding: .6rem;
    }}
    .transcript-line {{
      display: block;
      width: 100%;
      text-align: left;
      color: #c8d4f4;
      background: transparent;
      border: none;
      border-radius: 8px;
      margin: 0;
      padding: .35rem .45rem;
      cursor: pointer;
      line-height: 1.35;
    }}
    .transcript-line:hover {{ background: #1a2444; }}
    .transcript-line.active {{ background: #2a427f; color: #f2f6ff; }}
  </style>
</head>
<body>
  <div class="wrap">
    <p><a id="back-to-library" href="/">← Back to Library</a></p>
    <h2>{title}</h2>
    <p class="meta">{source}</p>
    <{media_kind} id="player" class="player" controls preload="metadata">
      <source src="/media?id={row.row_id}" />
      {subtitles_html}
      Your browser does not support this media type.
    </{media_kind}>
    {transcript_html}
  </div>
  <script>
    (function() {{
      const rowId = {row.row_id};
      const startSeconds = {resume_value:.6f};
      const player = document.getElementById('player');
      const backToLibrary = document.getElementById('back-to-library');
      const shouldAutoPlay = new URLSearchParams(window.location.search).get('autoplay') === '1';
      const mediaSettingsStorageKey = 'getofflineMediaElementSettings';
      const resumeLabel = document.getElementById('resume-label');
      const transcript = document.getElementById('transcript');
      const subtitleTrackEl = document.getElementById('subtitle-track');
      let lastSentSeconds = -9999;
      let progressInFlight = false;
      let queuedProgressSeconds = null;
      let progressController = null;
      const periodicProgressSeconds = 5.0;
      const progressRequestTimeoutMs = 2000;
      let hasAppliedInitialSeek = false;
      let lastActiveCue = null;
      let transcriptReady = false;
      let hasSentPageExitProgress = false;
      let lastForcedProgressAt = 0;
      let playbackCompleted = false;

      if (!player) return;

      function readStoredMediaSettings() {{
        const raw = window.localStorage.getItem(mediaSettingsStorageKey);
        if (!raw) return null;
        try {{
          const parsed = JSON.parse(raw);
          if (!parsed || typeof parsed !== 'object') return null;
          const volume = Number(parsed.volume);
          return {{
            volume: Number.isFinite(volume) ? Math.min(1, Math.max(0, volume)) : null,
            muted: !!parsed.muted,
          }};
        }} catch (_) {{
          return null;
        }}
      }}

      function applyStoredMediaSettings() {{
        const stored = readStoredMediaSettings();
        if (!stored) return;
        if (stored.volume !== null) player.volume = stored.volume;
        player.muted = !!stored.muted;
      }}

      function persistMediaSettings() {{
        window.localStorage.setItem(mediaSettingsStorageKey, JSON.stringify({{
          volume: Number(player.volume),
          muted: !!player.muted,
        }}));
      }}

      function updateLabel(seconds) {{
        if (!resumeLabel) return;
        resumeLabel.textContent = Number(seconds || 0).toFixed(1) + 's';
      }}

      function getMiniPlayerResumeSeconds() {{
        const raw = localStorage.getItem('getofflineMiniPlayerState');
        if (!raw) return null;
        try {{
          const state = JSON.parse(raw);
          if (!state || Number(state.rowId || 0) !== rowId) return null;
          const candidate = Number(state.currentTime || 0);
          return Number.isFinite(candidate) && candidate > 0 ? candidate : null;
        }} catch (_) {{
          return null;
        }}
      }}

      function sendProgressRequest(seconds, keepalive, reason, forced) {{
        const body = new URLSearchParams();
        body.set('id', String(rowId));
        body.set('position_seconds', seconds.toFixed(3));
        body.set('reason', String(reason || 'unknown'));
        body.set('forced', forced ? '1' : '0');

        const options = {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
          body: body.toString(),
        }};

        let abortTimer = null;
        if (!keepalive) {{
          progressController = new AbortController();
          options.signal = progressController.signal;
          abortTimer = window.setTimeout(() => {{
            if (!progressController) return;
            progressController.abort();
          }}, progressRequestTimeoutMs);
        }}

        return fetch('/progress', options)
          .then((response) => {{
            if (!response.ok) console.warn('[getoffline] /progress failed with status', response.status);
            return response;
          }})
          .catch((err) => {{
            if (err && err.name === 'AbortError') return null;
            console.warn('[getoffline] /progress request failed', err);
            return null;
          }})
          .finally(() => {{
            if (abortTimer !== null) window.clearTimeout(abortTimer);
            progressController = null;
          }});
      }}

      function postProgress(seconds, force, reason) {{
        const safe = Math.max(0, Number(seconds || 0));
        if (!force && Math.abs(safe - lastSentSeconds) < periodicProgressSeconds) return;
        if (force) {{
          const nowMs = Date.now();
          if ((nowMs - lastForcedProgressAt) < 1200 && reason !== 'ended' && reason !== 'page-exit') return;
          lastForcedProgressAt = nowMs;
        }}
        updateLabel(safe);
        lastSentSeconds = safe;

        if (force) {{
          const beaconBody = new URLSearchParams();
          beaconBody.set('id', String(rowId));
          beaconBody.set('position_seconds', safe.toFixed(3));
          beaconBody.set('reason', String(reason || 'unknown'));
          beaconBody.set('forced', '1');
          if (navigator.sendBeacon) {{
            const blob = new Blob([beaconBody.toString()], {{ type: 'application/x-www-form-urlencoded' }});
            if (navigator.sendBeacon('/progress', blob)) return;
          }}

          // During navigation lifecycle events, avoid starting extra fetches that can be cancelled.
          if (reason === 'page-exit' || reason === 'back-link' || reason === 'pause') return;

          sendProgressRequest(safe, false, reason || 'forced', true);
          return;
        }}

        if (progressInFlight) {{
          queuedProgressSeconds = safe;
          return;
        }}

        progressInFlight = true;
        queuedProgressSeconds = null;
        sendProgressRequest(safe, false, reason || 'timeupdate', false).finally(() => {{
          progressInFlight = false;
          if (queuedProgressSeconds === null) return;
          const queued = queuedProgressSeconds;
          queuedProgressSeconds = null;
          postProgress(queued, false, reason);
        }});
      }}

      function persistMiniPlayerState() {{
        if (playbackCompleted) return;
        localStorage.setItem('getofflineMiniPlayerState', JSON.stringify({{
          rowId,
          title: {json.dumps(row.title or media_path.name)},
          source: {json.dumps(row.source_name or '')},
          kind: {json.dumps(media_kind)},
          src: '/media?id=' + rowId,
          playUrl: '/play?id=' + rowId,
          currentTime: player.currentTime || 0,
          paused: player.paused,
        }}));
      }}

      function applyInitialSeek() {{
        if (hasAppliedInitialSeek) return;
        const initialSeconds = getMiniPlayerResumeSeconds() ?? startSeconds;
        if (initialSeconds <= 0) return;
        const target = Number.isFinite(player.duration) && player.duration > 1
          ? Math.min(initialSeconds, Math.max(player.duration - 1, 0))
          : initialSeconds;
        try {{
          player.currentTime = target;
          hasAppliedInitialSeek = true;
          updateLabel(target);
        }} catch (_) {{}}
      }}

      function syncTranscriptFromTrack() {{
        if (!transcript || !player.textTracks || player.textTracks.length === 0) return false;
        const track = player.textTracks[0];
        if (!track) return false;

        track.mode = 'hidden';
        const cues = Array.from(track.cues || []);
        if (!cues.length) return false;

        transcript.textContent = '';
        cues.forEach((cue, idx) => {{
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'transcript-line';
          btn.dataset.idx = String(idx);
          btn.textContent = (cue.text || '').replace(/\\s+/g, ' ').trim();
          btn.addEventListener('click', () => {{
            player.currentTime = Math.max(0, cue.startTime || 0);
            player.play().catch(() => {{}});
          }});
          transcript.appendChild(btn);
        }});

        const onCueChange = () => {{
          const active = track.activeCues && track.activeCues.length ? track.activeCues[0] : null;
          if (active === lastActiveCue) return;
          lastActiveCue = active;

          const activeIndex = cues.indexOf(active);
          const lines = transcript.querySelectorAll('.transcript-line');
          lines.forEach((line, idx) => {{
            if (idx === activeIndex) {{
              line.classList.add('active');
              line.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
            }} else {{
              line.classList.remove('active');
            }}
          }});
        }};

        track.removeEventListener('cuechange', onCueChange);
        track.addEventListener('cuechange', onCueChange);
        onCueChange();
        transcriptReady = true;
        return true;
      }}

      function scheduleTranscriptInit() {{
        if (!transcript || transcriptReady) return;
        transcript.textContent = 'Loading transcript…';

        let attempts = 0;
        const maxAttempts = 40;
        const timer = setInterval(() => {{
          attempts += 1;
          if (syncTranscriptFromTrack() || attempts >= maxAttempts) {{
            clearInterval(timer);
            if (!transcriptReady) transcript.textContent = 'No subtitle cues available.';
          }}
        }}, 150);
      }}

      player.addEventListener('loadedmetadata', applyInitialSeek);
      player.addEventListener('canplay', applyInitialSeek);
      player.addEventListener('loadedmetadata', applyStoredMediaSettings);
      player.addEventListener('canplay', () => {{
        if (shouldAutoPlay) player.play().catch(() => {{}});
      }});
      player.addEventListener('playing', applyInitialSeek);
      player.addEventListener('volumechange', persistMediaSettings);
      player.addEventListener('loadeddata', scheduleTranscriptInit);
      window.addEventListener('pageshow', scheduleTranscriptInit);
      if (subtitleTrackEl) subtitleTrackEl.addEventListener('load', scheduleTranscriptInit);
      scheduleTranscriptInit();

      player.addEventListener('timeupdate', () => {{
        persistMiniPlayerState();
        if (!player.paused) postProgress(player.currentTime, false);
      }});
      player.addEventListener('pause', () => {{
        persistMiniPlayerState();
        postProgress(player.currentTime, true, 'pause');
      }});
      player.addEventListener('play', persistMiniPlayerState);
      player.addEventListener('ended', () => {{
        playbackCompleted = true;
        try {{ player.currentTime = 0; }} catch (_) {{}}
        localStorage.removeItem('getofflineMiniPlayerState');
        postProgress(0, true, 'ended');
      }});

      if (backToLibrary) {{
        backToLibrary.addEventListener('click', () => {{
          if (playbackCompleted) {{
            localStorage.removeItem('getofflineMiniPlayerState');
            postProgress(0, true, 'back-link');
            return;
          }}
          persistMiniPlayerState();
          postProgress(player.currentTime, true, 'back-link');
        }});
      }}

      function sendPageExitProgress() {{
        if (hasSentPageExitProgress) return;
        hasSentPageExitProgress = true;
        if (playbackCompleted) {{
          postProgress(0, true, 'page-exit');
          return;
        }}
        postProgress(player.currentTime, true, 'page-exit');
      }}

      window.addEventListener('pagehide', sendPageExitProgress);
    }})();
  </script>
</body>
</html>"""


def _render_settings(config: Dict[str, Dict[str, object]]) -> str:
    defaults = config.get("defaults", {})
    cookie_text = str((config.get("download_settings") or {}).get("youtube_cookie_text") or "")
    output_root = html.escape(str(defaults.get("output_root") or ""))
    audio_format = html.escape(str(defaults.get("audio_format") or "mp3"))
    audio_quality = html.escape(str(defaults.get("audio_quality") or "0"))
    ffmpeg_audio_filter = html.escape(str(defaults.get("ffmpeg_audio_filter") or ""))
    max_downloads = html.escape(str(defaults.get("max_downloads") or "3"))
    playlist_end = html.escape(str(defaults.get("playlist_end") or "3"))
    processing_workers = html.escape(str(defaults.get("processing_workers") or "2"))
    auto_update_minutes = html.escape(str(defaults.get("auto_update_minutes") or str(DEFAULT_AUTO_UPDATE_MINUTES)))
    summary_model = html.escape(str(defaults.get("summary_model") or "qwen2.5:0.5b"))
    ollama_path = html.escape(str(defaults.get("ollama_path") or "ollama"))
    deno_path = html.escape(str(defaults.get("deno_path") or "deno"))
    def default_checked(key: str, fallback: bool = False) -> str:
        value = defaults.get(key, fallback)
        if isinstance(value, bool):
            checked = value
        else:
            checked = str(value or "").strip().lower() in {"1", "true", "yes", "on"}
        return " checked" if checked else ""

    android_sync_enabled_checked = default_checked("android_sync_enabled")
    android_sync_adb_path_raw = str(defaults.get("android_sync_adb_path") or "adb")
    android_sync_adb_path = html.escape(android_sync_adb_path_raw)
    resolved_android_sync_adb_path = html.escape(str(shutil.which(android_sync_adb_path_raw) or "not found"))
    android_sync_connection_mode_raw = str(defaults.get("android_sync_connection_mode") or "usb").strip().lower()
    android_sync_usb_selected = " selected" if android_sync_connection_mode_raw != "wifi" else ""
    android_sync_wifi_selected = " selected" if android_sync_connection_mode_raw == "wifi" else ""
    android_sync_wifi_address = html.escape(str(defaults.get("android_sync_wifi_address") or ""))
    android_sync_destination = html.escape(str(defaults.get("android_sync_destination") or "/sdcard/Movies/GetOffline"))
    android_sync_max_items = html.escape(str(defaults.get("android_sync_max_items") or "10"))
    android_sync_include_subtitles_checked = default_checked("android_sync_include_subtitles", True)
    android_sync_include_unplayed_checked = default_checked("android_sync_include_unplayed", True)
    android_sync_include_started_checked = default_checked("android_sync_include_started", True)
    android_sync_include_played_checked = default_checked("android_sync_include_played", False)
    android_sync_exclude_regex = html.escape(str(defaults.get("android_sync_exclude_regex") or ""))
    resolved_ollama_path = html.escape(str(shutil.which(str(defaults.get("ollama_path") or "ollama")) or "not found"))
    resolved_deno_path = html.escape(str(shutil.which(str(defaults.get("deno_path") or "deno")) or "not found"))
    telemetry_dumps_enabled = bool(defaults.get("telemetry_dumps_enabled"))
    telemetry_dumps_checked = " checked" if telemetry_dumps_enabled else ""
    cookie_value = html.escape(cookie_text)

    youtube_cards = []
    for item in config.get("youtube") or []:
        row_id = int(item.get("id") or 0)
        name = html.escape(str(item.get("name") or ""))
        url = html.escape(str(item.get("url") or ""))
        media_type_value = str(item.get("type") or "audio")
        subtitles_enabled = bool(item.get("subtitles", True))
        enabled = bool(item.get("enabled", True))
        status = "enabled" if enabled else "disabled"
        subtitle_offset = item.get("subtitle_offset_seconds")
        subtitle_offset_text = html.escape("" if subtitle_offset is None else str(subtitle_offset))
        source_max_downloads = item.get("max_downloads")
        source_max_downloads_text = html.escape("" if source_max_downloads is None else str(source_max_downloads))
        media_audio_selected = " selected" if media_type_value == "audio" else ""
        media_video_selected = " selected" if media_type_value == "video" else ""
        subtitles_yes_selected = " selected" if subtitles_enabled else ""
        subtitles_no_selected = "" if subtitles_enabled else " selected"
        enabled_selected = " selected" if enabled else ""
        disabled_selected = "" if enabled else " selected"
        youtube_cards.append(
            f"""
            <details class="source-card">
              <summary>
                <span class="source-card-title">{name or "Unnamed YouTube source"}</span>
                <span class="source-card-url">{url}</span>
                <span class="row-status">{status}</span>
              </summary>
              <input type="hidden" name="source_id" value="{row_id}" />
              <div class="source-card-grid">
                <div>
                  <label for="youtube_name_{row_id}">Name</label>
                  <input id="youtube_name_{row_id}" type="text" name="name_{row_id}" value="{name}" required />
                </div>
                <div>
                  <label for="youtube_url_{row_id}">URL</label>
                  <input id="youtube_url_{row_id}" type="url" name="url_{row_id}" value="{url}" required />
                </div>
                <div>
                  <label for="youtube_media_type_{row_id}">Download type</label>
                  <select id="youtube_media_type_{row_id}" name="media_type_{row_id}"><option value="audio"{media_audio_selected}>audio</option><option value="video"{media_video_selected}>video</option></select>
                </div>
                <div>
                  <label for="youtube_subtitles_{row_id}">Subtitles</label>
                  <select id="youtube_subtitles_{row_id}" name="subtitles_{row_id}"><option value="1"{subtitles_yes_selected}>yes</option><option value="0"{subtitles_no_selected}>no</option></select>
                </div>
                <div>
                  <label for="youtube_offset_{row_id}">Subtitle offset seconds</label>
                  <input id="youtube_offset_{row_id}" type="text" name="subtitle_offset_seconds_{row_id}" value="{subtitle_offset_text}" placeholder="offset (optional)" />
                </div>
                <div>
                  <label for="youtube_max_downloads_{row_id}">Max downloads</label>
                  <input id="youtube_max_downloads_{row_id}" type="number" min="1" step="1" name="max_downloads_{row_id}" value="{source_max_downloads_text}" placeholder="use default" />
                </div>
                <div>
                  <label for="youtube_enabled_{row_id}">Status</label>
                  <select id="youtube_enabled_{row_id}" name="enabled_{row_id}"><option value="1"{enabled_selected}>enabled</option><option value="0"{disabled_selected}>disabled</option></select>
                </div>
              </div>
              <label class="delete-check" for="youtube_delete_{row_id}"><input id="youtube_delete_{row_id}" type="checkbox" name="delete_{row_id}" value="1" /> Delete this source on save</label>
            </details>
            """
        )

    podcast_cards = []
    for item in config.get("podcasts") or []:
        row_id = int(item.get("id") or 0)
        name = html.escape(str(item.get("name") or ""))
        url = html.escape(str(item.get("url") or ""))
        subtitles_enabled = bool(item.get("subtitles", True))
        enabled = bool(item.get("enabled", True))
        status = "enabled" if enabled else "disabled"
        subtitle_offset = item.get("subtitle_offset_seconds")
        subtitle_offset_text = html.escape("" if subtitle_offset is None else str(subtitle_offset))
        source_max_downloads = item.get("max_downloads")
        source_max_downloads_text = html.escape("" if source_max_downloads is None else str(source_max_downloads))
        subtitles_yes_selected = " selected" if subtitles_enabled else ""
        subtitles_no_selected = "" if subtitles_enabled else " selected"
        enabled_selected = " selected" if enabled else ""
        disabled_selected = "" if enabled else " selected"
        podcast_cards.append(
            f"""
            <details class="source-card">
              <summary>
                <span class="source-card-title">{name or "Unnamed podcast source"}</span>
                <span class="source-card-url">{url}</span>
                <span class="row-status">{status}</span>
              </summary>
              <input type="hidden" name="source_id" value="{row_id}" />
              <div class="source-card-grid">
                <div>
                  <label for="podcast_name_{row_id}">Name</label>
                  <input id="podcast_name_{row_id}" type="text" name="name_{row_id}" value="{name}" required />
                </div>
                <div>
                  <label for="podcast_url_{row_id}">URL</label>
                  <input id="podcast_url_{row_id}" type="url" name="url_{row_id}" value="{url}" required />
                </div>
                <div>
                  <label for="podcast_subtitles_{row_id}">Subtitles</label>
                  <select id="podcast_subtitles_{row_id}" name="subtitles_{row_id}"><option value="1"{subtitles_yes_selected}>yes</option><option value="0"{subtitles_no_selected}>no</option></select>
                </div>
                <div>
                  <label for="podcast_offset_{row_id}">Subtitle offset seconds</label>
                  <input id="podcast_offset_{row_id}" type="text" name="subtitle_offset_seconds_{row_id}" value="{subtitle_offset_text}" placeholder="offset (optional)" />
                </div>
                <div>
                  <label for="podcast_max_downloads_{row_id}">Max downloads</label>
                  <input id="podcast_max_downloads_{row_id}" type="number" min="1" step="1" name="max_downloads_{row_id}" value="{source_max_downloads_text}" placeholder="use default" />
                </div>
                <div>
                  <label for="podcast_enabled_{row_id}">Status</label>
                  <select id="podcast_enabled_{row_id}" name="enabled_{row_id}"><option value="1"{enabled_selected}>enabled</option><option value="0"{disabled_selected}>disabled</option></select>
                </div>
              </div>
              <label class="delete-check" for="podcast_delete_{row_id}"><input id="podcast_delete_{row_id}" type="checkbox" name="delete_{row_id}" value="1" /> Delete this source on save</label>
            </details>
            """
        )

    youtube_sources = "".join(youtube_cards) or "<p>No YouTube sources configured.</p>"
    podcast_sources = "".join(podcast_cards) or "<p>No podcast sources configured.</p>"
    runtime_stats = _collect_runtime_stats()
    runtime_rows = []
    for label, value in runtime_stats:
        runtime_rows.append(
            f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        )
    runtime_stats_table = "".join(runtime_rows)

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GetOffline Settings</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3f6fc;
      --text: #12203a;
      --card-bg: #ffffff;
      --border: #dbe5f6;
      --border-soft: #e9eef9;
      --primary: #275df0;
      --primary-strong: #1d4fd1;
      --danger: #be123c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
      margin: 0;
      padding: 1.5rem 1rem 2rem;
      background: radial-gradient(circle at top, #f8fbff 0%, var(--bg) 55%);
      color: var(--text);
      line-height: 1.45;
    }}
    .wrap {{
      max-width: 1120px;
      margin: 0 auto;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.4rem;
      box-shadow: 0 16px 40px rgba(15, 35, 80, 0.08);
    }}
    h1, h2, h3 {{ margin-top: 0; letter-spacing: -0.01em; }}
    h1 {{ margin-bottom: 1.2rem; }}
    h2 {{ margin-bottom: .4rem; font-size: 1.24rem; }}
    h3 {{ margin: 1rem 0 .4rem; font-size: 1.03rem; }}
    label {{ display: block; margin: .75rem 0 .35rem; font-weight: 600; font-size: .92rem; }}
    input, select, textarea {{
      width: 100%;
      padding: .62rem .68rem;
      border: 1px solid #cdd9f1;
      border-radius: 10px;
      font: inherit;
      background: #fff;
    }}
    input:focus, select:focus, textarea:focus {{
      outline: none;
      border-color: #8eb0ff;
      box-shadow: 0 0 0 3px rgba(39, 93, 240, 0.16);
    }}
    textarea {{ min-height: 180px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .actions {{ margin-top: 1rem; display: flex; gap: .5rem; flex-wrap: wrap; }}
    button, a {{
      border-radius: 10px;
      border: 1px solid #c9d7f2;
      padding: .5rem .86rem;
      text-decoration: none;
      color: inherit;
      background: #fff;
      cursor: pointer;
      transition: all .14s ease-in-out;
      font-weight: 600;
    }}
    button:hover, a:hover {{ border-color: #adbfdf; transform: translateY(-1px); }}
    button.primary {{ background: var(--primary); color: #fff; border-color: var(--primary); }}
    button.primary:hover {{ background: var(--primary-strong); border-color: var(--primary-strong); }}
    button.danger {{ border-color: #f2bfca; color: var(--danger); background: #fff7f9; }}
    .section {{
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 1rem;
      margin-top: 1rem;
      background: linear-gradient(180deg, #ffffff 0%, #fcfdff 100%);
    }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .82rem; }}
    table {{ width: 100%; table-layout: fixed; border-collapse: collapse; margin-top: .75rem; border: 1px solid var(--border-soft); border-radius: 10px; overflow: hidden; }}
    thead th {{ background: #f4f7fe; font-size: .84rem; text-transform: uppercase; letter-spacing: .03em; color: #33415e; }}
    th, td {{ border-bottom: 1px solid var(--border-soft); padding: .52rem; text-align: left; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: 0; }}
    .section table input, .section table select {{ width: 100%; min-width: 0; }}
    .row-actions {{ display: flex; gap: .35rem; flex-wrap: nowrap; align-items: center; justify-content: center; }}
    .row-actions form {{ margin: 0; }}
    .compact-form {{ display: inline-block; }}
    .row-status {{ font-size: .78rem; color: #364968; text-transform: uppercase; font-weight: 700; letter-spacing: .04em; }}
    .source-table td {{ padding-top: .7rem; padding-bottom: .7rem; }}
    .source-table tr:not(.source-row-actions-row) td {{ border-bottom: 0; }}
    .source-table input, .source-table select {{ padding: .46rem .56rem; border-radius: 9px; }}
    .source-table th, .source-table td {{ overflow: hidden; }}
    .source-table td:nth-child(1), .source-table th:nth-child(1) {{ width: 18%; }}
    .source-table td:nth-child(2), .source-table th:nth-child(2) {{ width: 28%; }}
    .source-table td:nth-child(3), .source-table th:nth-child(3) {{ width: 9%; }}
    .source-table td:nth-child(4), .source-table th:nth-child(4) {{ width: 9%; }}
    .source-table td:nth-child(5), .source-table th:nth-child(5) {{ width: 16%; }}
    .source-table td:nth-child(6), .source-table th:nth-child(6) {{ width: 10%; }}
    .source-table td:last-child, .source-table th:last-child {{ width: 10%; text-align: center; }}
    .source-table td:nth-child(2) input {{ font-size: .95rem; }}
    .source-table td:nth-child(2) input, .source-table td:nth-child(5) input {{ text-overflow: ellipsis; }}
    .table-action {{ min-width: 7.25rem; padding: .35rem .72rem; font-size: .88rem; text-align: center; }}
    .table-wrap {{ overflow: visible; }}
    .source-row-actions-row td {{ border-bottom: 1px solid var(--border-soft); }}
    .source-row-actions-cell {{ padding-top: 0; padding-bottom: .8rem; }}
    .source-row-actions {{ justify-content: center; }}
    .section-help {{ margin: .15rem 0 .9rem; color: #52627d; }}
    .android-section {{ border-color: #bcd0f8; background: linear-gradient(180deg, #f8fbff 0%, #ffffff 100%); }}
    .ytdlp-section {{ border-color: #bfdbfe; background: linear-gradient(180deg, #f8fcff 0%, #ffffff 100%); }}
    .ollama-section {{ border-color: #c7d2fe; background: linear-gradient(180deg, #fbfbff 0%, #ffffff 100%); }}
    .source-list {{ display: grid; gap: .75rem; margin-top: .75rem; }}
    .source-card {{ border: 1px solid var(--border-soft); border-radius: 12px; background: #fff; overflow: hidden; }}
    .source-card[open] {{ border-color: #c9d7f2; box-shadow: 0 8px 22px rgba(15, 35, 80, 0.06); }}
    .source-card summary {{ display: grid; grid-template-columns: minmax(10rem, 1fr) minmax(12rem, 2fr) auto; gap: .75rem; align-items: center; padding: .78rem .9rem; cursor: pointer; font-weight: 700; }}
    .source-card summary::-webkit-details-marker {{ display: none; }}
    .source-card summary::before {{ content: '▸'; color: var(--primary); font-size: .9rem; transition: transform .14s ease-in-out; }}
    .source-card[open] summary::before {{ transform: rotate(90deg); }}
    .source-card summary {{ grid-template-columns: auto minmax(10rem, 1fr) minmax(12rem, 2fr) auto; }}
    .source-card-url {{ color: #52627d; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .source-card-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .82rem; padding: 0 .9rem .9rem; }}
    .delete-check {{ display: flex; align-items: center; gap: .45rem; margin: 0 .9rem .9rem; color: var(--danger); }}
    .delete-check input, label input[type="checkbox"] {{ width: auto; }}
    .source-save-actions {{ justify-content: flex-end; }}
    code {{ background: #eef3ff; border: 1px solid #d9e4fb; border-radius: 6px; padding: .1rem .3rem; }}
    @media (max-width: 900px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .wrap {{ padding: 1rem; }}
      th, td {{ padding: .45rem; }}
      .source-table, .source-table thead, .source-table tbody, .source-table tr, .source-table th, .source-table td {{ display: block; width: 100%; }}
      .source-table thead {{ display: none; }}
      .source-table tr {{ border-bottom: 1px solid var(--border-soft); padding: .4rem 0 .6rem; }}
      .source-table td {{ border: 0; padding: .35rem 0; }}
      .source-table td:last-child {{ padding-top: .2rem; }}
      .row-actions {{ gap: .4rem; justify-content: center; flex-wrap: wrap; }}
      .source-card summary, .source-card-grid {{ grid-template-columns: 1fr; }}
      .source-card summary {{ gap: .35rem; }}
      .source-card-url {{ white-space: normal; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Settings</h1>

    <div class="section">
      <h2>General</h2>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="update_defaults" />
        <label for="output_root">Output root</label>
        <input id="output_root" name="output_root" value="{output_root}" required />

        <div class="grid">
          <div>
            <label for="processing_workers">Processing workers</label>
            <input id="processing_workers" name="processing_workers" value="{processing_workers}" required />
          </div>
          <div>
            <label for="auto_update_minutes">Auto update interval (minutes)</label>
            <input id="auto_update_minutes" name="auto_update_minutes" value="{auto_update_minutes}" required />
          </div>
        </div>

        <div class="actions">
          <button type="submit" class="primary">Save general settings</button>
        </div>
      </form>
    </div>

    <div class="section ytdlp-section">
      <h2>yt-dlp configuration</h2>
      <p>Configure yt-dlp download behavior and authentication used for YouTube and media extraction.</p>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="update_ytdlp" />
        <div class="grid">
          <div>
            <label for="audio_format">Audio format</label>
            <input id="audio_format" name="audio_format" value="{audio_format}" required />
          </div>
          <div>
            <label for="audio_quality">Audio quality</label>
            <input id="audio_quality" name="audio_quality" value="{audio_quality}" required />
          </div>
          <div>
            <label for="ffmpeg_audio_filter">FFmpeg audio filter</label>
            <input id="ffmpeg_audio_filter" name="ffmpeg_audio_filter" value="{ffmpeg_audio_filter}" placeholder="loudnorm=I=-14:TP=-1.5:LRA=11" />
          </div>
          <div>
            <label for="max_downloads">Default source max downloads</label>
            <input id="max_downloads" name="max_downloads" value="{max_downloads}" required />
          </div>
          <div>
            <label for="deno_path">Deno executable</label>
            <input id="deno_path" name="deno_path" value="{deno_path}" required />
          </div>
        </div>
        <p><strong>Resolved path:</strong> Deno <code>{resolved_deno_path}</code></p>
        <label for="youtube_cookie_text">YouTube cookies.txt content</label>
        <textarea id="youtube_cookie_text" name="youtube_cookie_text" placeholder="# Netscape HTTP Cookie File">{cookie_value}</textarea>
        <div class="actions">
          <button type="submit" class="primary">Save yt-dlp configuration</button>
        </div>
      </form>
    </div>

    <div class="section ollama-section">
      <h2>Ollama configuration</h2>
      <p>Configure the local model used to generate summaries and manage stored summary text.</p>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="update_ollama" />
        <div class="grid">
          <div>
            <label for="summary_model">Ollama summary model</label>
            <input id="summary_model" name="summary_model" value="{summary_model}" required />
          </div>
          <div>
            <label for="ollama_path">Ollama executable</label>
            <input id="ollama_path" name="ollama_path" value="{ollama_path}" required />
          </div>
        </div>
        <p><strong>Resolved path:</strong> Ollama <code>{resolved_ollama_path}</code></p>
        <div class="actions">
          <button type="submit" class="primary">Save Ollama configuration</button>
        </div>
      </form>
      <form method="post" action="/settings" onsubmit="return confirm('Clear all stored summaries? They will be regenerated later.');">
        <input type="hidden" name="settings_action" value="clear_summaries" />
        <div class="actions">
          <button type="submit" class="danger">Clear summaries</button>
        </div>
      </form>
    </div>

    <div class="section android-section">
      <h2>Android push configuration</h2>
      <p>Configure Android push settings. Manual and scheduled downloads automatically try to push matching media when an authorized device is found. The app uses <code>adb push</code>.</p>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="update_android_sync" />
        <div class="grid">
          <div>
            <label for="android_sync_enabled"><input id="android_sync_enabled" type="checkbox" name="android_sync_enabled" value="1"{android_sync_enabled_checked} /> Auto-sync to Android</label>
          </div>
          <div>
            <label for="android_sync_include_subtitles"><input id="android_sync_include_subtitles" type="checkbox" name="android_sync_include_subtitles" value="1"{android_sync_include_subtitles_checked} /> Include subtitles</label>
          </div>
          <div>
            <label for="android_sync_include_unplayed"><input id="android_sync_include_unplayed" type="checkbox" name="android_sync_include_unplayed" value="1"{android_sync_include_unplayed_checked} /> Sync unplayed media</label>
          </div>
          <div>
            <label for="android_sync_include_started"><input id="android_sync_include_started" type="checkbox" name="android_sync_include_started" value="1"{android_sync_include_started_checked} /> Sync started media</label>
          </div>
          <div>
            <label for="android_sync_include_played"><input id="android_sync_include_played" type="checkbox" name="android_sync_include_played" value="1"{android_sync_include_played_checked} /> Sync played media</label>
          </div>
          <div>
            <label for="android_sync_adb_path">ADB executable</label>
            <input id="android_sync_adb_path" name="android_sync_adb_path" value="{android_sync_adb_path}" required />
          </div>
          <div>
            <label for="android_sync_connection_mode">ADB connection</label>
            <select id="android_sync_connection_mode" name="android_sync_connection_mode">
              <option value="usb"{android_sync_usb_selected}>USB / already connected device</option>
              <option value="wifi"{android_sync_wifi_selected}>Wi-Fi (connect to paired device)</option>
            </select>
          </div>
          <div>
            <label for="android_sync_wifi_address">Wi-Fi device address</label>
            <input id="android_sync_wifi_address" name="android_sync_wifi_address" value="{android_sync_wifi_address}" placeholder="192.168.1.50:5555" />
          </div>
          <div>
            <label for="android_sync_destination">Phone folder</label>
            <input id="android_sync_destination" name="android_sync_destination" value="{android_sync_destination}" required />
          </div>
          <div>
            <label for="android_sync_max_items">Max items per sync</label>
            <input id="android_sync_max_items" name="android_sync_max_items" value="{android_sync_max_items}" required />
          </div>
          <div>
            <label for="android_sync_exclude_regex">Exclude media matching regex</label>
            <input id="android_sync_exclude_regex" name="android_sync_exclude_regex" value="{android_sync_exclude_regex}" placeholder="trailer|sample" />
          </div>
        </div>
        <p><strong>Resolved path:</strong> ADB <code>{resolved_android_sync_adb_path}</code></p>
        <div class="actions">
          <button type="submit" class="primary">Save Android push configuration</button>
        </div>
      </form>
      <form method="post" action="/android-sync?next=/settings">
        <div class="actions">
          <button type="submit">Sync to Android now</button>
        </div>
      </form>
    </div>

    <div class="section">
      <h2>YouTube sources</h2>
      <p class="section-help">Open a source to edit its settings, then use the single save button at the bottom to apply all YouTube source changes.</p>
      <form method="post" action="/settings" onsubmit="return confirm('Save YouTube source changes? Checked sources will be deleted.');">
        <input type="hidden" name="settings_action" value="update_sources" />
        <input type="hidden" name="source_type" value="youtube" />
        <div class="source-list">{youtube_sources}</div>
        <div class="actions source-save-actions"><button type="submit" class="primary">Save YouTube sources</button></div>
      </form>

      <h3>Add YouTube source</h3>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="add_source" />
        <input type="hidden" name="source_type" value="youtube" />
        <div class="grid">
          <div><label>Name</label><input name="name" required /></div>
          <div><label>URL</label><input name="url" required /></div>
          <div>
            <label>Download type</label>
            <select name="media_type"><option value="audio">audio</option><option value="video">video</option></select>
          </div>
          <div>
            <label>Subtitles enabled</label>
            <select name="subtitles"><option value="1">yes</option><option value="0">no</option></select>
          </div>
          <div><label>Max downloads (optional)</label><input type="number" min="1" step="1" name="max_downloads" placeholder="use default" /></div>
        </div>
        <label>Subtitle offset seconds (optional)</label>
        <input name="subtitle_offset_seconds" />
        <div class="actions"><button type="submit" class="primary">Add YouTube source</button></div>
      </form>
    </div>

    <div class="section">
      <h2>Podcast sources</h2>
      <p class="section-help">Open a source to edit its settings, then use the single save button at the bottom to apply all podcast source changes.</p>
      <form method="post" action="/settings" onsubmit="return confirm('Save podcast source changes? Checked sources will be deleted.');">
        <input type="hidden" name="settings_action" value="update_sources" />
        <input type="hidden" name="source_type" value="podcast" />
        <div class="source-list">{podcast_sources}</div>
        <div class="actions source-save-actions"><button type="submit" class="primary">Save podcast sources</button></div>
      </form>

      <h3>Add podcast source</h3>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="add_source" />
        <input type="hidden" name="source_type" value="podcast" />
        <div class="grid">
          <div><label>Name</label><input name="name" required /></div>
          <div><label>URL</label><input name="url" required /></div>
          <div>
            <label>Subtitles enabled</label>
            <select name="subtitles"><option value="1">yes</option><option value="0">no</option></select>
          </div>
          <div><label>Max downloads (optional)</label><input type="number" min="1" step="1" name="max_downloads" placeholder="use default" /></div>
          <div><label>Subtitle offset seconds (optional)</label><input name="subtitle_offset_seconds" /></div>
        </div>
        <div class="actions"><button type="submit" class="primary">Add podcast source</button></div>
      </form>
    </div>

    <div class="section">
      <h2>Runtime stats</h2>
      <p>Live process-level diagnostics useful for performance tuning.</p>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="update_telemetry" />
        <label style="display:flex; align-items:center; gap:.5rem; font-weight:500; margin-top:.9rem;">
          <input type="checkbox" name="telemetry_dumps_enabled" value="1" style="width:auto;"{telemetry_dumps_checked} />
          Enable manual telemetry dumps (may impact performance)
        </label>
        <div class="actions">
          <button type="submit" class="primary">Save telemetry setting</button>
        </div>
      </form>
      <div class="actions">
        <form method="post" action="/settings" style="display:inline-block">
          <input type="hidden" name="settings_action" value="take_heapdump" />
          <button type="submit">Take telemetry memory dump</button>
        </form>
        <form method="post" action="/settings" style="display:inline-block">
          <input type="hidden" name="settings_action" value="take_threaddump" />
          <button type="submit">Take telemetry thread dump</button>
        </form>
      </div>
      <table>
        <tbody>{runtime_stats_table}</tbody>
      </table>
    </div>

    <div class="actions"><a href="/">Back to library</a></div>
  </div>
</body>
</html>"""


def _collect_runtime_stats() -> List[Tuple[str, str]]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    open_fd_count = -1
    open_fd_targets: List[str] = []
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            fd_entries = os.listdir(fd_dir)
            open_fd_count = len(fd_entries)
            if DEBUG_MEMORY_ENABLED:
                for fd_name in fd_entries:
                    fd_path = f"{fd_dir}/{fd_name}"
                    try:
                        open_fd_targets.append(f"{fd_name}: {os.readlink(fd_path)}")
                    except OSError:
                        open_fd_targets.append(f"{fd_name}: <unreadable>")
            break
        except OSError:
            continue

    # On Linux, ru_maxrss is reported in KB. On macOS/BSD, it is bytes.
    memory_mb = float(usage.ru_maxrss) / 1024.0
    if sys.platform == "darwin":
        memory_mb /= 1024.0

    tracked_objects_value = "disabled"
    if DEBUG_MEMORY_ENABLED:
        tracked_objects_value = f"{len(gc.get_objects()):,}"

    stats: List[Tuple[str, str]] = [
        ("Process ID", str(os.getpid())),
        ("Resident memory (ru_maxrss)", f"{memory_mb:,.2f} MB"),
        ("User CPU time", f"{usage.ru_utime:.3f} s"),
        ("System CPU time", f"{usage.ru_stime:.3f} s"),
        ("Open file descriptors", "unavailable" if open_fd_count < 0 else str(open_fd_count)),
        ("Python tracked objects", tracked_objects_value),
        ("GC generation counters", str(gc.get_count())),
        ("Active threads", str(threading.active_count())),
    ]
    if DEBUG_MEMORY_ENABLED:
        _write_runtime_diagnostics_snapshot(
            memory_mb=memory_mb,
            usage=usage,
            open_fd_count=open_fd_count,
            open_fd_targets=open_fd_targets,
        )
    return stats


def _write_runtime_diagnostics_snapshot(
    *,
    memory_mb: float,
    usage,
    open_fd_count: int,
    open_fd_targets: List[str],
) -> None:
    try:
        object_type_counts = Counter(type(obj).__name__ for obj in gc.get_objects())
        payload = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "cwd": str(Path.cwd()),
            "resident_memory_mb": round(memory_mb, 2),
            "cpu_user_seconds": round(float(usage.ru_utime), 6),
            "cpu_system_seconds": round(float(usage.ru_stime), 6),
            "open_file_descriptors": open_fd_count,
            "open_file_descriptor_targets": open_fd_targets,
            "python_tracked_objects": len(gc.get_objects()),
            "gc_generation_counters": list(gc.get_count()),
            "active_threads": threading.active_count(),
            "object_type_counts": dict(object_type_counts.most_common()),
        }
        diagnostics_path = Path.cwd() / "runtime_stats_debug.jsonl"
        with diagnostics_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("Unable to persist runtime diagnostics snapshot: %s", exc)


def _write_python_heapdump() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    heapdump_path = Path.cwd() / f"python_heapdump_{timestamp}.txt"

    if not tracemalloc.is_tracing():
        tracemalloc.start(25)

    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics("lineno")
    with heapdump_path.open("w", encoding="utf-8") as handle:
        handle.write(f"captured_at={datetime.now(timezone.utc).isoformat()}\n")
        handle.write(f"pid={os.getpid()}\n")
        handle.write(f"python_tracked_objects={len(gc.get_objects())}\n")
        handle.write(f"active_threads={threading.active_count()}\n\n")
        handle.write(f"Top {HEAPDUMP_TOP_ALLOCATIONS} allocations by lineno:\n")
        for idx, stat in enumerate(top_stats[:HEAPDUMP_TOP_ALLOCATIONS], start=1):
            handle.write(f"{idx:>4}. {stat}\n")

        total_size = sum(stat.size for stat in top_stats)
        handle.write(f"\nTotal traced allocation size: {total_size / (1024 * 1024):.2f} MiB\n")

    return heapdump_path


def _write_python_threaddump() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    threaddump_path = Path.cwd() / f"python_threaddump_{timestamp}.txt"

    with threaddump_path.open("w", encoding="utf-8") as handle:
        handle.write(f"captured_at={datetime.now(timezone.utc).isoformat()}\n")
        handle.write(f"pid={os.getpid()}\n")
        handle.write(f"active_threads={threading.active_count()}\n\n")
        for thread in threading.enumerate():
            handle.write(f"Thread name={thread.name!r} ident={thread.ident} daemon={thread.daemon} alive={thread.is_alive()}\n")

        handle.write("\n=== Stack traces ===\n")
        frames = sys._current_frames()
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            handle.write(f"\n--- Thread {thread.name!r} ident={thread.ident} ---\n")
            if frame is None:
                handle.write("No frame available.\n")
                continue
            handle.write("".join(traceback.format_stack(frame)))

    return threaddump_path


def _parse_range_header(range_header: str, file_size: int) -> Optional[Dict[str, int]]:
    if not range_header or not range_header.startswith("bytes="):
        return None

    value = range_header[len("bytes="):].strip()
    if "," in value:
        return None

    start_text, _, end_text = value.partition("-")
    if not start_text and not end_text:
        return None

    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
    else:
        suffix = int(end_text)
        if suffix <= 0:
            return None
        start = max(0, file_size - suffix)
        end = file_size - 1

    if start > end or start < 0 or end >= file_size:
        return None

    return {"start": start, "end": end}


def _write_stream_bytes(handler: BaseHTTPRequestHandler, media_path: Path, stream, total_bytes: int) -> None:
    remaining = max(0, int(total_bytes))
    while remaining > 0:
        chunk = stream.read(min(256 * 1024, remaining))
        if not chunk:
            break
        try:
            handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            _log_stream_disconnect(media_path)
            break
        remaining -= len(chunk)


def _stream_media(handler: BaseHTTPRequestHandler, media_path: Path) -> None:
    file_size = media_path.stat().st_size
    content_type = mimetypes.guess_type(str(media_path))[0] or "application/octet-stream"

    range_header = handler.headers.get("Range")
    parsed = _parse_range_header(range_header, file_size) if range_header else None

    if parsed is None:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(file_size))
        handler.send_header("Accept-Ranges", "bytes")
        handler.end_headers()

        with media_path.open("rb") as f:
            _write_stream_bytes(handler, media_path, f, file_size)
        return

    start = parsed["start"]
    end = parsed["end"]
    length = end - start + 1

    handler.send_response(206)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
    handler.send_header("Accept-Ranges", "bytes")
    handler.end_headers()

    with media_path.open("rb") as f:
        f.seek(start)
        _write_stream_bytes(handler, media_path, f, length)


def _log_stream_disconnect(media_path: Path) -> None:
    media_key = str(media_path)
    now = time.monotonic()
    with _DISCONNECT_LOG_LOCK:
        last_logged = _LAST_DISCONNECT_LOGGED_AT.get(media_key)
        if last_logged is not None and (now - last_logged) < DISCONNECT_LOG_WINDOW_SECONDS:
            return
        _LAST_DISCONNECT_LOGGED_AT[media_key] = now

    log.info("Client disconnected while streaming media: %s", media_path.name)


def make_handler(state: AppState):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            try:
                self.connection.settimeout(5.0)
            except OSError:
                pass

        def end_headers(self):
            self.send_header("Connection", "close")
            super().end_headers()

        def do_GET(self):  # noqa: N802
            self.close_connection = True
            parsed = urlparse(self.path)
            path = posixpath.normpath(parsed.path)
            query = parse_qs(parsed.query)
            rows_cache: Optional[List[MediaRow]] = None

            def _rows() -> List[MediaRow]:
                nonlocal rows_cache
                if rows_cache is None:
                    rows_cache = fetch_downloaded_media_rows(state.database_path, state.output_root)
                return rows_cache

            if path == "/":
                status = _snapshot_status(state.update_status)
                show_played = (query.get('show_played') or ['0'])[0] in {'1', 'true', 'yes', 'on'}
                favorites_only = (query.get('favorites') or ['0'])[0] in {'1', 'true', 'yes', 'on'}
                body = _render_index(
                    _rows(),
                    state.output_root,
                    state.database_path,
                    status,
                    show_played=show_played,
                    favorites_only=favorites_only,
                    android_status=_snapshot_android_sync_status(state.android_sync_status),
                )
                body_bytes = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path == "/settings":
                try:
                    stored = get_stored_config(str(state.database_path))
                except (sqlite3.OperationalError, OSError) as exc:
                    log.exception("GET /settings failed to load config db=%s: %s", state.database_path, exc)
                    self.send_error(503, "Database unavailable")
                    return
                body = _render_settings(stored)
                body_bytes = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path == "/update-status":
                status_payload = _snapshot_status(state.update_status)
                body_bytes = json.dumps(status_payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path == "/android-sync-status":
                status_payload = _snapshot_android_sync_status(state.android_sync_status)
                body_bytes = json.dumps(status_payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path == "/youtube-search":
                from youtube import search_youtube_videos

                query_text = str((query.get("q") or [""])[0]).strip()
                results = search_youtube_videos(query_text, limit=10) if query_text else []
                body_bytes = json.dumps({"results": results}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path == "/transcript-search":
                request_started_at = time.monotonic()
                try:
                    query_text = str((query.get("q") or [""])[0]).strip()
                    log.info("GET /transcript-search q=%r", query_text)
                    results: List[Dict[str, object]] = _search_transcripts_index(state.database_path, query_text, limit=50)
                    body_bytes = json.dumps({"results": results[:50]}).encode("utf-8")
                    status = 200
                except Exception as exc:
                    log.exception("Transcript search request failed: %s", exc)
                    body_bytes = json.dumps({"results": [], "error": "search_failed"}).encode("utf-8")
                    status = 200
                elapsed_ms = (time.monotonic() - request_started_at) * 1000.0
                log.info(
                    "GET /transcript-search q=%r status=%s results=%s elapsed_ms=%.1f",
                    str((query.get("q") or [""])[0]).strip(),
                    status,
                    len(json.loads(body_bytes.decode("utf-8")).get("results", [])),
                    elapsed_ms,
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path in {"/mark-played", "/mark-unplay"}:
                raw_id = (query.get("id") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    log.warning("POST /progress missing/invalid id payload=%s", body)
                    self.send_error(400, "Missing or invalid id")
                    return

                played_value = path == "/mark-played"
                _mark_download_played_from_webapp(state, int(raw_id), played=played_value)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path in {"/favorite", "/unfavorite"}:
                raw_id = (query.get("id") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return

                favorite_value = path == "/favorite"
                mark_download_favorite(str(state.database_path), int(raw_id), favorite=favorite_value)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path == "/delete-file":
                raw_id = (query.get("id") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return
                row = fetch_downloaded_media_row_by_id(state.database_path, int(raw_id))
                if row is None:
                    self.send_error(404, "Item not found")
                    return
                media_path = _resolve_safe_media_path(state.output_root, row.file_path)
                if media_path is not None and media_path.exists():
                    media_path.unlink(missing_ok=True)
                else:
                    delete_download_entry(str(state.database_path), int(raw_id))
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path == "/redownload":
                raw_id = (query.get("id") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return
                row = fetch_downloaded_media_row_by_id(state.database_path, int(raw_id))
                if row is None:
                    self.send_error(404, "Item not found")
                    return
                if row.source_type == "youtube" and row.item_url:
                    trigger_single_youtube_download(
                        state,
                        url=row.item_url,
                        media_type=_infer_media_type_for_redownload(row),
                        force_redownload=True,
                    )
                elif row.source_type == "podcast":
                    trigger_background_update(state)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path in {"/play", "/media", "/subtitle"}:
                request_started_at = time.monotonic()
                raw_id = (query.get("id") or [None])[0]
                log.info("GET %s requested id=%s", path, raw_id)
                if raw_id is None or not str(raw_id).isdigit():
                    log.warning("GET %s invalid id=%s", path, raw_id)
                    self.send_error(400, "Missing or invalid id")
                    return

                row = fetch_downloaded_media_row_by_id(state.database_path, int(raw_id))
                if row is None:
                    log.warning("GET %s missing row id=%s", path, raw_id)
                    self.send_error(404, "Item not found")
                    return

                media_path = _resolve_safe_media_path(state.output_root, row.file_path)
                if media_path is None:
                    log.warning("GET %s media unavailable id=%s path=%s", path, raw_id, row.file_path)
                    self.send_error(404, "Media file unavailable")
                    return

                if path == "/play":
                    requested_start = (query.get("t") or [None])[0]
                    resume_seconds = get_download_position_seconds(str(state.database_path), row.row_id)
                    if requested_start is not None:
                        try:
                            resume_seconds = max(0.0, float(requested_start))
                        except (TypeError, ValueError):
                            pass
                    subtitle_path = _resolve_safe_subtitle_path(state.output_root, row, media_path)
                    body = _render_player(row, media_path, resume_seconds, subtitle_path is not None)
                    body_bytes = body.encode("utf-8")
                    elapsed_ms = (time.monotonic() - request_started_at) * 1000.0
                    log.info(
                        "GET /play id=%s resume_seconds=%.3f subtitle=%s render_ms=%.1f",
                        row.row_id,
                        resume_seconds,
                        "yes" if subtitle_path is not None else "no",
                        elapsed_ms,
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                    return

                if path == "/subtitle":
                    subtitle_path = _resolve_safe_subtitle_path(state.output_root, row, media_path)
                    if subtitle_path is None:
                        self.send_error(404, "Subtitle unavailable")
                        return

                    subtitle_text = subtitle_path.read_text(encoding="utf-8", errors="replace")
                    if subtitle_path.suffix.lower() == ".srt":
                        subtitle_text = _srt_to_vtt(subtitle_text)
                    elif not subtitle_text.lstrip().startswith("WEBVTT"):
                        subtitle_text = "WEBVTT\n\n" + subtitle_text

                    body_bytes = subtitle_text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/vtt; charset=utf-8")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.wfile.write(body_bytes)
                    return

                media_started_at = time.monotonic()
                range_header = self.headers.get("Range") or "none"
                log.info("GET /media id=%s range=%s", raw_id, range_header)
                _stream_media(self, media_path)
                elapsed_ms = (time.monotonic() - media_started_at) * 1000.0
                log.info("GET /media id=%s completed stream_ms=%.1f", raw_id, elapsed_ms)
                return

            self.send_error(404, "Not found")

        def do_POST(self):  # noqa: N802
            self.close_connection = True
            parsed = urlparse(self.path)
            path = posixpath.normpath(parsed.path)
            query = parse_qs(parsed.query)

            if path == "/import-media":
                content_type = self.headers.get("Content-Type") or ""
                try:
                    content_length = int(self.headers.get("Content-Length") or 0)
                    if "multipart/form-data" in content_type:
                        body = self.rfile.read(content_length) if content_length > 0 else b""
                        file_name, payload = _extract_multipart_file(content_type, body, field_name="media_file")
                        _import_dropped_media_file(state, file_name=file_name, payload=payload)
                    else:
                        raw_name = str(self.headers.get("X-Upload-Filename") or "").strip()
                        file_name = raw_name
                        if raw_name:
                            try:
                                from urllib.parse import unquote
                                file_name = unquote(raw_name)
                            except Exception:
                                file_name = raw_name
                        _import_dropped_media_stream(state, file_name=file_name, stream=self.rfile, total_bytes=content_length)
                except ValueError as exc:
                    self.send_error(400, str(exc))
                    return
                except Exception:
                    log.exception("POST /import-media failed")
                    self.send_error(500, "Failed to import media")
                    return

                self.send_response(204)
                self.end_headers()
                return

            if path == "/update":
                started = trigger_background_update(state)
                log.info("Manual update requested (started=%s)", "yes" if started else "no")
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path == "/android-sync":
                started = trigger_android_sync(state, force=True)
                log.info("Manual Android sync requested (started=%s)", "yes" if started else "no")
                redirect_to = (query.get("next") or ["/"])[0]
                if redirect_to not in {"/", "/settings"}:
                    redirect_to = "/"
                self.send_response(303)
                self.send_header("Location", redirect_to)
                self.end_headers()
                return

            if path == "/progress":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                form = parse_qs(body)
                raw_id = (form.get("id") or [None])[0]
                raw_position = (form.get("position_seconds") or [None])[0]
                raw_reason = str((form.get("reason") or ["unknown"])[0]).strip() or "unknown"
                raw_forced = str((form.get("forced") or ["0"])[0]).strip().lower()
                is_forced = raw_forced in {"1", "true", "yes", "on"}
                if raw_id is None or not str(raw_id).isdigit():
                    log.warning("POST /progress missing/invalid id payload=%s", body)
                    self.send_error(400, "Missing or invalid id")
                    return

                try:
                    position_seconds = float(raw_position or 0.0)
                except (TypeError, ValueError):
                    log.warning("POST /progress invalid position_seconds payload=%s", body)
                    self.send_error(400, "Missing or invalid position_seconds")
                    return

                if is_forced:
                    log.info("POST /progress forced id=%s reason=%s seconds=%.3f", raw_id, raw_reason, position_seconds)

                row_id = int(raw_id)
                if _is_playback_completion_reason(raw_reason):
                    position_seconds = 0.0
                _enqueue_progress_update(state, row_id, position_seconds, reason=raw_reason, forced=is_forced)
                if _is_playback_completion_reason(raw_reason):
                    _mark_download_played_from_webapp(state, row_id, played=True)
                self.send_response(204)
                self.end_headers()
                return

            if path == "/edit-metadata":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                form = parse_qs(body)
                raw_id = (form.get("id") or [None])[0]
                title = str((form.get("title") or [""])[0]).strip()
                source_name = str((form.get("source_name") or [""])[0]).strip()
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return
                if not title or not source_name:
                    self.send_error(400, "Missing title/source_name")
                    return
                updated = _update_download_metadata(state.database_path, int(raw_id), title=title, source_name=source_name)
                if not updated:
                    self.send_error(404, "Item not found")
                    return
                self.send_response(204)
                self.end_headers()
                return

            if path == "/batch-update":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                form = parse_qs(body)
                batch_action = str((form.get("batch_action") or [""])[0]).strip().lower()
                row_ids = [int(raw_id) for raw_id in (form.get("ids") or []) if str(raw_id).isdigit()]

                if batch_action in {"played", "unplayed", "favorite", "unfavorite", "delete", "download"} and row_ids:
                    should_trigger_podcast_redownload = False
                    for row_id in row_ids:
                        if batch_action == "played":
                            _mark_download_played_from_webapp(state, row_id, played=True)
                        elif batch_action == "unplayed":
                            _mark_download_played_from_webapp(state, row_id, played=False)
                        elif batch_action == "favorite":
                            mark_download_favorite(str(state.database_path), row_id, favorite=True)
                        elif batch_action == "unfavorite":
                            mark_download_favorite(str(state.database_path), row_id, favorite=False)
                        elif batch_action == "delete":
                            row = fetch_downloaded_media_row_by_id(state.database_path, row_id)
                            if row is None:
                                continue
                            media_path = _resolve_safe_media_path(state.output_root, row.file_path)
                            if media_path is not None and media_path.exists():
                                media_path.unlink(missing_ok=True)
                            else:
                                delete_download_entry(str(state.database_path), row_id)
                        elif batch_action == "download":
                            row = fetch_downloaded_media_row_by_id(state.database_path, row_id)
                            if row is None:
                                continue
                            if row.source_type == "youtube" and row.item_url:
                                trigger_single_youtube_download(
                                    state,
                                    url=row.item_url,
                                    media_type=_infer_media_type_for_redownload(row),
                                    force_redownload=True,
                                )
                            elif row.source_type == "podcast":
                                should_trigger_podcast_redownload = True

                    if batch_action == "download" and should_trigger_podcast_redownload:
                        trigger_background_update(state)

                if _is_async_request(self):
                    self.send_response(204)
                    self.end_headers()
                    return

                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path == "/mark-all-played":
                _mark_all_downloads_played_from_webapp(state)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path == "/quick-add-youtube":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                form = parse_qs(body)
                url = str((form.get("url") or [""])[0]).strip()
                media_type = str((form.get("media_type") or ["audio"])[0]).strip().lower()
                if media_type not in {"audio", "video"}:
                    self.send_error(400, "Invalid media_type")
                    return
                if not url:
                    self.send_error(400, "Missing url")
                    return
                trigger_single_youtube_download(
                    state,
                    url=url,
                    media_type=media_type,
                    subtitles_enabled=True,
                )
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path == "/settings":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                form = parse_qs(body)
                settings_action = (form.get("settings_action") or [""])[0]

                if settings_action == "update_defaults":
                    updates = {
                        "output_root": (form.get("output_root") or [""])[0],
                        "processing_workers": (form.get("processing_workers") or [""])[0],
                        "auto_update_minutes": (form.get("auto_update_minutes") or [""])[0],
                    }
                    sanitized_updates = {
                        k: str(v).strip()
                        for k, v in updates.items()
                        if str(v).strip()
                    }
                    update_stored_defaults(str(state.database_path), sanitized_updates)
                elif settings_action == "update_ytdlp":
                    updates = {
                        "audio_format": (form.get("audio_format") or [""])[0],
                        "audio_quality": (form.get("audio_quality") or [""])[0],
                        "ffmpeg_audio_filter": (form.get("ffmpeg_audio_filter") or [""])[0],
                        "max_downloads": (form.get("max_downloads") or [""])[0],
                        "playlist_end": (form.get("max_downloads") or [""])[0],
                        "deno_path": (form.get("deno_path") or [""])[0],
                    }
                    sanitized_updates = {
                        k: str(v).strip()
                        for k, v in updates.items()
                        if str(v).strip() or k == "ffmpeg_audio_filter"
                    }
                    update_stored_defaults(str(state.database_path), sanitized_updates)
                    raw_cookie = (form.get("youtube_cookie_text") or [""])[0]
                    cookie_text = str(raw_cookie).strip()
                    update_download_settings(str(state.database_path), cookie_text or None)
                elif settings_action == "update_ollama":
                    updates = {
                        "summary_model": (form.get("summary_model") or [""])[0],
                        "ollama_path": (form.get("ollama_path") or [""])[0],
                    }
                    sanitized_updates = {
                        k: str(v).strip()
                        for k, v in updates.items()
                        if str(v).strip()
                    }
                    update_stored_defaults(str(state.database_path), sanitized_updates)
                elif settings_action == "update_android_sync":
                    updates = {
                        "android_sync_enabled": "1" if (form.get("android_sync_enabled") or ["0"])[0] in {"1", "true", "yes", "on"} else "0",
                        "android_sync_adb_path": (form.get("android_sync_adb_path") or ["adb"])[0],
                        "android_sync_connection_mode": (form.get("android_sync_connection_mode") or ["usb"])[0],
                        "android_sync_wifi_address": (form.get("android_sync_wifi_address") or [""])[0],
                        "android_sync_destination": (form.get("android_sync_destination") or ["/sdcard/Movies/GetOffline"])[0],
                        "android_sync_max_items": (form.get("android_sync_max_items") or ["10"])[0],
                        "android_sync_include_subtitles": "1" if (form.get("android_sync_include_subtitles") or ["0"])[0] in {"1", "true", "yes", "on"} else "0",
                        "android_sync_include_unplayed": "1" if (form.get("android_sync_include_unplayed") or ["0"])[0] in {"1", "true", "yes", "on"} else "0",
                        "android_sync_include_started": "1" if (form.get("android_sync_include_started") or ["0"])[0] in {"1", "true", "yes", "on"} else "0",
                        "android_sync_include_played": "1" if (form.get("android_sync_include_played") or ["0"])[0] in {"1", "true", "yes", "on"} else "0",
                        "android_sync_exclude_regex": (form.get("android_sync_exclude_regex") or [""])[0],
                    }
                    sanitized_updates = {
                        k: str(v).strip()
                        for k, v in updates.items()
                        if str(v).strip() or k in {"android_sync_exclude_regex", "android_sync_wifi_address"}
                    }
                    update_stored_defaults(str(state.database_path), sanitized_updates)
                elif settings_action == "update_telemetry":
                    telemetry_dumps_enabled = (form.get("telemetry_dumps_enabled") or ["0"])[0] in {"1", "true", "yes", "on"}
                    update_stored_defaults(
                        str(state.database_path),
                        {"telemetry_dumps_enabled": "1" if telemetry_dumps_enabled else "0"},
                    )

                elif settings_action == "update_cookie":
                    raw_cookie = (form.get("youtube_cookie_text") or [""])[0]
                    cookie_text = str(raw_cookie).strip()
                    update_download_settings(str(state.database_path), cookie_text or None)

                elif settings_action == "take_heapdump":
                    if not bool(state.config.get("defaults", {}).get("telemetry_dumps_enabled")):
                        log.warning("Telemetry memory dump request ignored because telemetry dumps are disabled.")
                    else:
                        heapdump_path = _write_python_heapdump()
                        log.warning("Captured telemetry memory dump to %s", heapdump_path)

                elif settings_action == "take_threaddump":
                    if not bool(state.config.get("defaults", {}).get("telemetry_dumps_enabled")):
                        log.warning("Telemetry thread dump request ignored because telemetry dumps are disabled.")
                    else:
                        threaddump_path = _write_python_threaddump()
                        log.warning("Captured Python threaddump to %s", threaddump_path)
                elif settings_action == "clear_summaries":
                    deleted = clear_all_summaries(str(state.database_path))

                elif settings_action == "update_sources":
                    source_type = str((form.get("source_type") or [""])[0]).strip().lower()
                    if source_type not in {"youtube", "podcast"}:
                        self.send_error(400, "Invalid source_type")
                        return
                    for source_id_raw in form.get("source_id") or []:
                        if not str(source_id_raw).isdigit():
                            self.send_error(400, "Invalid source_id")
                            return
                        source_id = int(source_id_raw)
                        if (form.get(f"delete_{source_id}") or ["0"])[0] in {"1", "true", "yes", "on"}:
                            delete_source_config(str(state.database_path), source_id)
                            continue

                        name = str((form.get(f"name_{source_id}") or [""])[0]).strip()
                        url = str((form.get(f"url_{source_id}") or [""])[0]).strip()
                        if not name or not url:
                            self.send_error(400, "Missing source name/url")
                            return
                        media_type = None
                        if source_type == "youtube":
                            media_type = str((form.get(f"media_type_{source_id}") or ["audio"])[0]).strip().lower()
                            if media_type not in {"audio", "video"}:
                                self.send_error(400, "Invalid media_type")
                                return
                        subtitles = (form.get(f"subtitles_{source_id}") or ["1"])[0] in {"1", "true", "yes", "on"}
                        raw_offset = str((form.get(f"subtitle_offset_seconds_{source_id}") or [""])[0]).strip()
                        try:
                            subtitle_offset = float(raw_offset) if raw_offset else None
                        except ValueError:
                            self.send_error(400, "Invalid subtitle_offset_seconds")
                            return
                        enabled = (form.get(f"enabled_{source_id}") or ["1"])[0] in {"1", "true", "yes", "on"}
                        raw_max_downloads = str((form.get(f"max_downloads_{source_id}") or [""])[0]).strip()
                        try:
                            source_max_downloads = int(raw_max_downloads) if raw_max_downloads else None
                        except ValueError:
                            self.send_error(400, "Invalid max_downloads")
                            return
                        if source_max_downloads is not None and source_max_downloads < 1:
                            self.send_error(400, "Invalid max_downloads")
                            return
                        update_source_config(
                            str(state.database_path),
                            row_id=source_id,
                            name=name,
                            url=url,
                            media_type=media_type,
                            subtitles=subtitles,
                            subtitle_offset_seconds=subtitle_offset,
                            max_downloads=source_max_downloads,
                        )
                        set_source_enabled(str(state.database_path), source_id, enabled)

                elif settings_action == "add_source":
                    source_type = str((form.get("source_type") or [""])[0]).strip().lower()
                    if source_type not in {"youtube", "podcast"}:
                        self.send_error(400, "Invalid source_type")
                        return
                    name = str((form.get("name") or [""])[0]).strip()
                    url = str((form.get("url") or [""])[0]).strip()
                    if not name or not url:
                        self.send_error(400, "Missing source name/url")
                        return
                    media_type = (form.get("media_type") or [None])[0] if source_type == "youtube" else None
                    subtitles = (form.get("subtitles") or ["1"])[0] in {"1", "true", "yes", "on"}
                    raw_offset = str((form.get("subtitle_offset_seconds") or [""])[0]).strip()
                    try:
                        subtitle_offset = float(raw_offset) if raw_offset else None
                    except ValueError:
                        self.send_error(400, "Invalid subtitle_offset_seconds")
                        return
                    raw_max_downloads = str((form.get("max_downloads") or [""])[0]).strip()
                    try:
                        source_max_downloads = int(raw_max_downloads) if raw_max_downloads else None
                    except ValueError:
                        self.send_error(400, "Invalid max_downloads")
                        return
                    if source_max_downloads is not None and source_max_downloads < 1:
                        self.send_error(400, "Invalid max_downloads")
                        return
                    add_source_config(
                        str(state.database_path),
                        source_type=source_type,
                        name=name,
                        url=url,
                        media_type=media_type,
                        subtitles=subtitles,
                        subtitle_offset_seconds=subtitle_offset,
                        max_downloads=source_max_downloads,
                        enabled=True,
                    )

                else:
                    source_action = (form.get("source_action") or [""])[0]
                    source_id_raw = (form.get("source_id") or [""])[0]
                    if source_action and source_id_raw.isdigit():
                        source_id = int(source_id_raw)
                        if source_action == "delete":
                            delete_source_config(str(state.database_path), source_id)
                        elif source_action == "toggle":
                            enabled = (form.get("enabled") or ["1"])[0] in {"1", "true", "yes", "on"}
                            set_source_enabled(str(state.database_path), source_id, enabled)
                        elif source_action == "edit":
                            source_type = str((form.get("source_type") or [""])[0]).strip().lower()
                            if source_type not in {"youtube", "podcast"}:
                                self.send_error(400, "Invalid source_type")
                                return
                            name = str((form.get("name") or [""])[0]).strip()
                            url = str((form.get("url") or [""])[0]).strip()
                            if not name or not url:
                                self.send_error(400, "Missing source name/url")
                                return
                            media_type = None
                            if source_type == "youtube":
                                media_type = str((form.get("media_type") or ["audio"])[0]).strip().lower()
                                if media_type not in {"audio", "video"}:
                                    self.send_error(400, "Invalid media_type")
                                    return
                            subtitles = (form.get("subtitles") or ["1"])[0] in {"1", "true", "yes", "on"}
                            raw_max_downloads = str((form.get("max_downloads") or [""])[0]).strip()
                            try:
                                source_max_downloads = int(raw_max_downloads) if raw_max_downloads else None
                            except ValueError:
                                self.send_error(400, "Invalid max_downloads")
                                return
                            if source_max_downloads is not None and source_max_downloads < 1:
                                self.send_error(400, "Invalid max_downloads")
                                return
                            raw_offset = str((form.get("subtitle_offset_seconds") or [""])[0]).strip()
                            try:
                                subtitle_offset = float(raw_offset) if raw_offset else None
                            except ValueError:
                                self.send_error(400, "Invalid subtitle_offset_seconds")
                                return
                            update_source_config(
                                str(state.database_path),
                                row_id=source_id,
                                name=name,
                                url=url,
                                media_type=media_type,
                                subtitles=subtitles,
                                subtitle_offset_seconds=subtitle_offset,
                                max_downloads=source_max_downloads,
                            )

                try:
                    stored = get_stored_config(str(state.database_path))
                except (sqlite3.OperationalError, OSError) as exc:
                    log.exception("POST /settings failed to refresh stored config db=%s: %s", state.database_path, exc)
                    self.send_error(503, "Database unavailable")
                    return
                state.config["defaults"] = stored["defaults"]
                state.config["download_settings"] = stored["download_settings"]
                state.config["youtube"] = stored["youtube"]
                state.config["podcasts"] = stored["podcasts"]
                state.output_root = Path(stored["defaults"]["output_root"])
                materialize_youtube_cookie_file(str(state.database_path))

                self.send_response(303)
                self.send_header("Location", "/settings")
                self.end_headers()
                return

            self.send_error(404, "Not found")
        def log_message(self, fmt, *args):
            _ = fmt, args

    return _Handler


def run_webapp(config: Dict, host: str = "127.0.0.1", port: int = 8080):
    defaults = config["defaults"]
    init_database(str(defaults["database_path"]))
    stored = get_stored_config(str(defaults["database_path"]))
    config["defaults"] = stored["defaults"]
    config["download_settings"] = stored["download_settings"]
    config["youtube"] = stored["youtube"]
    config["podcasts"] = stored["podcasts"]
    materialize_youtube_cookie_file(str(defaults["database_path"]))
    state = AppState(
        output_root=Path(config["defaults"]["output_root"]),
        database_path=Path(defaults["database_path"]),
        config=config,
        update_runner=_default_update_runner,
    )
    configured_defaults = config.get("defaults") or {}
    ensure_local_summary_model(
        model_name=str(configured_defaults.get("summary_model") or "qwen2.5:0.5b"),
        ollama_path=str(configured_defaults.get("ollama_path") or "ollama"),
    )
    transcript_indexing_enabled = str(os.getenv("GETOFFLINE_ENABLE_TRANSCRIPT_INDEXING", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if transcript_indexing_enabled:
        _index_transcripts_on_startup(state)
        regenerated = generate_missing_summaries(
            str(state.database_path),
            limit=500,
            model_name=str(configured_defaults.get("summary_model") or "qwen2.5:0.5b"),
            timeout_seconds=int(configured_defaults.get("summary_timeout_seconds") or 90),
        )
        log.info("Startup summary regeneration complete regenerated=%s", regenerated)
    else:
        log.info("Transcript startup indexing disabled (GETOFFLINE_ENABLE_TRANSCRIPT_INDEXING=0).")
    auto_update_stop_event = threading.Event()
    auto_update_thread = threading.Thread(
        target=_auto_update_loop,
        args=(state, auto_update_stop_event),
        daemon=True,
    )
    auto_update_thread.start()

    progress_flush_stop_event = threading.Event()
    progress_flush_thread = threading.Thread(
        target=_progress_flush_loop,
        args=(state, progress_flush_stop_event),
        daemon=True,
    )
    progress_flush_thread.start()

    android_sync_stop_event = threading.Event()
    android_sync_thread = threading.Thread(
        target=_android_sync_loop,
        args=(state, android_sync_stop_event),
        daemon=True,
    )
    android_sync_thread.start()

    def _idle_rss_loop(stop_event: threading.Event) -> None:
        while not stop_event.wait(IDLE_RSS_LOG_INTERVAL_SECONDS):
            usage = resource.getrusage(resource.RUSAGE_SELF)
            rss_mb = float(usage.ru_maxrss) / 1024.0
            if sys.platform == "darwin":
                rss_mb /= 1024.0
            if MEMORY_CEILING_MB > 0 and rss_mb > MEMORY_CEILING_MB:
                log.warning("Memory ceiling exceeded: rss=%.2f MB ceiling=%.2f MB", rss_mb, MEMORY_CEILING_MB)

    rss_stop_event = threading.Event()
    rss_thread = threading.Thread(target=_idle_rss_loop, args=(rss_stop_event,), daemon=True)
    rss_thread.start()

    descriptor_cleanup_stop_event = threading.Event()
    descriptor_cleanup_enabled = str(os.getenv("GETOFFLINE_ENABLE_DESCRIPTOR_CLEANUP", "")).strip().lower() in {"1", "true", "yes", "on"}
    if descriptor_cleanup_enabled:
        descriptor_cleanup_thread = threading.Thread(
            target=_descriptor_cleanup_loop,
            args=(state, descriptor_cleanup_stop_event),
            daemon=True,
        )
        descriptor_cleanup_thread.start()
    else:
        log.info("Descriptor cleanup loop disabled (set GETOFFLINE_ENABLE_DESCRIPTOR_CLEANUP=1 to enable).")

    server = ThreadingHTTPServer((host, int(port)), make_handler(state))
    print(f"Web app running at http://{host}:{port}")
    print("Automatic download checks are enabled. Adjust the interval in Settings (minutes).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        auto_update_stop_event.set()
        progress_flush_stop_event.set()
        android_sync_stop_event.set()
        rss_stop_event.set()
        descriptor_cleanup_stop_event.set()
        state.pending_progress_event.set()
        server.server_close()
