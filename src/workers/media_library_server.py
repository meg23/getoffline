import hashlib
import html
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from workers.content_filter import (
    delete_media_artifacts,
    log_filtered_deletion,
    screen_transcript,
)
from workers.logger import get_logger
from workers.profiles import ProfileManager
from workers.subtitles import create_subtitles
from workers.download_store import upsert_download

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
SQLITE_PLAYBACK_TIMEOUT_SECONDS = 0.1

log = get_logger("media_import")


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
    profile_manager: Optional[ProfileManager] = None
    profile_lock: threading.RLock = field(default_factory=threading.RLock)
    profile_update_statuses: Dict[str, UpdateStatus] = field(default_factory=dict)
    profile_auth_sessions: Dict[str, Tuple[str, float]] = field(default_factory=dict)
    profile_auth_lock: threading.RLock = field(default_factory=threading.RLock)


def _normalize_stem(value: str) -> str:
    normalized = re.sub(r"\.{2,}", ".", str(value or "")).rstrip(". ")
    normalized = re.sub(
        r"^(?:manual-\d{8}-\d{6}(?:-\d+)?-)+", "", normalized, flags=re.IGNORECASE
    )
    return normalized or "item"


def _import_dropped_media_file(state: AppState, file_name: str, payload: bytes) -> None:
    if not file_name:
        raise ValueError("Missing filename")
    suffix = Path(file_name).suffix.lower()
    if suffix not in MEDIA_EXTENSIONS:
        raise ValueError("Unsupported media type")
    if not payload:
        raise ValueError("Empty file payload")

    destination_root = state.output_root.expanduser().resolve() / "manual"
    destination_root.mkdir(parents=True, exist_ok=True)
    stem = _normalize_stem(Path(file_name).stem)
    destination_path = destination_root / f"{stem}{suffix}"
    counter = 1
    while destination_path.exists():
        destination_path = destination_root / f"{stem}-{counter}{suffix}"
        counter += 1

    destination_path.write_bytes(payload)
    stat = destination_path.stat()
    checksum = hashlib.sha1(payload).hexdigest()
    now_iso = datetime.now(timezone.utc).isoformat()
    item_uid = f"manual-{checksum}-{int(stat.st_size)}"
    metadata = _manual_import_metadata(
        destination_path=destination_path,
        destination_root=destination_root,
        stem=stem,
        suffix=suffix,
        item_uid=item_uid,
        file_size=int(stat.st_size),
        sha1=checksum,
        original_filename=file_name,
        ingest_method="drag-and-drop",
        now_iso=now_iso,
    )
    _postprocess_imported_media(state, metadata=metadata, media_path=destination_path)


def _manual_import_metadata(
    *,
    destination_path: Path,
    destination_root: Path,
    stem: str,
    suffix: str,
    item_uid: str,
    file_size: int,
    sha1: str,
    original_filename: str,
    ingest_method: str,
    now_iso: str,
) -> Dict[str, object]:
    drag_drop = ingest_method == "drag-and-drop"
    return {
        "source_type": "manual",
        "source_name": "Manual Uploads",
        "source_url": None,
        "item_uid": item_uid,
        "item_id": item_uid,
        "item_url": None,
        "media_url": None,
        "title": stem,
        "description": (
            "Imported via browser drag-and-drop"
            if drag_drop
            else "Imported from local directory"
        ),
        "uploader": "local",
        "channel": "Manual Uploads",
        "extractor": "browser-drop" if drag_drop else "directory-import",
        "playlist_id": None,
        "playlist_title": None,
        "upload_date": now_iso[:10],
        "duration_seconds": None,
        "file_path": str(destination_path),
        "file_ext": suffix.lstrip("."),
        "file_size_bytes": file_size,
        "expected_bytes": file_size,
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
            "ingest_method": ingest_method,
            "original_filename": original_filename,
            "sha1": sha1,
        },
        "storage_root": str(destination_root),
    }


def _import_dropped_media_stream(
    state: AppState,
    file_name: str,
    stream,
    total_bytes: int,
    ingest_method: str = "drag-and-drop",
) -> Path:
    if not file_name:
        raise ValueError("Missing filename")
    suffix = Path(file_name).suffix.lower()
    if suffix not in MEDIA_EXTENSIONS:
        raise ValueError("Unsupported media type")
    if total_bytes <= 0:
        raise ValueError("Empty file payload")

    destination_root = state.output_root.expanduser().resolve() / "manual"
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
    metadata = _manual_import_metadata(
        destination_path=destination_path,
        destination_root=destination_root,
        stem=stem,
        suffix=suffix,
        item_uid=f"manual-{hasher.hexdigest()}-{bytes_written}",
        file_size=int(bytes_written),
        sha1=hasher.hexdigest(),
        original_filename=file_name,
        ingest_method=ingest_method,
        now_iso=now_iso,
    )
    _postprocess_imported_media(state, metadata=metadata, media_path=destination_path)
    return destination_path


def import_local_media_file(state: AppState, source_path: Path) -> Path:
    """Import one local media file through the manual-upload workflow."""
    resolved_source = Path(source_path).expanduser().resolve()
    if not resolved_source.is_file():
        raise ValueError(f"Media file does not exist: {resolved_source}")
    if resolved_source.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError(f"Unsupported media type: {resolved_source.name}")
    with resolved_source.open("rb") as stream:
        return _import_dropped_media_stream(
            state,
            file_name=resolved_source.name,
            stream=stream,
            total_bytes=resolved_source.stat().st_size,
            ingest_method="directory-import",
        )


def _manual_upload_filter_enabled(defaults: Dict) -> bool:
    value = defaults.get("manual_upload_delete_explicit_content", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _filter_imported_media(
    item_uid: str,
    media_path: Path,
    subtitle_path: Optional[Path],
    defaults: Dict,
) -> Optional[str]:
    if not _manual_upload_filter_enabled(defaults):
        log.info(
            "Manual upload profanity check skipped item_uid=%s reason=disabled",
            item_uid,
        )
        return None
    if subtitle_path is None or not Path(subtitle_path).exists():
        log.warning(
            "Manual upload profanity check cannot run item_uid=%s reason=transcript-unavailable media_path=%s",
            item_uid,
            media_path,
        )
        return "transcript-unavailable"
    started_at = time.perf_counter()
    log.info(
        "Manual upload profanity check started item_uid=%s transcript_path=%s media_path=%s",
        item_uid,
        subtitle_path,
        media_path,
    )
    try:
        explicit_match = screen_transcript(subtitle_path)
    except Exception as exc:
        elapsed_seconds = time.perf_counter() - started_at
        log.warning(
            "Manual upload profanity check failed item_uid=%s error=%s elapsed_seconds=%.3f",
            item_uid,
            exc,
            elapsed_seconds,
        )
        return "screening-failed"
    elapsed_seconds = time.perf_counter() - started_at
    if explicit_match is None:
        log.info(
            "Manual upload profanity check finished item_uid=%s result=clean elapsed_seconds=%.3f",
            item_uid,
            elapsed_seconds,
        )
        return None
    log.warning(
        "Manual upload profanity check finished item_uid=%s result=matched category=%r "
        "matched_term=%r matched_sentence=%r elapsed_seconds=%.3f",
        item_uid,
        explicit_match.category,
        explicit_match.term,
        explicit_match.sentence,
        elapsed_seconds,
    )
    deleted_paths = delete_media_artifacts(Path(media_path))
    log_filtered_deletion(
        source_type="manual",
        source_name="Manual Uploads",
        title=_normalize_stem(Path(media_path).stem),
        media_path=Path(media_path),
        match=explicit_match,
        deleted_paths=deleted_paths,
    )
    return explicit_match.category


def _download_id_for_item_uid(db_path: Path, item_uid: str) -> Optional[int]:
    with sqlite3.connect(str(db_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS) as conn:
        row = conn.execute(
            """
            SELECT id
            FROM downloads
            WHERE source_type = 'manual'
              AND source_name = 'Manual Uploads'
              AND item_uid = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (item_uid,),
        ).fetchone()
    return int(row[0]) if row else None


def _index_imported_media_transcript(
    state: AppState, item_uid: str, subtitle_path: Path, defaults: Dict
) -> None:
    download_id = _download_id_for_item_uid(state.database_path, item_uid)
    if download_id is None:
        log.warning(
            "Post-import transcript indexing skipped item_uid=%s reason=download-row-missing",
            item_uid,
        )
        return

    segments = _subtitle_segments_from_path(subtitle_path)
    if not segments:
        log.warning(
            "Post-import transcript indexing skipped item_uid=%s reason=no-segments",
            item_uid,
        )
        return

    with sqlite3.connect(
        str(state.database_path), timeout=SQLITE_PLAYBACK_TIMEOUT_SECONDS
    ) as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO transcript_segments (download_id, subtitle_path, start_seconds, end_seconds, text)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (download_id, str(subtitle_path), start, end, text)
                for start, end, text in segments
            ],
        )
        conn.commit()

    log.info(
        "Post-import transcript indexed item_uid=%s download_id=%s segments=%s",
        item_uid,
        download_id,
        len(segments),
    )


def _postprocess_imported_media(
    state: AppState, metadata: Dict, media_path: Path
) -> None:
    defaults = (state.config or {}).get("defaults") or {}
    item_uid = str(metadata["item_uid"])
    subtitle_mode = str(defaults.get("subtitle_transcription_mode") or "in_process")
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
        log.warning(
            "Post-import subtitle generation failed item_uid=%s error=%s", item_uid, exc
        )
        subtitle_path = None

    filtered_category = _filter_imported_media(
        item_uid, Path(media_path), subtitle_path, defaults
    )
    if filtered_category is not None:
        if Path(media_path).exists():
            deleted_paths = delete_media_artifacts(Path(media_path))
            log.warning(
                "Deleted manual upload before database insert because transcript screening did not pass item_uid=%s reason=%s deleted_artifacts=%s",
                item_uid,
                filtered_category,
                ", ".join(str(path) for path in deleted_paths) or "none",
            )
        log.info(
            "Manual upload not added to database after profanity check item_uid=%s status=%s",
            item_uid,
            filtered_category,
        )
        return

    if subtitle_path:
        metadata["subtitle_enabled"] = True
        metadata["subtitle_path"] = str(subtitle_path)
    upsert_download(str(state.database_path), metadata)
    log.info(
        "Manual upload added to database after profanity check item_uid=%s status=%s",
        item_uid,
        metadata["download_status"],
    )
    if subtitle_path:
        _index_imported_media_transcript(
            state, item_uid, Path(subtitle_path), defaults
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
        out_lines.append(
            f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}{tail}"
        )

    return "\n".join(out_lines).strip() + "\n"


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
        cue_text = re.sub(
            r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", " ".join(cue_lines)))
        ).strip()
        if cue_text and start is not None and end is not None:
            segments.append((start, end, cue_text))
    return segments
