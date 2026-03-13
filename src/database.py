import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from sqlalchemy import (
        Boolean,
        DateTime,
        Float,
        Integer,
        String,
        Text,
        UniqueConstraint,
        create_engine,
        select,
    )
    from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

    HAS_SQLALCHEMY = True
except ModuleNotFoundError:  # pragma: no cover
    HAS_SQLALCHEMY = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_database_path(defaults: Dict[str, Any]) -> str:
    configured = defaults.get("database_path")
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(os.path.expanduser(defaults["output_root"]), "downloads.sqlite3")


def build_item_uid(*, item_id: Optional[str], item_url: Optional[str], media_url: Optional[str], title: Optional[str]) -> str:
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
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_schema_migrations_table(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                revision TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _is_revision_applied(db_path: str, revision: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE revision = ? LIMIT 1",
            (revision,),
        ).fetchone()
        return row is not None


def _record_revision(db_path: str, revision: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO schema_migrations (revision, applied_at) VALUES (?, ?)",
            (revision, _utcnow().isoformat()),
        )
        conn.commit()


def _migration_0001_create_downloads(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
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
            """
        )
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


def _migration_0006_add_favorite_column(db_path: str) -> None:
    columns = _table_columns_sqlite(db_path, "downloads")
    if "favorite" in columns:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE downloads ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
        conn.commit()


MIGRATIONS = [
    ("0001_create_downloads", _migration_0001_create_downloads),
    ("0002_add_playback_columns", _migration_0002_add_playback_columns),
    (
        "0003_add_config_tables",
        lambda db_path: _migration_0003_add_config_tables(db_path),
    ),
    (
        "0004_add_source_configs",
        lambda db_path: _migration_0004_add_source_configs(db_path),
    ),
    (
        "0005_add_source_enabled",
        lambda db_path: _migration_0005_add_source_enabled(db_path),
    ),
    (
        "0006_add_favorite_column",
        lambda db_path: _migration_0006_add_favorite_column(db_path),
    ),
]


DEFAULT_APP_CONFIG = {
    "output_root": "./downloads",
    "audio_format": "mp3",
    "audio_quality": "0",
    "max_downloads": "3",
    "playlist_end": "3",
    "processing_workers": "2",
}


def _migration_0003_add_config_tables(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS download_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                youtube_cookie_text TEXT,
                cookie_updated_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _migration_0004_add_source_configs(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
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
            """
        )
        conn.commit()


def _migration_0005_add_source_enabled(db_path: str) -> None:
    columns = _table_columns_sqlite(db_path, "source_configs")
    if "enabled" in columns:
        return
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE source_configs ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        conn.commit()


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


def ensure_config_seeded(db_path: str, defaults: Optional[Dict[str, Any]] = None) -> None:
    init_database(db_path)
    now = _utcnow().isoformat()
    seed = dict(DEFAULT_APP_CONFIG)
    if defaults:
        for key in DEFAULT_APP_CONFIG:
            if key in defaults and defaults.get(key) is not None:
                seed[key] = str(defaults[key])

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


def get_stored_config(db_path: str) -> Dict[str, Any]:
    ensure_config_seeded(db_path)

    defaults = dict(DEFAULT_APP_CONFIG)
    with sqlite3.connect(db_path) as conn:
        for key, value in conn.execute("SELECT key, value FROM app_config"):
            defaults[key] = value

        row = conn.execute("SELECT youtube_cookie_text FROM download_settings WHERE id = 1").fetchone()
        source_rows = conn.execute(
            """
            SELECT id, source_type, name, url, media_type, enabled, subtitles, subtitle_offset_seconds
            FROM source_configs
            ORDER BY source_type, position, id
            """
        ).fetchall()

    youtube = []
    podcasts = []
    for row_id, source_type, name, url, media_type, enabled, subtitles, subtitle_offset in source_rows:
        payload = {
            "id": int(row_id),
            "name": name,
            "url": url,
            "enabled": bool(enabled),
            "subtitles": bool(subtitles),
        }
        if subtitle_offset is not None:
            payload["subtitle_offset_seconds"] = subtitle_offset
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
            "max_downloads": _coerce_int(defaults["max_downloads"], 3),
            "playlist_end": _coerce_int(defaults["playlist_end"], 3),
            "processing_workers": _coerce_int(defaults.get("processing_workers"), 2),
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


def update_download_settings(db_path: str, youtube_cookie_text: Optional[str]) -> None:
    ensure_config_seeded(db_path)
    now = _utcnow().isoformat()
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


def materialize_youtube_cookie_file(db_path: str, cookie_path: Optional[str] = None) -> Optional[str]:
    stored = get_stored_config(db_path)
    cookie_text = stored["download_settings"].get("youtube_cookie_text")
    if not cookie_text:
        return None

    if cookie_path:
        target_path = Path(str(cookie_path)).expanduser()
    else:
        target_path = Path(tempfile.gettempdir()) / f"getoffline-yt-dlp-cookies-{Path(db_path).name}.txt"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(cookie_text, encoding="utf-8")
    return str(target_path)


def replace_sources(db_path: str, youtube: List[Dict[str, Any]], podcasts: List[Dict[str, Any]]) -> None:
    ensure_config_seeded(db_path)
    now = _utcnow().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM source_configs")

        for idx, item in enumerate(youtube):
            conn.execute(
                """
                INSERT INTO source_configs (
                    source_type, position, name, url, media_type, enabled, subtitles, subtitle_offset_seconds, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    now,
                ),
            )

        for idx, item in enumerate(podcasts):
            conn.execute(
                """
                INSERT INTO source_configs (
                    source_type, position, name, url, media_type, enabled, subtitles, subtitle_offset_seconds, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    now,
                ),
            )

        conn.commit()


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
    enabled: bool = True,
) -> None:
    ensure_config_seeded(db_path)
    now = _utcnow().isoformat()
    with sqlite3.connect(db_path) as conn:
        current_position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM source_configs WHERE source_type = ?",
            (source_type,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO source_configs (
                source_type, position, name, url, media_type, enabled, subtitles, subtitle_offset_seconds, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_type,
                int(current_position or 0),
                str(name or "").strip(),
                str(url or "").strip(),
                (str(media_type).strip().lower() if media_type is not None else None),
                1 if enabled else 0,
                1 if subtitles else 0,
                subtitle_offset_seconds,
                now,
            ),
        )
        conn.commit()


def delete_source_config(db_path: str, row_id: int) -> bool:
    ensure_config_seeded(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("DELETE FROM source_configs WHERE id = ?", (int(row_id),))
        conn.commit()
        return (cur.rowcount or 0) > 0


def set_source_enabled(db_path: str, row_id: int, enabled: bool) -> bool:
    ensure_config_seeded(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE source_configs SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _utcnow().isoformat(), int(row_id)),
        )
        conn.commit()
        return (cur.rowcount or 0) > 0


def _init_database_sqlite(db_path: str) -> None:
    apply_migrations(db_path)


def _ensure_downloads_columns_sqlite(db_path: str) -> None:
    _migration_0002_add_playback_columns(db_path)
    _migration_0006_add_favorite_column(db_path)


def _is_downloaded_sqlite(db_path: str, source_type: str, source_name: str, item_uid: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT COALESCE(file_path, '') FROM downloads
            WHERE source_type = ? AND source_name = ? AND item_uid = ? AND download_status = 'downloaded'
            LIMIT 1
            """,
            (source_type, source_name, item_uid),
        ).fetchone()
        if row is None:
            return False
        file_path = str(row[0] or "").strip()
        if not file_path:
            return False
        return Path(file_path).expanduser().is_file()


def _upsert_download_sqlite(db_path: str, payload: Dict[str, Any]):
    now = _utcnow().isoformat()
    values = {
        "source_type": payload["source_type"],
        "source_name": payload["source_name"],
        "source_url": payload.get("source_url"),
        "item_uid": payload["item_uid"],
        "item_id": payload.get("item_id"),
        "item_url": payload.get("item_url"),
        "media_url": payload.get("media_url"),
        "title": payload.get("title"),
        "description": payload.get("description"),
        "uploader": payload.get("uploader"),
        "channel": payload.get("channel"),
        "extractor": payload.get("extractor"),
        "playlist_id": payload.get("playlist_id"),
        "playlist_title": payload.get("playlist_title"),
        "upload_date": payload.get("upload_date"),
        "duration_seconds": payload.get("duration_seconds"),
        "file_path": payload.get("file_path"),
        "file_ext": payload.get("file_ext"),
        "file_size_bytes": payload.get("file_size_bytes"),
        "expected_bytes": payload.get("expected_bytes"),
        "format_id": payload.get("format_id"),
        "format_note": payload.get("format_note"),
        "audio_codec": payload.get("audio_codec"),
        "video_codec": payload.get("video_codec"),
        "resolution": payload.get("resolution"),
        "fps": payload.get("fps"),
        "subtitle_enabled": 1 if payload.get("subtitle_enabled", True) else 0,
        "subtitle_path": payload.get("subtitle_path"),
        "download_status": payload.get("download_status", "downloaded"),
        "error_message": payload.get("error_message"),
        "raw_metadata_json": _coerce_json(payload.get("raw_metadata")),
        "first_seen_at": now,
        "last_seen_at": now,
        "completed_at": now if payload.get("download_status", "downloaded") == "downloaded" else None,
        "played": 1 if payload.get("played", False) else 0,
        "favorite": 1 if payload.get("favorite", False) else 0,
        "played_at": payload.get("played_at"),
    }

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO downloads (
                source_type, source_name, source_url, item_uid, item_id, item_url, media_url,
                title, description, uploader, channel, extractor, playlist_id, playlist_title,
                upload_date, duration_seconds, file_path, file_ext, file_size_bytes, expected_bytes,
                format_id, format_note, audio_codec, video_codec, resolution, fps,
                subtitle_enabled, subtitle_path, download_status, error_message, raw_metadata_json,
                first_seen_at, last_seen_at, completed_at, played, favorite, played_at
            ) VALUES (
                :source_type, :source_name, :source_url, :item_uid, :item_id, :item_url, :media_url,
                :title, :description, :uploader, :channel, :extractor, :playlist_id, :playlist_title,
                :upload_date, :duration_seconds, :file_path, :file_ext, :file_size_bytes, :expected_bytes,
                :format_id, :format_note, :audio_codec, :video_codec, :resolution, :fps,
                :subtitle_enabled, :subtitle_path, :download_status, :error_message, :raw_metadata_json,
                :first_seen_at, :last_seen_at, :completed_at, :played, :favorite, :played_at
            )
            ON CONFLICT(source_type, source_name, item_uid) DO UPDATE SET
                source_url=excluded.source_url,
                item_id=excluded.item_id,
                item_url=excluded.item_url,
                media_url=excluded.media_url,
                title=excluded.title,
                description=excluded.description,
                uploader=excluded.uploader,
                channel=excluded.channel,
                extractor=excluded.extractor,
                playlist_id=excluded.playlist_id,
                playlist_title=excluded.playlist_title,
                upload_date=excluded.upload_date,
                duration_seconds=excluded.duration_seconds,
                file_path=excluded.file_path,
                file_ext=excluded.file_ext,
                file_size_bytes=excluded.file_size_bytes,
                expected_bytes=excluded.expected_bytes,
                format_id=excluded.format_id,
                format_note=excluded.format_note,
                audio_codec=excluded.audio_codec,
                video_codec=excluded.video_codec,
                resolution=excluded.resolution,
                fps=excluded.fps,
                subtitle_enabled=excluded.subtitle_enabled,
                subtitle_path=excluded.subtitle_path,
                download_status=excluded.download_status,
                error_message=excluded.error_message,
                raw_metadata_json=excluded.raw_metadata_json,
                last_seen_at=excluded.last_seen_at,
                completed_at=excluded.completed_at,
                played=COALESCE(downloads.played, 0),
                favorite=COALESCE(downloads.favorite, 0),
                played_at=downloads.played_at
            """,
            values,
        )
        conn.commit()


if HAS_SQLALCHEMY:
    class Base(DeclarativeBase):
        pass


    class DownloadRecord(Base):
        __tablename__ = "downloads"
        __table_args__ = (UniqueConstraint("source_type", "source_name", "item_uid", name="uq_download_source_item"),)

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        source_type: Mapped[str] = mapped_column(String(32), nullable=False)
        source_name: Mapped[str] = mapped_column(String(255), nullable=False)
        source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        item_uid: Mapped[str] = mapped_column(String(255), nullable=False)
        item_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
        item_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        media_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        uploader: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
        channel: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
        extractor: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
        playlist_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
        playlist_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        upload_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
        duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
        file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        file_ext: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
        file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
        expected_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
        format_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
        format_note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
        audio_codec: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
        video_codec: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
        resolution: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
        fps: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
        subtitle_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
        subtitle_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        download_status: Mapped[str] = mapped_column(String(32), nullable=False, default="downloaded")
        error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        raw_metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
        first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
        last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
        completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
        played: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
        favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
        played_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
        last_position_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
        total_listened_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
        last_position_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


    @lru_cache(maxsize=4)
    def _engine_for(db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(f"sqlite:///{db_path}", future=True)


    def init_database(db_path: str) -> None:
        apply_migrations(db_path)
        Base.metadata.create_all(_engine_for(db_path))


    def is_downloaded(db_path: str, source_type: str, source_name: str, item_uid: str) -> bool:
        with Session(_engine_for(db_path)) as session:
            stmt = select(DownloadRecord.file_path).where(
                DownloadRecord.source_type == source_type,
                DownloadRecord.source_name == source_name,
                DownloadRecord.item_uid == item_uid,
                DownloadRecord.download_status == "downloaded",
            )
            file_path = session.execute(stmt).scalar_one_or_none()
            if not file_path:
                return False
            return Path(str(file_path)).expanduser().is_file()


    def upsert_download(db_path: str, payload: Dict[str, Any]):
        now = _utcnow()
        with Session(_engine_for(db_path)) as session:
            stmt = select(DownloadRecord).where(
                DownloadRecord.source_type == payload["source_type"],
                DownloadRecord.source_name == payload["source_name"],
                DownloadRecord.item_uid == payload["item_uid"],
            )
            existing = session.execute(stmt).scalar_one_or_none()
            if existing is None:
                existing = DownloadRecord(
                    source_type=payload["source_type"],
                    source_name=payload["source_name"],
                    source_url=payload.get("source_url"),
                    item_uid=payload["item_uid"],
                    first_seen_at=now,
                )
                session.add(existing)

            for key in [
                "item_id", "item_url", "media_url", "title", "description", "uploader", "channel", "extractor",
                "playlist_id", "playlist_title", "upload_date", "duration_seconds", "file_path", "file_ext",
                "file_size_bytes", "expected_bytes", "format_id", "format_note", "audio_codec", "video_codec",
                "resolution", "fps", "subtitle_path", "download_status", "error_message",
            ]:
                setattr(existing, key, payload.get(key))
            existing.subtitle_enabled = bool(payload.get("subtitle_enabled", True))
            existing.raw_metadata_json = _coerce_json(payload.get("raw_metadata"))
            existing.last_seen_at = now
            if existing.download_status == "downloaded":
                existing.completed_at = now
            session.commit()

    def mark_download_played(db_path: str, row_id: int, played: bool = True) -> bool:
        now = _utcnow() if played else None
        with Session(_engine_for(db_path)) as session:
            record = session.get(DownloadRecord, int(row_id))
            if record is None:
                return False
            record.played = bool(played)
            record.played_at = now
            record.last_seen_at = _utcnow()
            session.commit()
            return True

    def mark_all_downloads_played(db_path: str) -> int:
        now = _utcnow()
        with Session(_engine_for(db_path)) as session:
            updated = session.query(DownloadRecord).filter(DownloadRecord.played.is_(False)).update(
                {
                    DownloadRecord.played: True,
                    DownloadRecord.played_at: now,
                    DownloadRecord.last_seen_at: now,
                },
                synchronize_session=False,
            )
            session.commit()
            return int(updated or 0)

    def mark_download_favorite(db_path: str, row_id: int, favorite: bool = True) -> bool:
        with Session(_engine_for(db_path)) as session:
            record = session.get(DownloadRecord, int(row_id))
            if record is None:
                return False
            record.favorite = bool(favorite)
            record.last_seen_at = _utcnow()
            session.commit()
            return True

    def delete_download_entry(db_path: str, row_id: int) -> bool:
        with Session(_engine_for(db_path)) as session:
            record = session.get(DownloadRecord, int(row_id))
            if record is None:
                return False
            session.delete(record)
            session.commit()
            return True

    def get_download_position_seconds(db_path: str, row_id: int) -> float:
        with Session(_engine_for(db_path)) as session:
            record = session.get(DownloadRecord, int(row_id))
            if record is None:
                return 0.0
            return float(record.last_position_seconds or 0)

    def update_download_position_seconds(db_path: str, row_id: int, position_seconds: float) -> bool:
        safe_position = max(0.0, float(position_seconds or 0.0))
        now = _utcnow()
        with Session(_engine_for(db_path)) as session:
            record = session.get(DownloadRecord, int(row_id))
            if record is None:
                return False
            previous = max(0.0, float(record.last_position_seconds or 0.0))
            listened_delta = max(0.0, safe_position - previous)
            record.last_position_seconds = safe_position
            record.total_listened_seconds = max(0.0, float(record.total_listened_seconds or 0.0)) + listened_delta
            record.last_position_updated_at = now
            record.last_seen_at = now
            session.commit()
            return True

    def get_total_listened_seconds(db_path: str) -> float:
        with Session(_engine_for(db_path)) as session:
            stmt = select(DownloadRecord.total_listened_seconds)
            values = session.execute(stmt).scalars().all()
            return float(sum(float(value or 0.0) for value in values))


else:
    def init_database(db_path: str) -> None:
        _init_database_sqlite(db_path)

    def is_downloaded(db_path: str, source_type: str, source_name: str, item_uid: str) -> bool:
        return _is_downloaded_sqlite(db_path, source_type, source_name, item_uid)

    def upsert_download(db_path: str, payload: Dict[str, Any]):
        _upsert_download_sqlite(db_path, payload)

    def mark_download_played(db_path: str, row_id: int, played: bool = True) -> bool:
        now = _utcnow().isoformat() if played else None
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "UPDATE downloads SET played = ?, played_at = ?, last_seen_at = ? WHERE id = ?",
                (1 if played else 0, now, _utcnow().isoformat(), int(row_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_all_downloads_played(db_path: str) -> int:
        now = _utcnow().isoformat()
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "UPDATE downloads SET played = 1, played_at = ?, last_seen_at = ? WHERE COALESCE(played, 0) = 0",
                (now, now),
            )
            conn.commit()
            return int(cur.rowcount or 0)

    def mark_download_favorite(db_path: str, row_id: int, favorite: bool = True) -> bool:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "UPDATE downloads SET favorite = ?, last_seen_at = ? WHERE id = ?",
                (1 if favorite else 0, _utcnow().isoformat(), int(row_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_download_entry(db_path: str, row_id: int) -> bool:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute("DELETE FROM downloads WHERE id = ?", (int(row_id),))
            conn.commit()
            return cur.rowcount > 0

    def get_download_position_seconds(db_path: str, row_id: int) -> float:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(last_position_seconds, 0) FROM downloads WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            if row is None:
                return 0.0
            return float(row[0] or 0.0)

    def update_download_position_seconds(db_path: str, row_id: int, position_seconds: float) -> bool:
        safe_position = max(0.0, float(position_seconds or 0.0))
        now = _utcnow().isoformat()
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(last_position_seconds, 0), COALESCE(total_listened_seconds, 0) FROM downloads WHERE id = ?",
                (int(row_id),),
            ).fetchone()
            if row is None:
                return False
            previous = max(0.0, float(row[0] or 0.0))
            total = max(0.0, float(row[1] or 0.0))
            listened_delta = max(0.0, safe_position - previous)
            cur = conn.execute(
                "UPDATE downloads SET last_position_seconds = ?, total_listened_seconds = ?, last_position_updated_at = ?, last_seen_at = ? WHERE id = ?",
                (safe_position, total + listened_delta, now, now, int(row_id)),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_total_listened_seconds(db_path: str) -> float:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_listened_seconds), 0) FROM downloads"
            ).fetchone()
            return float((row[0] if row else 0.0) or 0.0)
