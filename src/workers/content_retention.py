"""Automatic retention cleanup for downloaded media."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from workers.download_store import (
    DOWNLOAD_STATUS_MISSING,
    DOWNLOAD_STATUS_RETENTION_DELETED,
    resolve_download_artifact_path,
)
from workers.logger import get_logger


log = get_logger("content_retention")


@dataclass(frozen=True)
class RetentionCleanupResult:
    deleted_files: int = 0
    marked_missing: int = 0
    marked_retention_deleted: int = 0
    ignored_manual: int = 0
    ignored_favorites: int = 0


def _parse_database_timestamp(value: Optional[str]) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def enforce_content_retention(
    db_path: str,
    output_root: str,
    retention_days: int,
    *,
    now: Optional[datetime] = None,
) -> RetentionCleanupResult:
    """Delete expired automatic media while retaining terminal database records.

    A retention value of zero disables cleanup. Manual uploads are never deleted or
    marked missing by this task, and favorite items are never automatically deleted.
    Non-manual rows whose files are already absent are marked missing regardless of
    favorite state or age while retention is enabled. Files deleted because they have
    expired receive a terminal retention status so feed updates do not download them
    again.
    """
    try:
        days = int(retention_days)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return RetentionCleanupResult()

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    cutoff = current_time.astimezone(timezone.utc) - timedelta(days=days)

    deleted_files = 0
    marked_missing = 0
    marked_retention_deleted = 0
    status_updates = []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, source_type, file_path, file_path_relative,
                   completed_at, first_seen_at, COALESCE(favorite, 0)
            FROM downloads
            WHERE download_status = 'downloaded'
            """
        ).fetchall()

        ignored_manual = sum(1 for row in rows if str(row[1]).lower() == "manual")
        ignored_favorites = 0
        for row_id, source_type, file_path, relative_path, completed_at, first_seen_at, favorite in rows:
            if str(source_type).lower() == "manual":
                continue

            resolved = resolve_download_artifact_path(output_root, file_path, relative_path)
            media_path = Path(resolved) if resolved else None
            if media_path is None or not media_path.is_file():
                status_updates.append(
                    (DOWNLOAD_STATUS_MISSING, "Media file is missing", int(row_id))
                )
                marked_missing += 1
                continue
            if bool(favorite):
                ignored_favorites += 1
                continue

            content_date = _parse_database_timestamp(completed_at) or _parse_database_timestamp(first_seen_at)
            if content_date is None or content_date > cutoff:
                continue

            try:
                media_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("Unable to delete expired media file %s: %s", media_path, exc)
                continue
            else:
                deleted_files += 1
            status_updates.append(
                (
                    DOWNLOAD_STATUS_RETENTION_DELETED,
                    "Media file removed by content retention",
                    int(row_id),
                )
            )
            marked_retention_deleted += 1

        if status_updates:
            timestamp = current_time.astimezone(timezone.utc).isoformat()
            update_parameters = []
            for download_status, error_message, row_id in status_updates:
                update_parameters.append(
                    (download_status, error_message, timestamp, row_id)
                )
            conn.executemany(
                """
                UPDATE downloads
                SET download_status = ?,
                    error_message = ?,
                    last_seen_at = ?
                WHERE id = ? AND download_status = 'downloaded'
                """,
                update_parameters,
            )
            conn.commit()

    result = RetentionCleanupResult(
        deleted_files=deleted_files,
        marked_missing=marked_missing,
        marked_retention_deleted=marked_retention_deleted,
        ignored_manual=ignored_manual,
        ignored_favorites=ignored_favorites,
    )
    if result.deleted_files or result.marked_missing or result.marked_retention_deleted:
        log.info(
            "Content retention complete: deleted=%s marked_missing=%s "
            "marked_retention_deleted=%s ignored_manual=%s ignored_favorites=%s",
            result.deleted_files,
            result.marked_missing,
            result.marked_retention_deleted,
            result.ignored_manual,
            result.ignored_favorites,
        )
    return result
