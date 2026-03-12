import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from sqlalchemy import (
        Boolean,
        DateTime,
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


def _init_database_sqlite(db_path: str) -> None:
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
                UNIQUE(source_type, source_name, item_uid)
            )
            """
        )
        conn.commit()


def _is_downloaded_sqlite(db_path: str, source_type: str, source_name: str, item_uid: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT id FROM downloads
            WHERE source_type = ? AND source_name = ? AND item_uid = ? AND download_status = 'downloaded'
            LIMIT 1
            """,
            (source_type, source_name, item_uid),
        )
        return cur.fetchone() is not None


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
                first_seen_at, last_seen_at, completed_at
            ) VALUES (
                :source_type, :source_name, :source_url, :item_uid, :item_id, :item_url, :media_url,
                :title, :description, :uploader, :channel, :extractor, :playlist_id, :playlist_title,
                :upload_date, :duration_seconds, :file_path, :file_ext, :file_size_bytes, :expected_bytes,
                :format_id, :format_note, :audio_codec, :video_codec, :resolution, :fps,
                :subtitle_enabled, :subtitle_path, :download_status, :error_message, :raw_metadata_json,
                :first_seen_at, :last_seen_at, :completed_at
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
                completed_at=excluded.completed_at
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


    @lru_cache(maxsize=4)
    def _engine_for(db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(f"sqlite:///{db_path}", future=True)


    def init_database(db_path: str) -> None:
        Base.metadata.create_all(_engine_for(db_path))


    def is_downloaded(db_path: str, source_type: str, source_name: str, item_uid: str) -> bool:
        with Session(_engine_for(db_path)) as session:
            stmt = select(DownloadRecord.id).where(
                DownloadRecord.source_type == source_type,
                DownloadRecord.source_name == source_name,
                DownloadRecord.item_uid == item_uid,
                DownloadRecord.download_status == "downloaded",
            )
            return session.execute(stmt).first() is not None


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

else:
    def init_database(db_path: str) -> None:
        _init_database_sqlite(db_path)

    def is_downloaded(db_path: str, source_type: str, source_name: str, item_uid: str) -> bool:
        return _is_downloaded_sqlite(db_path, source_type, source_name, item_uid)

    def upsert_download(db_path: str, payload: Dict[str, Any]):
        _upsert_download_sqlite(db_path, payload)
