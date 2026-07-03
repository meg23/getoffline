import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from workers.logger import get_logger

log = get_logger("download_store")


DOWNLOAD_STATUS_DOWNLOADED = "downloaded"
DOWNLOAD_STATUS_FILTERED = "filtered"
DOWNLOAD_STATUS_MISSING = "missing"
DOWNLOAD_STATUS_RETENTION_DELETED = "retention_deleted"
PROCESSED_DOWNLOAD_STATUSES = (
    DOWNLOAD_STATUS_DOWNLOADED,
    DOWNLOAD_STATUS_FILTERED,
    DOWNLOAD_STATUS_MISSING,
    DOWNLOAD_STATUS_RETENTION_DELETED,
)


def _is_sqlite_lock_error_message(message: str) -> bool:
    text = str(message or "").lower()
    return "database is locked" in text or "database table is locked" in text


def _log_sqlite_lock(db_path: str, operation: str, error_message: str) -> None:
    log.warning(
        "SQLite lock while %s (db=%s): %s",
        operation,
        db_path,
        error_message,
    )


def _log_sqlite_lock_if_needed(db_path: str, operation: str, exc: Exception) -> None:
    if _is_sqlite_lock_error_message(str(exc)):
        _log_sqlite_lock(db_path, operation, str(exc))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_path(value: Any, *, base_dir: Optional[str] = None) -> str:
    raw = os.path.expandvars(os.path.expanduser(str(value or "").strip()))
    candidate = Path(raw)
    if not candidate.is_absolute() and base_dir:
        candidate = Path(base_dir).expanduser() / candidate
    return str(candidate.resolve())


def resolve_database_path(
    defaults: Dict[str, Any], *, base_dir: Optional[str] = None
) -> str:
    configured = defaults.get("database_path")
    if configured:
        return _resolve_path(configured, base_dir=base_dir)
    return _resolve_path(
        Path(str(defaults["output_root"]).strip()) / "downloads.sqlite3",
        base_dir=base_dir,
    )


def build_item_uid(
    *,
    item_id: Optional[str],
    item_url: Optional[str],
    media_url: Optional[str],
    title: Optional[str],
) -> str:
    for candidate in (item_id, item_url, media_url):
        if candidate:
            return str(candidate)[:255]
    title_value = title or "unknown"
    digest = hashlib.sha1(title_value.encode("utf-8")).hexdigest()
    return f"title:{digest}"


def _coerce_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


def _table_columns_sqlite(db_path: str, table_name: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }


def _ensure_schema_migrations_table(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                revision TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """)
        conn.commit()


def _is_revision_applied(db_path: str, revision: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE revision = ? LIMIT 1",
            (revision,),
        ).fetchone()
        return row is not None


def _record_revision(db_path: str, revision: str) -> None:
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (revision, applied_at) VALUES (?, ?)",
                (revision, _utcnow().isoformat()),
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "recording schema revision", exc)
        raise


def _migration_0001_create_downloads(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_url TEXT,
                item_uid TEXT NOT NULL,
                item_id TEXT,
                item_url TEXT,
                media_url TEXT,
                title TEXT,
                description TEXT,
                uploader TEXT,
                channel TEXT,
                extractor TEXT,
                playlist_id TEXT,
                playlist_title TEXT,
                upload_date TEXT,
                duration_seconds INTEGER,
                file_path TEXT,
                file_ext TEXT,
                file_size_bytes INTEGER,
                expected_bytes INTEGER,
                format_id TEXT,
                format_note TEXT,
                audio_codec TEXT,
                video_codec TEXT,
                resolution TEXT,
                fps INTEGER,
                subtitle_enabled INTEGER NOT NULL DEFAULT 1,
                subtitle_path TEXT,
                download_status TEXT NOT NULL DEFAULT 'downloaded',
                error_message TEXT,
                raw_metadata_json TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                completed_at TEXT,
                played INTEGER NOT NULL DEFAULT 0,
                favorite INTEGER NOT NULL DEFAULT 0,
                played_at TEXT,
                last_position_seconds REAL NOT NULL DEFAULT 0,
                total_listened_seconds REAL NOT NULL DEFAULT 0,
                last_position_updated_at TEXT,
                UNIQUE(source_type, source_name, item_uid)
            )
            """)
        conn.commit()


def _migration_0002_add_playback_columns(db_path: str) -> None:
    expected_columns = {
        "played": "ALTER TABLE downloads ADD COLUMN played INTEGER NOT NULL DEFAULT 0",
        "played_at": "ALTER TABLE downloads ADD COLUMN played_at TEXT",
        "last_position_seconds": "ALTER TABLE downloads ADD COLUMN last_position_seconds REAL NOT NULL DEFAULT 0",
        "total_listened_seconds": "ALTER TABLE downloads ADD COLUMN total_listened_seconds REAL NOT NULL DEFAULT 0",
        "last_position_updated_at": "ALTER TABLE downloads ADD COLUMN last_position_updated_at TEXT",
    }
    columns = _table_columns_sqlite(db_path, "downloads")
    with sqlite3.connect(db_path) as conn:
        for name, ddl in expected_columns.items():
            if name not in columns:
                conn.execute(ddl)
        conn.commit()


def _migration_0003_add_config_tables(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                youtube_cookie_text TEXT,
                cookie_updated_at TEXT,
                updated_at TEXT NOT NULL
            )
            """)
        conn.commit()


def _migration_0004_add_source_configs(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                position INTEGER NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                media_type TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                subtitles INTEGER NOT NULL DEFAULT 1,
                subtitle_offset_seconds REAL,
                updated_at TEXT NOT NULL
            )
            """)
        conn.commit()


def _migration_0005_add_source_enabled(db_path: str) -> None:
    columns = _table_columns_sqlite(db_path, "source_configs")
    if "enabled" in columns:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE source_configs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )
        conn.commit()


def _migration_0006_add_favorite_column(db_path: str) -> None:
    columns = _table_columns_sqlite(db_path, "downloads")
    if "favorite" in columns:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE downloads ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()


def _migration_0007_add_relative_media_paths(db_path: str) -> None:
    expected_columns = {
        "file_path_relative": "ALTER TABLE downloads ADD COLUMN file_path_relative TEXT",
        "subtitle_path_relative": "ALTER TABLE downloads ADD COLUMN subtitle_path_relative TEXT",
    }
    columns = _table_columns_sqlite(db_path, "downloads")
    with sqlite3.connect(db_path) as conn:
        for name, ddl in expected_columns.items():
            if name not in columns:
                conn.execute(ddl)
        conn.commit()


def _migration_0008_add_transcript_search_tables(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transcript_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                download_id INTEGER NOT NULL,
                subtitle_path TEXT NOT NULL,
                start_seconds REAL NOT NULL,
                end_seconds REAL,
                text TEXT NOT NULL,
                UNIQUE(download_id, subtitle_path, start_seconds, text)
            )
            """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_transcript_segments_download_id
            ON transcript_segments(download_id)
            """)
        conn.commit()


def _migration_0010_add_source_max_downloads(db_path: str) -> None:
    columns = _table_columns_sqlite(db_path, "source_configs")
    if "max_downloads" in columns:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE source_configs ADD COLUMN max_downloads INTEGER")
        conn.commit()


def _migration_0011_add_source_explicit_content_filter(db_path: str) -> None:
    columns = _table_columns_sqlite(db_path, "source_configs")
    if "delete_explicit_content" in columns:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE source_configs ADD COLUMN delete_explicit_content INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()


def _migration_0012_add_youtube_include_flags(db_path: str) -> None:
    columns = _table_columns_sqlite(db_path, "source_configs")
    missing = [
        column
        for column in ("include_shorts", "include_livestreams")
        if column not in columns
    ]
    if not missing:
        return
    with sqlite3.connect(db_path) as conn:
        for column in missing:
            conn.execute(
                f"ALTER TABLE source_configs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()


def _migration_0013_add_source_title_exclude_filter(db_path: str) -> None:
    columns = _table_columns_sqlite(db_path, "source_configs")
    if "title_exclude" in columns:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE source_configs ADD COLUMN title_exclude TEXT")
        conn.commit()


def _run_migration_0003(db_path: str) -> None:
    _migration_0003_add_config_tables(db_path)


def _run_migration_0004(db_path: str) -> None:
    _migration_0004_add_source_configs(db_path)


def _run_migration_0005(db_path: str) -> None:
    _migration_0005_add_source_enabled(db_path)


def _run_migration_0006(db_path: str) -> None:
    _migration_0006_add_favorite_column(db_path)


def _run_migration_0007(db_path: str) -> None:
    _migration_0007_add_relative_media_paths(db_path)


def _run_migration_0008(db_path: str) -> None:
    _migration_0008_add_transcript_search_tables(db_path)


def _run_migration_0010(db_path: str) -> None:
    _migration_0010_add_source_max_downloads(db_path)


def _run_migration_0011(db_path: str) -> None:
    _migration_0011_add_source_explicit_content_filter(db_path)


def _run_migration_0012(db_path: str) -> None:
    _migration_0012_add_youtube_include_flags(db_path)


def _run_migration_0013(db_path: str) -> None:
    _migration_0013_add_source_title_exclude_filter(db_path)


MIGRATIONS = [
    ("0001_create_downloads", _migration_0001_create_downloads),
    ("0002_add_playback_columns", _migration_0002_add_playback_columns),
    ("0003_add_config_tables", _run_migration_0003),
    ("0004_add_source_configs", _run_migration_0004),
    ("0005_add_source_enabled", _run_migration_0005),
    ("0006_add_favorite_column", _run_migration_0006),
    ("0007_add_relative_media_paths", _run_migration_0007),
    ("0008_add_transcript_search_tables", _run_migration_0008),
    ("0010_add_source_max_downloads", _run_migration_0010),
    ("0011_add_source_explicit_content_filter", _run_migration_0011),
    ("0012_add_youtube_include_flags", _run_migration_0012),
    ("0013_add_source_title_exclude_filter", _run_migration_0013),
]


DEFAULT_APP_CONFIG = {
    "output_root": "./downloads",
    "audio_format": "mp3",
    "audio_quality": "0",
    "ffmpeg_audio_filter": "loudnorm=I=-14:TP=-1.5:LRA=11",
    "max_downloads": "3",
    "playlist_end": "3",
    "processing_workers": "2",
    "auto_update_minutes": "20",
    "auto_delete_content_days": "0",
    "subtitle_transcription_mode": "in_process",
    "manual_upload_delete_explicit_content": "0",
    "telemetry_dumps_enabled": "0",
    "js_runtime_path": "qjs",
    "android_sync_enabled": "0",
    "android_sync_adb_path": "adb",
    "android_sync_connection_mode": "usb",
    "android_sync_wifi_address": "",
    "android_sync_destination": "/sdcard/Movies/GetOffline",
    "android_sync_max_items": "10",
    "android_sync_include_subtitles": "1",
    "android_sync_include_unplayed": "1",
    "android_sync_include_started": "1",
    "android_sync_include_played": "0",
    "android_sync_exclude_regex": "",
}


def apply_migrations(db_path: str) -> None:
    _ensure_schema_migrations_table(db_path)
    for revision, migrate in MIGRATIONS:
        if _is_revision_applied(db_path, revision):
            continue
        migrate(db_path)
        _record_revision(db_path, revision)


def _coerce_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def ensure_config_seeded(
    db_path: str, defaults: Optional[Dict[str, Any]] = None
) -> None:
    # Database initialization/migrations are expected to run during process startup.
    now = _utcnow().isoformat()
    seed = dict(DEFAULT_APP_CONFIG)
    if defaults:
        for key in DEFAULT_APP_CONFIG:
            if key in defaults and defaults.get(key) is not None:
                seed[key] = str(defaults[key])

    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            for key, value in seed.items():
                conn.execute(
                    """
                    INSERT INTO app_config (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (key, str(value), now),
                )

            conn.execute(
                """
                INSERT INTO download_settings (id, youtube_cookie_text, cookie_updated_at, updated_at)
                VALUES (1, NULL, NULL, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now,),
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "seeding app config", exc)
        raise


def get_stored_config(db_path: str) -> Dict[str, Any]:
    ensure_config_seeded(db_path)

    defaults = dict(DEFAULT_APP_CONFIG)
    with sqlite3.connect(db_path) as conn:
        for key, value in conn.execute("SELECT key, value FROM app_config"):
            defaults[key] = value

        row = conn.execute(
            "SELECT youtube_cookie_text FROM download_settings WHERE id = 1"
        ).fetchone()
        source_rows = conn.execute("""
            SELECT id, source_type, name, url, media_type, enabled, subtitles, subtitle_offset_seconds, max_downloads, delete_explicit_content, include_shorts, include_livestreams, title_exclude
            FROM source_configs
            ORDER BY source_type, position, id
            """).fetchall()

    youtube = []
    podcasts = []
    for (
        row_id,
        source_type,
        name,
        url,
        media_type,
        enabled,
        subtitles,
        subtitle_offset,
        source_max_downloads,
        delete_explicit_content,
        include_shorts,
        include_livestreams,
        title_exclude,
    ) in source_rows:
        payload = {
            "id": int(row_id),
            "name": name,
            "url": url,
            "enabled": bool(enabled),
            "subtitles": bool(subtitles),
            "delete_explicit_content": bool(delete_explicit_content),
            "include_shorts": bool(include_shorts),
            "include_livestreams": bool(include_livestreams),
            "title_exclude": str(title_exclude or ""),
        }
        if subtitle_offset is not None:
            payload["subtitle_offset_seconds"] = subtitle_offset
        if source_max_downloads is not None:
            payload["max_downloads"] = int(source_max_downloads)
        if source_type == "youtube":
            payload["type"] = media_type or "audio"
            youtube.append(payload)
        elif source_type == "podcast":
            podcasts.append(payload)

    return {
        "defaults": {
            "output_root": os.path.expanduser(defaults["output_root"]),
            "audio_format": defaults["audio_format"],
            "audio_quality": _coerce_int(defaults["audio_quality"], 0),
            "ffmpeg_audio_filter": str(defaults.get("ffmpeg_audio_filter") or ""),
            "max_downloads": _coerce_int(defaults["max_downloads"], 3),
            "playlist_end": _coerce_int(defaults["playlist_end"], 3),
            "processing_workers": _coerce_int(defaults.get("processing_workers"), 2),
            "auto_update_minutes": _coerce_int(defaults.get("auto_update_minutes"), 20),
            "auto_delete_content_days": max(
                0, _coerce_int(defaults.get("auto_delete_content_days"), 0)
            ),
            "js_runtime_path": str(defaults.get("js_runtime_path") or "qjs"),
            "android_sync_enabled": str(defaults.get("android_sync_enabled") or "0")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            "android_sync_adb_path": str(
                defaults.get("android_sync_adb_path") or "adb"
            ),
            "android_sync_connection_mode": str(
                defaults.get("android_sync_connection_mode") or "usb"
            ),
            "android_sync_wifi_address": str(
                defaults.get("android_sync_wifi_address") or ""
            ),
            "android_sync_destination": str(
                defaults.get("android_sync_destination") or "/sdcard/Movies/GetOffline"
            ),
            "android_sync_max_items": _coerce_int(
                defaults.get("android_sync_max_items"), 10
            ),
            "android_sync_include_subtitles": str(
                defaults.get("android_sync_include_subtitles") or "1"
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            "android_sync_include_unplayed": str(
                defaults.get("android_sync_include_unplayed") or "1"
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            "android_sync_include_started": str(
                defaults.get("android_sync_include_started") or "1"
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            "android_sync_include_played": str(
                defaults.get("android_sync_include_played") or "0"
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            "android_sync_exclude_regex": str(
                defaults.get("android_sync_exclude_regex") or ""
            ),
            "subtitle_transcription_mode": str(
                defaults.get("subtitle_transcription_mode") or "in_process"
            ),
            "manual_upload_delete_explicit_content": str(
                defaults.get("manual_upload_delete_explicit_content") or "0"
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            "telemetry_dumps_enabled": str(
                defaults.get(
                    "telemetry_dumps_enabled", defaults.get("heapdump_enabled", "0")
                )
                or "0"
            )
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            "database_path": db_path,
        },
        "download_settings": {
            "youtube_cookie_text": row[0] if row else None,
        },
        "youtube": youtube,
        "podcasts": podcasts,
    }


def update_stored_defaults(db_path: str, updates: Dict[str, Any]) -> None:
    if not updates:
        return
    ensure_config_seeded(db_path)
    now = _utcnow().isoformat()
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            for key, value in updates.items():
                if key not in DEFAULT_APP_CONFIG or value is None:
                    continue
                conn.execute(
                    """
                    INSERT INTO app_config (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                    """,
                    (key, str(value), now),
                )
            conn.commit()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "updating app defaults", exc)
        raise


def update_download_settings(db_path: str, youtube_cookie_text: Optional[str]) -> None:
    ensure_config_seeded(db_path)
    now = _utcnow().isoformat()
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO download_settings (id, youtube_cookie_text, cookie_updated_at, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    youtube_cookie_text = excluded.youtube_cookie_text,
                    cookie_updated_at = excluded.cookie_updated_at,
                    updated_at = excluded.updated_at
                """,
                (
                    youtube_cookie_text,
                    now if youtube_cookie_text else None,
                    now,
                ),
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "updating download settings", exc)
        raise


def materialize_youtube_cookie_file(
    db_path: str, cookie_path: Optional[str] = None
) -> Optional[str]:
    stored = get_stored_config(db_path)
    cookie_text = stored["download_settings"].get("youtube_cookie_text")
    if not cookie_text:
        return None

    if cookie_path:
        target_path = Path(str(cookie_path)).expanduser()
    else:
        target_path = (
            Path(tempfile.gettempdir())
            / f"getoffline-yt-dlp-cookies-{Path(db_path).name}.txt"
        )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(cookie_text, encoding="utf-8")
    return str(target_path)


def replace_sources(
    db_path: str, youtube: List[Dict[str, Any]], podcasts: List[Dict[str, Any]]
) -> None:
    ensure_config_seeded(db_path)
    now = _utcnow().isoformat()
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM source_configs")
            for idx, item in enumerate(youtube):
                conn.execute(
                    """
                    INSERT INTO source_configs (
                        source_type, position, name, url, media_type, enabled, subtitles, subtitle_offset_seconds, max_downloads, delete_explicit_content, include_shorts, include_livestreams, title_exclude, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "youtube",
                        idx,
                        str(item.get("name") or "").strip(),
                        str(item.get("url") or "").strip(),
                        str(item.get("type") or "audio").strip().lower(),
                        1 if bool(item.get("enabled", True)) else 0,
                        1 if bool(item.get("subtitles", True)) else 0,
                        item.get("subtitle_offset_seconds"),
                        item.get("max_downloads"),
                        1 if bool(item.get("delete_explicit_content", False)) else 0,
                        1 if bool(item.get("include_shorts", False)) else 0,
                        1 if bool(item.get("include_livestreams", False)) else 0,
                        str(item.get("title_exclude") or "").strip(),
                        now,
                    ),
                )

            for idx, item in enumerate(podcasts):
                conn.execute(
                    """
                    INSERT INTO source_configs (
                        source_type, position, name, url, media_type, enabled, subtitles, subtitle_offset_seconds, max_downloads, delete_explicit_content, include_shorts, include_livestreams, title_exclude, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "podcast",
                        idx,
                        str(item.get("name") or "").strip(),
                        str(item.get("url") or "").strip(),
                        None,
                        1 if bool(item.get("enabled", True)) else 0,
                        1 if bool(item.get("subtitles", True)) else 0,
                        item.get("subtitle_offset_seconds"),
                        item.get("max_downloads"),
                        1 if bool(item.get("delete_explicit_content", False)) else 0,
                        1 if bool(item.get("include_shorts", False)) else 0,
                        1 if bool(item.get("include_livestreams", False)) else 0,
                        str(item.get("title_exclude") or "").strip(),
                        now,
                    ),
                )

            conn.commit()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "replacing source configs", exc)
        raise


def seed_sources_from_config(db_path: str, config: Dict[str, Any]) -> None:
    ensure_config_seeded(db_path, config.get("defaults"))
    with sqlite3.connect(db_path) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM source_configs").fetchone()[0]
    if existing:
        return
    replace_sources(db_path, config.get("youtube", []), config.get("podcasts", []))


def add_source_config(
    db_path: str,
    *,
    source_type: str,
    name: str,
    url: str,
    media_type: Optional[str],
    subtitles: bool,
    subtitle_offset_seconds: Optional[float],
    max_downloads: Optional[int] = None,
    delete_explicit_content: bool = False,
    include_shorts: bool = False,
    include_livestreams: bool = False,
    title_exclude: str = "",
    enabled: bool = True,
) -> None:
    ensure_config_seeded(db_path)
    now = _utcnow().isoformat()
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            current_position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM source_configs WHERE source_type = ?",
                (source_type,),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO source_configs (
                    source_type, position, name, url, media_type, enabled, subtitles, subtitle_offset_seconds, max_downloads, delete_explicit_content, include_shorts, include_livestreams, title_exclude, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_type,
                    int(current_position or 0),
                    str(name or "").strip(),
                    str(url or "").strip(),
                    (
                        str(media_type).strip().lower()
                        if media_type is not None
                        else None
                    ),
                    1 if enabled else 0,
                    1 if subtitles else 0,
                    subtitle_offset_seconds,
                    max_downloads,
                    1 if delete_explicit_content else 0,
                    1 if include_shorts else 0,
                    1 if include_livestreams else 0,
                    str(title_exclude or "").strip(),
                    now,
                ),
            )
            conn.commit()
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "adding source config", exc)
        raise


def delete_source_config(db_path: str, row_id: int) -> bool:
    ensure_config_seeded(db_path)
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "DELETE FROM source_configs WHERE id = ?", (int(row_id),)
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "deleting source config", exc)
        raise


def set_source_enabled(db_path: str, row_id: int, enabled: bool) -> bool:
    ensure_config_seeded(db_path)
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "UPDATE source_configs SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, _utcnow().isoformat(), int(row_id)),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "updating source enabled state", exc)
        raise


def update_source_config(
    db_path: str,
    *,
    row_id: int,
    name: str,
    url: str,
    media_type: Optional[str],
    subtitles: bool,
    subtitle_offset_seconds: Optional[float],
    max_downloads: Optional[int] = None,
    delete_explicit_content: bool = False,
    title_exclude: str = "",
) -> bool:
    ensure_config_seeded(db_path)
    normalized_media_type = (
        str(media_type).strip().lower() if media_type is not None else None
    )
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                """
                UPDATE source_configs
                SET
                    name = ?,
                    url = ?,
                    media_type = ?,
                    subtitles = ?,
                    subtitle_offset_seconds = ?,
                    max_downloads = ?,
                    delete_explicit_content = ?,
                    title_exclude = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(name or "").strip(),
                    str(url or "").strip(),
                    normalized_media_type,
                    1 if subtitles else 0,
                    subtitle_offset_seconds,
                    max_downloads,
                    1 if delete_explicit_content else 0,
                    str(title_exclude or "").strip(),
                    _utcnow().isoformat(),
                    int(row_id),
                ),
            )
            conn.commit()
            return (cur.rowcount or 0) > 0
    except sqlite3.OperationalError as exc:
        _log_sqlite_lock_if_needed(db_path, "updating source config", exc)
        raise


def _compute_relative_storage_paths(
    payload: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    storage_root = payload.get("storage_root")
    if not storage_root:
        return None, None

    root = Path(str(storage_root)).expanduser().resolve()

    def _relativize(candidate: Any) -> Optional[str]:
        if not candidate:
            return None
        path = Path(str(candidate)).expanduser().resolve()
        try:
            return str(path.relative_to(root))
        except ValueError:
            return None

    return _relativize(payload.get("file_path")), _relativize(
        payload.get("subtitle_path")
    )


def resolve_download_artifact_path(
    output_root: str, stored_path: Optional[str], relative_path: Optional[str]
) -> Optional[str]:
    root = Path(str(output_root)).expanduser().resolve()
    if relative_path:
        return str((root / str(relative_path)).resolve())
    if stored_path:
        return str(Path(str(stored_path)).expanduser().resolve())
    return None


_DJANGO_READY = False


def _ensure_django_ready() -> None:
    global _DJANGO_READY
    if _DJANGO_READY:
        return
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
    import django
    from django.apps import apps

    if not apps.ready:
        django.setup()
    _DJANGO_READY = True


def _download_model():
    _ensure_django_ready()
    from models.models import Download

    return Download


def close_cached_descriptors() -> int:
    """Close Django database connections; kept for compatibility with callers."""
    _ensure_django_ready()
    from django.db import connections

    connections.close_all()
    return 0


def init_database(db_path: str) -> None:
    """Initialize the configured Django database.

    ``db_path`` is accepted for legacy call-site compatibility; application data now
    lives in the configured Django database rather than a per-profile SQLite file.
    """
    _ = db_path
    _ensure_django_ready()


def is_downloaded(
    db_path: str, source_type: str, source_name: str, item_uid: str
) -> bool:
    _ = db_path
    Download = _download_model()
    return Download.objects.filter(
        source_type=source_type,
        source_name=source_name,
        item_uid=item_uid,
        download_status__in=PROCESSED_DOWNLOAD_STATUSES,
    ).exists()


def has_episode_title_for_source(
    db_path: str, source_type: str, source_name: str, title: Optional[str]
) -> bool:
    _ = db_path
    normalized = str(title or "").strip().casefold()
    if not normalized:
        return False
    Download = _download_model()
    titles = Download.objects.filter(
        source_type=source_type,
        source_name=source_name,
        download_status__in=PROCESSED_DOWNLOAD_STATUSES,
        title__isnull=False,
    ).values_list("title", flat=True)
    return any(str(value or "").strip().casefold() == normalized for value in titles)


def upsert_download(db_path: str, payload: Dict[str, Any]):
    _ = db_path
    Download = _download_model()
    now = _utcnow()
    file_path_relative, subtitle_path_relative = _compute_relative_storage_paths(
        payload
    )
    defaults = {
        "source_url": payload.get("source_url"),
        "last_seen_at": now,
        "file_path_relative": file_path_relative,
        "subtitle_enabled": bool(payload.get("subtitle_enabled", True)),
        "subtitle_path_relative": subtitle_path_relative,
        "raw_metadata_json": _coerce_json(payload.get("raw_metadata")),
    }
    for key in [
        "item_id",
        "item_url",
        "media_url",
        "title",
        "description",
        "uploader",
        "channel",
        "upload_date",
        "duration_seconds",
        "file_path",
        "file_ext",
        "file_size_bytes",
        "subtitle_path",
        "download_status",
    ]:
        defaults[key] = payload.get(key)
    if defaults.get("download_status") == "downloaded":
        defaults["completed_at"] = now
    Download.objects.update_or_create(
        source_type=payload["source_type"],
        source_name=payload["source_name"],
        item_uid=payload["item_uid"],
        defaults=defaults,
    )


def mark_download_played(db_path: str, row_id: int, played: bool = True) -> bool:
    _ = db_path
    Download = _download_model()
    updated = Download.objects.filter(pk=int(row_id)).update(
        played=bool(played),
        played_at=_utcnow() if played else None,
        last_seen_at=_utcnow(),
    )
    return updated > 0


def reset_download_playback(db_path: str, row_id: int) -> bool:
    _ = db_path
    now = _utcnow()
    Download = _download_model()
    updated = Download.objects.filter(pk=int(row_id)).update(
        played=False,
        played_at=None,
        last_position_seconds=0.0,
        last_position_updated_at=now,
        last_seen_at=now,
    )
    return updated > 0


def mark_all_downloads_played(db_path: str) -> int:
    _ = db_path
    now = _utcnow()
    Download = _download_model()
    return int(
        Download.objects.filter(played=False).update(
            played=True, played_at=now, last_seen_at=now
        )
    )


def mark_download_favorite(db_path: str, row_id: int, favorite: bool = True) -> bool:
    _ = db_path
    Download = _download_model()
    return (
        Download.objects.filter(pk=int(row_id)).update(
            favorite=bool(favorite), last_seen_at=_utcnow()
        )
        > 0
    )


def delete_download_entry(db_path: str, row_id: int) -> bool:
    _ = db_path
    Download = _download_model()
    deleted, _detail = Download.objects.filter(pk=int(row_id)).delete()
    return deleted > 0


def get_download_position_seconds(db_path: str, row_id: int) -> float:
    _ = db_path
    Download = _download_model()
    value = (
        Download.objects.filter(pk=int(row_id))
        .values_list("last_position_seconds", flat=True)
        .first()
    )
    return float(value or 0.0)


def update_download_position_seconds(
    db_path: str, row_id: int, position_seconds: float
) -> bool:
    _ = db_path
    Download = _download_model()
    record = Download.objects.filter(pk=int(row_id)).first()
    if record is None:
        return False
    safe_position = max(0.0, float(position_seconds or 0.0))
    previous = max(0.0, float(record.last_position_seconds or 0.0))
    record.last_position_seconds = safe_position
    record.total_listened_seconds = max(
        0.0, float(record.total_listened_seconds or 0.0)
    ) + max(0.0, safe_position - previous)
    record.last_position_updated_at = _utcnow()
    record.last_seen_at = record.last_position_updated_at
    record.save(
        update_fields=[
            "last_position_seconds",
            "total_listened_seconds",
            "last_position_updated_at",
            "last_seen_at",
        ]
    )
    return True


def update_download_positions_batch(db_path: str, updates: Dict[int, float]) -> int:
    count = 0
    for row_id, seconds in updates.items():
        if update_download_position_seconds(db_path, row_id, seconds):
            count += 1
    return count


def get_total_listened_seconds(db_path: str) -> float:
    _ = db_path
    _ensure_django_ready()
    from django.db.models import Sum

    Download = _download_model()
    value = Download.objects.aggregate(total=Sum("total_listened_seconds"))["total"]
    return float(value or 0.0)
