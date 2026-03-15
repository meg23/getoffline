import html
import json
import mimetypes
import posixpath
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from logger import get_logger
from database import (
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
    update_download_settings,
    update_stored_defaults,
    update_download_position_seconds,
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

DEFAULT_AUTO_UPDATE_MINUTES = 20
DISCONNECT_LOG_WINDOW_SECONDS = 30.0
SQLITE_PLAYBACK_TIMEOUT_SECONDS = 0.1

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
    subtitle_path: Optional[str] = None


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
class AppState:
    output_root: Path
    database_path: Path
    config: Dict
    update_runner: Callable[[Dict, List[str]], None]
    update_status: UpdateStatus = field(default_factory=UpdateStatus)


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
    return normalized or "item"


def _is_sqlite_lock_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "database is locked" in text or "database table is locked" in text


def _log_sqlite_lock(operation: str, exc: Exception) -> None:
    if _is_sqlite_lock_error(exc):
        log.warning("SQLite lock while %s: %s", operation, exc)


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
                SELECT id, file_path
                FROM downloads
                WHERE download_status = 'downloaded' AND COALESCE(file_path, '') != ''
                """
            ).fetchall()

            updates = []
            for row_id, file_path in stale_rows:
                if _resolve_safe_media_path(root, file_path):
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
                    updates.append((repaired_path, int(row_id)))

            if updates:
                conn.executemany("UPDATE downloads SET file_path = ? WHERE id = ?", updates)
                conn.commit()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock("repairing downloaded file paths", exc)
        if not _is_sqlite_lock_error(exc):
            raise


def fetch_downloaded_media_rows(db_path: Path, output_root: Optional[Path] = None) -> List[MediaRow]:
    init_database(str(db_path))
    repair_root = output_root or db_path.parent
    _repair_downloaded_file_paths(db_path, repair_root)

    try:
        with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
            rows = conn.execute(
                """
                SELECT id, source_type, source_name, item_url, COALESCE(title, ''), COALESCE(file_path, ''),
                       file_ext, file_size_bytes, upload_date, COALESCE(played, 0), COALESCE(favorite, 0), played_at, subtitle_path
                FROM downloads
                WHERE download_status = 'downloaded'
                ORDER BY last_seen_at DESC, id DESC
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
            file_path=row[5],
            file_ext=row[6],
            file_size_bytes=row[7],
            upload_date=row[8],
            played=bool(row[9]),
            favorite=bool(row[10]),
            played_at=row[11],
            subtitle_path=row[12],
        )
        for row in rows
    ]


def fetch_downloaded_media_row_by_id(db_path: Path, row_id: int) -> Optional[MediaRow]:
    init_database(str(db_path))
    try:
        with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
            row = conn.execute(
                """
                SELECT id, source_type, source_name, item_url, COALESCE(title, ''), COALESCE(file_path, ''),
                       file_ext, file_size_bytes, upload_date, COALESCE(played, 0), COALESCE(favorite, 0), played_at, subtitle_path
                FROM downloads
                WHERE id = ? AND download_status = 'downloaded'
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
        file_path=row[5],
        file_ext=row[6],
        file_size_bytes=row[7],
        upload_date=row[8],
        played=bool(row[9]),
        favorite=bool(row[10]),
        played_at=row[11],
        subtitle_path=row[12],
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
    if row.subtitle_path:
        candidate_paths.append(Path(row.subtitle_path))
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


def trigger_single_youtube_download(state: AppState, *, url: str, media_type: str) -> bool:
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
                "subtitles": media_type == "audio",
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

        visible_rows.append(row)
        if row.played and not show_played:
            continue

        title = html.escape(row.title or path.name or "Unknown title")
        channel = html.escape(row.source_name or "?")
        source_kind = html.escape((row.source_type or "?").strip())
        size = html.escape(_human_size(row.file_size_bytes))
        raw_ext = (row.file_ext or path.suffix.lstrip(".")) or "?"
        ext = html.escape(raw_ext)
        media_kind = "video" if str(raw_ext).lower() in {"mp4", "mkv", "webm", "mov"} else "audio"
        ever_played = bool(row.played or getattr(row, "played_at", None))
        status_label = "played" if row.played else ("" if ever_played else "new")
        status_class = "status-played" if row.played else "status-new"
        status_title = "Already played" if row.played else ("Played previously" if ever_played else "Not played yet")
        if not file_exists:
            status_label = "missing"
            status_class = "status-missing"
            status_title = "File missing locally"
        mark_action = "unplay" if row.played else "played"
        mark_label = "Mark unplayed" if row.played else "Mark played"
        mark_icon = "bi-arrow-counterclockwise" if row.played else "bi-check2-circle"
        favorite_action = "unfavorite" if row_is_favorite else "favorite"
        favorite_label = "Remove favorite" if row_is_favorite else "Add favorite"
        favorite_icon = "bi-heart-fill" if row_is_favorite else "bi-heart"
        favorite_class = " icon-button-active" if row_is_favorite else ""
        play_or_download_href = f"/play?id={row.row_id}" if file_exists else f"/redownload?id={row.row_id}"
        play_or_download_label = "Play this item" if file_exists else "Redownload this item"
        play_or_download_icon = "bi-play-fill" if file_exists else "bi-download"
        resume_seconds = 0.0
        if file_exists:
            try:
                resume_seconds = get_download_position_seconds(str(database_path), row.row_id)
            except Exception:
                resume_seconds = 0.0
        delete_label = "Delete local file" if file_exists else "Delete this item from library"
        missing_action = f'<a class="icon-button" href="/delete-file?id={row.row_id}" title="{delete_label}" aria-label="{delete_label}">{_icon_use("bi-trash")}</a>'

        cards.append(
            f"""
            <tr>
                <td class="channel-col" data-label="Channel" title="{channel}">{channel}</td>
                <td class="title-cell episode-col" data-label="Episode" title="{title}">{title}</td>
                <td data-label="Source"><span class="pill status-new" title="Source: {source_kind}">{source_kind}</span></td>
                <td data-label="Type"><span class="pill">{ext}</span></td>
                <td data-label="Size">{size}</td>
                <td data-label="Status"><span class="pill {status_class}" title="{status_title}">{status_label}</span></td>
                <td class="actions" data-label="Actions">
                  <a class="icon-button" href="{play_or_download_href}" title="{play_or_download_label}" aria-label="{play_or_download_label}" data-play-link="1" data-row-id="{row.row_id}" data-title="{title}" data-source="{channel}" data-kind="{media_kind}" data-resume-seconds="{max(0.0, float(resume_seconds)):.3f}">{_icon_use(play_or_download_icon)}</a>
                  <a class="icon-button{favorite_class}" href="/{favorite_action}?id={row.row_id}" title="{favorite_label}" aria-label="{favorite_label}">{_icon_use(favorite_icon)}</a>
                  <a class="icon-button" href="/mark-{mark_action}?id={row.row_id}" title="{mark_label}" aria-label="{mark_label}">{_icon_use(mark_icon)}</a>
                  {missing_action}
                </td>
            </tr>
            """
        )

    table_rows = "\n".join(cards) if cards else "<tr><td colspan='7'>No media items found yet.</td></tr>"
    sync_running = status["is_running"] == "yes"
    button_disabled = "disabled" if sync_running else ""
    sync_icon_class = " is-spinning" if sync_running else ""
    sync_icon_href = "#bi-arrow-repeat" if sync_running else "#bi-download"
    total_items = len(visible_rows)
    played_items = sum(1 for item in visible_rows if item.played)
    favorite_items = sum(1 for item in visible_rows if bool(getattr(item, "favorite", False)))
    unplayed_items = max(total_items - played_items, 0)
    init_database(str(database_path))
    total_listened = _human_duration(get_total_listened_seconds(str(database_path)))
    toggle_show_played = not show_played
    query_bits = []
    if toggle_show_played:
        query_bits.append("show_played=1")
    if favorites_only:
        query_bits.append("favorites=1")
    toggle_href = "/" + ("?" + "&".join(query_bits) if query_bits else "")
    toggle_label = "Show played" if toggle_show_played else "Hide played"
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
    .col-channel {{ width: 14%; }}
    .col-episode {{ width: 36%; }}
    .col-source {{ width: 10%; }}
    .col-type {{ width: 8%; }}
    .col-size {{ width: 10%; }}
    .col-status {{ width: 8%; }}
    .col-actions {{ width: 14%; }}
    td[data-label="Type"], td[data-label="Size"], td[data-label="Status"],
    thead th:nth-child(4), thead th:nth-child(5), thead th:nth-child(6) {{
      text-align: left;
    }}
    td[data-label="Actions"], thead th:nth-child(7) {{
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
    .status-new {{ background: var(--new-bg); color: var(--new-text); }}
    .status-missing {{ background: #fde7e9; color: #96253b; }}
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

    .mini-player {{
      position: fixed;
      right: 1rem;
      bottom: 1rem;
      width: min(360px, calc(100vw - 2rem));
      z-index: 40;
      border: 1px solid #cbd6ee;
      border-radius: 12px;
      background: #fff;
      box-shadow: 0 12px 30px rgba(15, 34, 74, 0.2);
      padding: .7rem;
      display: none;
      gap: .55rem;
    }}
    .mini-player.is-visible {{ display: grid; }}
    .mini-player-header {{
      display: grid;
      grid-template-columns: 1fr auto auto;
      align-items: start;
      gap: .35rem .5rem;
    }}
    .mini-player-title {{ grid-column: 1; font-weight: 700; color: #17213a; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .mini-player-source {{ grid-column: 1; color: #5d6780; font-size: .85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .mini-player-open {{
      grid-row: 1;
      grid-column: 2;
      align-self: center;
      justify-self: end;
      font-size: .82rem;
      color: #2f62f2;
      text-decoration: none;
      border: 1px solid #cbd6ee;
      border-radius: 999px;
      padding: .25rem .65rem;
      background: #f4f7ff;
    }}
    .mini-player-open:hover {{ background: #e9efff; text-decoration: none; }}
    .mini-player-close {{
      grid-row: 1;
      grid-column: 3;
      justify-self: end;
      width: 1.7rem;
      height: 1.7rem;
      border-radius: 999px;
      border: 1px solid #cbd6ee;
      background: #f4f7ff;
      color: #2c3e74;
      font-size: 1.15rem;
      line-height: 1;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
    }}
    .mini-player-close:hover {{ background: #e9efff; }}
    .mini-player-media {{ width: 100%; border-radius: 10px; background: transparent; }}
    #mini-player-video {{ aspect-ratio: 16 / 9; max-height: 203px; background: #000; }}
    #mini-player-audio {{ border-radius: 999px; }}

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
      .mini-player {{ right: .5rem; left: .5rem; bottom: .5rem; width: auto; }}
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
      <div class="summary-grid">
        <div class="summary-card">
          <div class="summary-label">Visible Items</div>
          <div class="summary-value">{total_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">Played</div>
          <div class="summary-value">{played_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">New</div>
          <div class="summary-value">{unplayed_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">Favorites</div>
          <div class="summary-value">{favorite_items}</div>
        </div>
        <div class="summary-card">
          <div class="summary-label">Listened</div>
          <div class="summary-value">{total_listened}</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="toolbar toolbar-actions">
      <form id="sync-form" method="post" action="/update" class="toolbar-form">
        <button id="sync-button" class="icon-button icon-button-primary" type="submit" title="Sync downloads" aria-label="Sync downloads" {button_disabled}><svg class="bi{sync_icon_class}" aria-hidden="true" focusable="false"><use href="{sync_icon_href}"></use></svg></button>
      </form>
      <form method="post" action="/mark-all-played" class="toolbar-form">
        <button class="icon-button" type="submit" title="Mark all as played" aria-label="Mark all as played" {'disabled' if unplayed_items == 0 else ''}>{_icon_use("bi-check2-circle")}</button>
      </form>
      <button id="quick-add-open" class="icon-button" type="button" title="Add single YouTube link" aria-label="Add single YouTube link">{_icon_use("bi-plus-lg")}</button>
        <a class="icon-button" href="{toggle_href}" title="{toggle_label}" aria-label="{toggle_label}">{_icon_use(toggle_icon)}</a>
        <a class="icon-button" href="{favorites_href}" title="{favorites_label}" aria-label="{favorites_label}">{_icon_use(favorites_icon)}</a>
        <a class="icon-button" href="/settings" title="Settings" aria-label="Settings">{_icon_use("bi-gear")}</a>
      </div>
    </div>

    <div id="quick-add-backdrop" class="quick-add-backdrop" aria-hidden="true">
      <div class="quick-add-modal" role="dialog" aria-modal="true" aria-labelledby="quick-add-title">
        <h2 id="quick-add-title">Add single YouTube link</h2>
        <form method="post" action="/quick-add-youtube" class="quick-add-form">
          <div>
            <label for="quick-add-url">YouTube URL</label>
            <input id="quick-add-url" class="quick-add-input" type="url" name="url" placeholder="https://www.youtube.com/watch?v=..." required />
          </div>
          <div>
            <label for="quick-add-media-type">Download type</label>
            <select id="quick-add-media-type" class="quick-add-select" name="media_type">
              <option value="audio">audio</option>
              <option value="video">video</option>
            </select>
          </div>
          <div class="quick-add-actions">
            <button id="quick-add-cancel" type="button">Cancel</button>
            <button type="submit" class="primary">Add</button>
          </div>
        </form>
      </div>
    </div>

    <table>
      <colgroup>
        <col class="col-channel" />
        <col class="col-episode" />
        <col class="col-source" />
        <col class="col-type" />
        <col class="col-size" />
        <col class="col-status" />
        <col class="col-actions" />
      </colgroup>
      <thead><tr><th class="channel-col">Channel</th><th class="episode-col">Episode</th><th>Source</th><th>Type</th><th>Size</th><th>Status</th><th aria-label="Actions"></th></tr></thead>
      <tbody>{table_rows}</tbody>
    </table>

    <section id="mini-player" class="mini-player" aria-live="polite">
      <div class="mini-player-header">
        <div class="mini-player-title" id="mini-player-title"></div>
        <div class="mini-player-source" id="mini-player-source"></div>
        <button id="mini-player-close" class="mini-player-close" type="button" aria-label="Close mini player">&times;</button>
        <a id="mini-player-open" class="mini-player-open" href="/" aria-label="Open in dedicated player">Open</a>
      </div>
      <audio id="mini-player-audio" class="mini-player-media" controls preload="metadata"></audio>
      <video id="mini-player-video" class="mini-player-media" controls preload="metadata"></video>
    </section>
  </div>
  <script>
    (() => {{
      const syncForm = document.getElementById('sync-form');
      const syncButton = document.getElementById('sync-button');
      if (syncForm && syncButton && !syncButton.disabled) {{
        let syncRequestInFlight = false;

        syncForm.addEventListener('submit', (event) => {{
          event.preventDefault();
          if (syncRequestInFlight) return;
          syncRequestInFlight = true;

          syncButton.disabled = true;
          const icon = syncButton.querySelector('.bi');
          const iconUse = syncButton.querySelector('use');
          if (icon) icon.classList.add('is-spinning');
          if (iconUse) iconUse.setAttribute('href', '#bi-arrow-repeat');

          fetch('/update', {{ method: 'POST', keepalive: true }})
            .catch(() => {{}})
            .finally(() => {{
              window.setTimeout(() => {{
                window.location.reload();
              }}, 150);
            }});
        }});
      }}

      const miniPlayer = document.getElementById('mini-player');
      const miniTitle = document.getElementById('mini-player-title');
      const miniSource = document.getElementById('mini-player-source');
      const miniAudio = document.getElementById('mini-player-audio');
      const miniVideo = document.getElementById('mini-player-video');
      const miniOpen = document.getElementById('mini-player-open');
      const miniClose = document.getElementById('mini-player-close');

      function clearMiniMedia() {{
        [miniAudio, miniVideo].forEach((el) => {{
          if (!el) return;
          try {{ el.pause(); }} catch (_) {{}}
          el.removeAttribute('src');
          while (el.firstChild) el.removeChild(el.firstChild);
          el.load();
          el.style.display = 'none';
        }});
      }}

      function renderMiniPlayer(stateInput) {{
        if (!miniPlayer || !miniAudio || !miniVideo) return;
        let state = stateInput || null;
        if (!state) {{
          const raw = localStorage.getItem('getofflineMiniPlayerState');
          if (!raw) return;
          try {{ state = JSON.parse(raw); }} catch (_) {{ return; }}
        }}
        if (!state || !state.rowId || !state.src || !state.kind) return;
        if (miniOpen) miniOpen.href = state.playUrl || ('/play?id=' + state.rowId);

        clearMiniMedia();
        if (miniTitle) miniTitle.textContent = state.title || 'Now playing';
        if (miniSource) miniSource.textContent = state.source || '';

        const active = state.kind === 'video' ? miniVideo : miniAudio;
        active.style.display = 'block';
        const source = document.createElement('source');
        source.src = state.src;
        active.appendChild(source);

        active.addEventListener('loadedmetadata', () => {{
          active.currentTime = Math.max(0, Number(state.currentTime || 0));
          if (!state.paused) active.play().catch(() => {{}});
        }}, {{ once: true }});
        active.load();

        const persist = () => {{
          localStorage.setItem('getofflineMiniPlayerState', JSON.stringify({{
            ...state,
            currentTime: active.currentTime || 0,
            paused: active.paused,
          }}));
        }};
        active.addEventListener('timeupdate', persist);
        active.addEventListener('pause', persist);
        active.addEventListener('play', persist);
        active.addEventListener('ended', () => {{
          closeMiniPlayer();
        }});

        miniPlayer.classList.add('is-visible');
      }}

      function closeMiniPlayer() {{
        localStorage.removeItem('getofflineMiniPlayerState');
        clearMiniMedia();
        if (miniPlayer) miniPlayer.classList.remove('is-visible');
      }}

      document.querySelectorAll('a[data-play-link="1"]').forEach((link) => {{
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
            src: '/media?id=' + rowId,
            playUrl: '/play?id=' + rowId,
            currentTime: resumeSeconds,
            paused: false,
          }};
          localStorage.setItem('getofflineMiniPlayerState', JSON.stringify(state));
          renderMiniPlayer(state);
        }});
      }});

      if (miniOpen) {{
        miniOpen.addEventListener('click', (event) => {{
          event.preventDefault();
          const fallbackUrl = miniOpen.href || '/';
          const raw = localStorage.getItem('getofflineMiniPlayerState');
          if (!raw) {{
            window.location.assign(fallbackUrl);
            return;
          }}
          let state = null;
          try {{ state = JSON.parse(raw); }} catch (_) {{
            window.location.assign(fallbackUrl);
            return;
          }}
          if (!state || !state.rowId) {{
            window.location.assign(fallbackUrl);
            return;
          }}

          const active = state.kind === 'video' ? miniVideo : miniAudio;
          if (active && active.style.display !== 'none') {{
            state.currentTime = active.currentTime || 0;
            state.paused = active.paused;
          }}
          state.playUrl = '/play?id=' + state.rowId;
          localStorage.setItem('getofflineMiniPlayerState', JSON.stringify(state));
          const targetUrl = state.playUrl + (state.paused ? '' : '&autoplay=1');
          window.location.assign(targetUrl);
        }});
      }}

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
      if (!openBtn || !backdrop) return;

      const closeModal = () => {{
        backdrop.classList.remove('is-open');
        backdrop.setAttribute('aria-hidden', 'true');
      }};

      openBtn.addEventListener('click', () => {{
        backdrop.classList.add('is-open');
        backdrop.setAttribute('aria-hidden', 'false');
        if (urlInput) urlInput.focus();
      }});

      if (cancelBtn) cancelBtn.addEventListener('click', closeModal);
      backdrop.addEventListener('click', (event) => {{
        if (event.target === backdrop) closeModal();
      }});
      document.addEventListener('keydown', (event) => {{
        if (event.key === 'Escape' && backdrop.classList.contains('is-open')) closeModal();
      }});
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
      const resumeLabel = document.getElementById('resume-label');
      const transcript = document.getElementById('transcript');
      const subtitleTrackEl = document.getElementById('subtitle-track');
      let lastSentSeconds = -9999;
      let progressInFlight = false;
      let queuedProgressSeconds = null;
      let progressController = null;
      let hasAppliedInitialSeek = false;
      let lastActiveCue = null;
      let transcriptReady = false;

      if (!player) return;

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

      function sendProgressRequest(seconds, keepalive) {{
        const body = new URLSearchParams();
        body.set('id', String(rowId));
        body.set('position_seconds', seconds.toFixed(3));

        const options = {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
          body: body.toString(),
        }};

        if (keepalive) {{
          options.keepalive = true;
        }} else {{
          progressController = new AbortController();
          options.signal = progressController.signal;
        }}

        return fetch('/progress', options).catch(() => {{}});
      }}

      function abortPendingProgressRequest() {{
        if (!progressController) return;
        progressController.abort();
        progressController = null;
      }}

      function postProgress(seconds, force) {{
        const safe = Math.max(0, Number(seconds || 0));
        if (!force && Math.abs(safe - lastSentSeconds) < 1.0) return;
        updateLabel(safe);
        lastSentSeconds = safe;

        if (force && navigator.sendBeacon) {{
          abortPendingProgressRequest();
          const beaconBody = new URLSearchParams();
          beaconBody.set('id', String(rowId));
          beaconBody.set('position_seconds', safe.toFixed(3));
          const blob = new Blob([beaconBody.toString()], {{ type: 'application/x-www-form-urlencoded' }});
          if (navigator.sendBeacon('/progress', blob)) return;
        }}

        if (force) {{
          sendProgressRequest(safe, true);
          return;
        }}

        if (progressInFlight) {{
          queuedProgressSeconds = safe;
          return;
        }}

        progressInFlight = true;
        queuedProgressSeconds = null;
        sendProgressRequest(safe, false).finally(() => {{
          progressInFlight = false;
          progressController = null;
          if (queuedProgressSeconds === null) return;
          const queued = queuedProgressSeconds;
          queuedProgressSeconds = null;
          postProgress(queued, false);
        }});
      }}

      function persistMiniPlayerState() {{
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
      player.addEventListener('canplay', () => {{
        if (shouldAutoPlay) player.play().catch(() => {{}});
      }});
      player.addEventListener('playing', applyInitialSeek);
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
        postProgress(player.currentTime, true);
      }});
      player.addEventListener('play', persistMiniPlayerState);
      player.addEventListener('ended', () => {{
        localStorage.removeItem('getofflineMiniPlayerState');
        postProgress(0, true);
      }});

      if (backToLibrary) {{
        backToLibrary.addEventListener('click', () => {{
          persistMiniPlayerState();
          postProgress(player.currentTime, true);
        }});
      }}

      document.addEventListener('visibilitychange', () => {{
        if (document.hidden) postProgress(player.currentTime, true);
      }});
      window.addEventListener('beforeunload', () => postProgress(player.currentTime, true));
      window.addEventListener('pagehide', () => {{
        abortPendingProgressRequest();
        postProgress(player.currentTime, true);
      }});
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
    max_downloads = html.escape(str(defaults.get("max_downloads") or "3"))
    playlist_end = html.escape(str(defaults.get("playlist_end") or "3"))
    processing_workers = html.escape(str(defaults.get("processing_workers") or "2"))
    auto_update_minutes = html.escape(str(defaults.get("auto_update_minutes") or str(DEFAULT_AUTO_UPDATE_MINUTES)))
    cookie_value = html.escape(cookie_text)

    youtube_rows = []
    for item in config.get("youtube") or []:
        row_id = int(item.get("id") or 0)
        name = html.escape(str(item.get("name") or ""))
        url = html.escape(str(item.get("url") or ""))
        media_type = html.escape(str(item.get("type") or "audio"))
        subtitles = "yes" if item.get("subtitles", True) else "no"
        enabled = bool(item.get("enabled", True))
        status = "enabled" if enabled else "disabled"
        toggle_to = "0" if enabled else "1"
        toggle_label = "Disable" if enabled else "Enable"
        youtube_rows.append(
            f"""
            <tr>
              <td>{name}</td>
              <td><a href="{url}" target="_blank" rel="noreferrer">{url}</a></td>
              <td>{media_type}</td>
              <td>{subtitles}</td>
              <td>{status}</td>
              <td class="row-actions">                <form method="post" action="/settings">                  <input type="hidden" name="source_action" value="toggle" />                  <input type="hidden" name="source_id" value="{row_id}" />                  <input type="hidden" name="enabled" value="{toggle_to}" />                  <button type="submit">{toggle_label}</button>                </form>                <form method="post" action="/settings" onsubmit="return confirm('Delete this source?');">                  <input type="hidden" name="source_action" value="delete" />                  <input type="hidden" name="source_id" value="{row_id}" />                  <button type="submit" class="danger">Delete</button>                </form>
              </td>
            </tr>
            """
        )

    podcast_rows = []
    for item in config.get("podcasts") or []:
        row_id = int(item.get("id") or 0)
        name = html.escape(str(item.get("name") or ""))
        url = html.escape(str(item.get("url") or ""))
        subtitles = "yes" if item.get("subtitles", True) else "no"
        enabled = bool(item.get("enabled", True))
        status = "enabled" if enabled else "disabled"
        toggle_to = "0" if enabled else "1"
        toggle_label = "Disable" if enabled else "Enable"
        podcast_rows.append(
            f"""
            <tr>
              <td>{name}</td>
              <td><a href="{url}" target="_blank" rel="noreferrer">{url}</a></td>
              <td>{subtitles}</td>
              <td>{status}</td>
              <td class="row-actions">                <form method="post" action="/settings">                  <input type="hidden" name="source_action" value="toggle" />                  <input type="hidden" name="source_id" value="{row_id}" />                  <input type="hidden" name="enabled" value="{toggle_to}" />                  <button type="submit">{toggle_label}</button>                </form>                <form method="post" action="/settings" onsubmit="return confirm('Delete this source?');">                  <input type="hidden" name="source_action" value="delete" />                  <input type="hidden" name="source_id" value="{row_id}" />                  <button type="submit" class="danger">Delete</button>                </form>
              </td>
            </tr>
            """
        )

    youtube_table = "".join(youtube_rows) or "<tr><td colspan='6'>No YouTube sources configured.</td></tr>"
    podcast_table = "".join(podcast_rows) or "<tr><td colspan='5'>No podcast sources configured.</td></tr>"

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GetOffline Settings</title>
  <style>
    body {{ font-family: Inter, Segoe UI, Roboto, Arial, sans-serif; margin: 0; padding: 1rem; background: #f5f7fb; color: #17213a; }}
    .wrap {{ max-width: 1100px; margin: 0 auto; background: #fff; border: 1px solid #dbe3f3; border-radius: 12px; padding: 1rem; }}
    h1, h2, h3 {{ margin-top: 0; }}
    label {{ display: block; margin: .7rem 0 .2rem; font-weight: 600; }}
    input, select, textarea {{ width: 100%; padding: .55rem; border: 1px solid #cbd6ee; border-radius: 8px; font: inherit; }}
    textarea {{ min-height: 180px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .actions {{ margin-top: 1rem; display: flex; gap: .5rem; }}
    button, a {{ border-radius: 8px; border: 1px solid #cbd6ee; padding: .45rem .8rem; text-decoration: none; color: inherit; background: #fff; cursor: pointer; }}
    button.primary {{ background: #2f62f2; color: #fff; border-color: #2f62f2; }}
    button.danger {{ border-color: #d66; color: #a22; }}
    .section {{ border: 1px solid #e2e8f8; border-radius: 10px; padding: .9rem; margin-top: 1rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .8rem; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: .6rem; }}
    th, td {{ border-bottom: 1px solid #e9eef9; padding: .45rem; text-align: left; vertical-align: top; }}
    .row-actions {{ display: flex; gap: .35rem; flex-wrap: wrap; }}
    .row-actions form {{ margin: 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Settings</h1>

    <div class="section">
      <h2>Defaults</h2>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="update_defaults" />
        <label for="output_root">Output root</label>
        <input id="output_root" name="output_root" value="{output_root}" required />

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
            <label for="max_downloads">Max downloads</label>
            <input id="max_downloads" name="max_downloads" value="{max_downloads}" required />
          </div>
          <div>
            <label for="playlist_end">Playlist end</label>
            <input id="playlist_end" name="playlist_end" value="{playlist_end}" required />
          </div>
        </div>

        <label for="processing_workers">Processing workers</label>
        <input id="processing_workers" name="processing_workers" value="{processing_workers}" required />

        <label for="auto_update_minutes">Auto update interval (minutes)</label>
        <input id="auto_update_minutes" name="auto_update_minutes" value="{auto_update_minutes}" required />

        <div class="actions">
          <button type="submit" class="primary">Save defaults</button>
        </div>
      </form>
    </div>

    <div class="section">
      <h2>YouTube cookie text</h2>
      <form method="post" action="/settings">
        <input type="hidden" name="settings_action" value="update_cookie" />
        <label for="youtube_cookie_text">cookies.txt content</label>
        <textarea id="youtube_cookie_text" name="youtube_cookie_text" placeholder="# Netscape HTTP Cookie File">{cookie_value}</textarea>
        <div class="actions">
          <button type="submit" class="primary">Save cookie</button>
        </div>
      </form>
    </div>

    <div class="section">
      <h2>YouTube sources</h2>
    <table>
        <thead><tr><th>Name</th><th>URL</th><th>Type</th><th>Subtitles</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>{youtube_table}</tbody>
      </table>

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
        </div>
        <label>Subtitle offset seconds (optional)</label>
        <input name="subtitle_offset_seconds" />
        <div class="actions"><button type="submit" class="primary">Add YouTube source</button></div>
      </form>
    </div>

    <div class="section">
      <h2>Podcast sources</h2>
    <table>
        <thead><tr><th>Name</th><th>URL</th><th>Subtitles</th><th>Status</th><th>Actions</th></tr></thead>
        <tbody>{podcast_table}</tbody>
      </table>

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
          <div><label>Subtitle offset seconds (optional)</label><input name="subtitle_offset_seconds" /></div>
        </div>
        <div class="actions"><button type="submit" class="primary">Add podcast source</button></div>
      </form>
    </div>

    <div class="actions"><a href="/">Back to library</a></div>
  </div>
</body>
</html>"""


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
        def setup(self):
            super().setup()
            try:
                self.connection.settimeout(5.0)
            except OSError:
                pass

        def do_GET(self):  # noqa: N802
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
                body = _render_index(_rows(), state.output_root, state.database_path, status, show_played=show_played, favorites_only=favorites_only)
                body_bytes = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path == "/settings":
                stored = get_stored_config(str(state.database_path))
                body = _render_settings(stored)
                body_bytes = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body_bytes)))
                self.end_headers()
                self.wfile.write(body_bytes)
                return

            if path in {"/mark-played", "/mark-unplay"}:
                raw_id = (query.get("id") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return

                played_value = path == "/mark-played"
                mark_download_played(str(state.database_path), int(raw_id), played=played_value)
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
                        media_type="video" if (row.file_ext or "").lower() in {"mp4", "mkv", "webm", "mov"} else "audio",
                    )
                elif row.source_type == "podcast":
                    trigger_background_update(state)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path in {"/play", "/media", "/subtitle"}:
                raw_id = (query.get("id") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return

                row = fetch_downloaded_media_row_by_id(state.database_path, int(raw_id))
                if row is None:
                    self.send_error(404, "Item not found")
                    return

                media_path = _resolve_safe_media_path(state.output_root, row.file_path)
                if media_path is None:
                    self.send_error(404, "Media file unavailable")
                    return

                if path == "/play":
                    resume_seconds = get_download_position_seconds(str(state.database_path), row.row_id)
                    subtitle_path = _resolve_safe_subtitle_path(state.output_root, row, media_path)
                    body = _render_player(row, media_path, resume_seconds, subtitle_path is not None)
                    body_bytes = body.encode("utf-8")
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

                _stream_media(self, media_path)
                return

            self.send_error(404, "Not found")

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = posixpath.normpath(parsed.path)

            if path == "/update":
                trigger_background_update(state)
                self.send_response(303)
                self.send_header("Location", "/")
                self.end_headers()
                return

            if path == "/progress":
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                form = parse_qs(body)
                raw_id = (form.get("id") or [None])[0]
                raw_position = (form.get("position_seconds") or [None])[0]
                if raw_id is None or not str(raw_id).isdigit():
                    self.send_error(400, "Missing or invalid id")
                    return

                try:
                    position_seconds = float(raw_position or 0.0)
                except (TypeError, ValueError):
                    self.send_error(400, "Missing or invalid position_seconds")
                    return

                update_download_position_seconds(str(state.database_path), int(raw_id), position_seconds)
                self.send_response(204)
                self.end_headers()
                return

            if path == "/mark-all-played":
                mark_all_downloads_played(str(state.database_path))
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
                        "audio_format": (form.get("audio_format") or [""])[0],
                        "audio_quality": (form.get("audio_quality") or [""])[0],
                        "max_downloads": (form.get("max_downloads") or [""])[0],
                        "playlist_end": (form.get("playlist_end") or [""])[0],
                        "processing_workers": (form.get("processing_workers") or [""])[0],
                        "auto_update_minutes": (form.get("auto_update_minutes") or [""])[0],
                    }
                    sanitized_updates = {k: str(v).strip() for k, v in updates.items() if str(v).strip()}
                    update_stored_defaults(str(state.database_path), sanitized_updates)

                elif settings_action == "update_cookie":
                    raw_cookie = (form.get("youtube_cookie_text") or [""])[0]
                    cookie_text = str(raw_cookie).strip()
                    update_download_settings(str(state.database_path), cookie_text or None)

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
                    add_source_config(
                        str(state.database_path),
                        source_type=source_type,
                        name=name,
                        url=url,
                        media_type=media_type,
                        subtitles=subtitles,
                        subtitle_offset_seconds=subtitle_offset,
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

                stored = get_stored_config(str(state.database_path))
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
    auto_update_stop_event = threading.Event()
    auto_update_thread = threading.Thread(
        target=_auto_update_loop,
        args=(state, auto_update_stop_event),
        daemon=True,
    )
    auto_update_thread.start()
    server = ThreadingHTTPServer((host, int(port)), make_handler(state))
    print(f"Web app running at http://{host}:{port}")
    print("Automatic download checks are enabled. Adjust the interval in Settings (minutes).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        auto_update_stop_event.set()
        server.server_close()
