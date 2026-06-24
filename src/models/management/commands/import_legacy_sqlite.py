import sqlite3
from datetime import timezone as datetime_timezone
from pathlib import Path
from typing import Any, Optional

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from models.models import (
    AppConfigValue,
    Download,
    DownloadSettings,
    MediaSummary,
    ProfileConfigValue,
    ProfileDownloadSettings,
    SourceConfig,
    TranscriptSegment,
)


DOWNLOAD_FIELDS = [
    "source_type",
    "source_name",
    "source_url",
    "item_uid",
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
    "file_path_relative",
    "file_ext",
    "file_size_bytes",
    "subtitle_path",
    "subtitle_path_relative",
    "download_status",
    "raw_metadata_json",
    "first_seen_at",
    "last_seen_at",
    "completed_at",
    "played",
    "favorite",
    "played_at",
    "last_position_seconds",
    "total_listened_seconds",
    "last_position_updated_at",
]


SOURCE_FIELDS = [
    "source_type",
    "position",
    "name",
    "url",
    "media_type",
    "subtitles",
    "subtitle_offset_seconds",
    "enabled",
    "max_downloads",
    "delete_explicit_content",
    "include_shorts",
    "include_livestreams",
]


class Command(BaseCommand):
    help = "Import a legacy GetOffline SQLite database into the configured Django database."

    def add_arguments(self, parser):
        parser.add_argument("sqlite_path", help="Path to the legacy SQLite database.")
        parser.add_argument(
            "--profile-id",
            default="default",
            help="Destination profile/user partition for imported rows (default: default).",
        )
        parser.add_argument(
            "--replace-profile",
            action="store_true",
            help="Delete existing destination rows for the profile before importing.",
        )
        parser.add_argument(
            "--skip-config",
            action="store_true",
            help="Only import library metadata, transcripts, and summaries; skip settings/source tables.",
        )
        parser.add_argument(
            "--progress-interval",
            type=int,
            default=500,
            help="Print progress after this many rows while importing large tables (default: 500).",
        )
        parser.add_argument(
            "--media-root",
            help=(
                "Profile media directory visible to the Django app. "
                "Defaults to /app/downloads/<profile-id>."
            ),
        )

    def handle(self, *args, **options):
        sqlite_path = Path(options["sqlite_path"]).expanduser()
        profile_id = str(options["profile_id"]).strip() or "default"
        self.progress_interval = max(1, int(options["progress_interval"]))
        media_root = self._media_root(options.get("media_root"), profile_id)
        if not sqlite_path.exists():
            raise CommandError(f"Legacy SQLite database not found: {sqlite_path}")

        self._write_progress(f"Opening legacy SQLite database: {sqlite_path}")
        with sqlite3.connect(str(sqlite_path)) as legacy:
            legacy.row_factory = sqlite3.Row
            self._validate_tables(legacy)
            self._write_progress(f"Importing into destination profile: {profile_id}")
            with transaction.atomic():
                if options["replace_profile"]:
                    self._write_progress(
                        f"Clearing existing rows for destination profile: {profile_id}"
                    )
                    self._delete_profile(profile_id)
                counts = self._import_all(
                    legacy,
                    profile_id,
                    skip_config=options["skip_config"],
                    media_root=media_root,
                )

        self.stdout.write(
            self.style.SUCCESS(
                "Imported legacy SQLite database for profile "
                f"{profile_id!r}: {counts['downloads']} downloads, "
                f"{counts['transcript_segments']} transcript segments, "
                f"{counts['media_summaries']} summaries, "
                f"{counts['source_configs']} sources."
            )
        )

    def _validate_tables(self, legacy: sqlite3.Connection) -> None:
        tables = set()
        for row in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall():
            tables.add(row[0])
        if "downloads" not in tables:
            raise CommandError("Legacy database does not contain a downloads table.")

    def _delete_profile(self, profile_id: str) -> None:
        Download.objects.filter(profile_id=profile_id).delete()
        SourceConfig.objects.filter(profile_id=profile_id).delete()
        ProfileConfigValue.objects.filter(profile_id=profile_id).delete()
        ProfileDownloadSettings.objects.filter(profile_id=profile_id).delete()

    def _import_all(
        self,
        legacy: sqlite3.Connection,
        profile_id: str,
        *,
        skip_config: bool,
        media_root: Path,
    ) -> dict[str, int]:
        counts = {
            "downloads": 0,
            "transcript_segments": 0,
            "media_summaries": 0,
            "source_configs": 0,
        }
        if not skip_config:
            self._write_progress("Importing settings and sources...")
            counts["source_configs"] = self._import_config(legacy, profile_id)
            self._write_progress(
                f"Imported {counts['source_configs']} sources/settings rows."
            )
        else:
            self._write_progress("Skipping settings and sources.")
        self._write_progress(f"Setting profile media root to: {media_root}")
        self._set_profile_output_root(profile_id, media_root)
        self._write_progress("Importing downloads...")
        id_map = self._import_downloads(legacy, profile_id, media_root)
        counts["downloads"] = len(id_map)
        self._write_progress(f"Imported {counts['downloads']} downloads.")
        self._write_progress("Importing transcript segments...")
        counts["transcript_segments"] = self._import_transcript_segments(legacy, id_map)
        self._write_progress(
            f"Imported {counts['transcript_segments']} transcript segments."
        )
        self._write_progress("Importing media summaries...")
        counts["media_summaries"] = self._import_media_summaries(legacy, id_map)
        self._write_progress(f"Imported {counts['media_summaries']} media summaries.")
        return counts

    def _import_config(self, legacy: sqlite3.Connection, profile_id: str) -> int:
        now = timezone.now()
        if self._has_table(legacy, "app_config"):
            for row in legacy.execute("SELECT key, value, updated_at FROM app_config"):
                ProfileConfigValue.objects.update_or_create(
                    profile_id=profile_id,
                    key=row["key"],
                    defaults={
                        "value": row["value"],
                        "updated_at": self._datetime(row["updated_at"], now),
                    },
                )
                if profile_id == "default":
                    AppConfigValue.objects.update_or_create(
                        key=row["key"],
                        defaults={
                            "value": row["value"],
                            "updated_at": self._datetime(row["updated_at"], now),
                        },
                    )

        if self._has_table(legacy, "download_settings"):
            row = legacy.execute(
                "SELECT youtube_cookie_text, cookie_updated_at, updated_at FROM download_settings WHERE id = 1"
            ).fetchone()
            if row:
                ProfileDownloadSettings.objects.update_or_create(
                    profile_id=profile_id,
                    defaults={
                        "youtube_cookie_text": row["youtube_cookie_text"],
                        "cookie_updated_at": self._datetime(
                            row["cookie_updated_at"], None
                        ),
                        "updated_at": self._datetime(row["updated_at"], now),
                    },
                )
                if profile_id == "default":
                    DownloadSettings.objects.update_or_create(
                        id=1,
                        defaults={
                            "youtube_cookie_text": row["youtube_cookie_text"],
                            "cookie_updated_at": self._datetime(
                                row["cookie_updated_at"], None
                            ),
                            "updated_at": self._datetime(row["updated_at"], now),
                        },
                    )

        source_table = (
            "source_configs"
            if self._has_table(legacy, "source_configs")
            else "config_downloads"
        )
        if not self._has_table(legacy, source_table):
            return 0
        count = 0
        for row in legacy.execute(
            f"SELECT * FROM {source_table} ORDER BY source_type, position, id"
        ):
            payload = self._source_payload(row, now)
            SourceConfig.objects.update_or_create(
                profile_id=profile_id,
                source_type=payload["source_type"],
                position=payload["position"],
                defaults=self._source_defaults(payload),
            )
            count += 1
        return count

    def _import_downloads(
        self, legacy: sqlite3.Connection, profile_id: str, media_root: Path
    ) -> dict[int, int]:
        id_map = {}
        count = 0
        for row in legacy.execute("SELECT * FROM downloads ORDER BY id"):
            payload = self._download_payload(row, profile_id, media_root)
            download = self._upsert_download(payload)
            id_map[int(row["id"])] = int(download.id)
            count += 1
            self._write_row_progress("downloads", count)
        return id_map

    def _import_transcript_segments(
        self, legacy: sqlite3.Connection, id_map: dict[int, int]
    ) -> int:
        if not self._has_table(legacy, "transcript_segments"):
            return 0
        count = 0
        for row in legacy.execute("SELECT * FROM transcript_segments ORDER BY id"):
            new_download_id = id_map.get(int(row["download_id"]))
            if not new_download_id:
                continue
            TranscriptSegment.objects.update_or_create(
                download_id=new_download_id,
                subtitle_path=row["subtitle_path"],
                start_seconds=row["start_seconds"],
                text=row["text"],
                defaults={"end_seconds": row["end_seconds"]},
            )
            count += 1
            self._write_row_progress("transcript segments", count)
        return count

    def _import_media_summaries(
        self, legacy: sqlite3.Connection, id_map: dict[int, int]
    ) -> int:
        if not self._has_table(legacy, "media_summaries"):
            return 0
        count = 0
        for row in legacy.execute("SELECT * FROM media_summaries"):
            new_download_id = id_map.get(int(row["download_id"]))
            if not new_download_id:
                continue
            MediaSummary.objects.update_or_create(
                download_id=new_download_id,
                defaults={
                    "summary_text": row["summary_text"],
                    "model_name": row["model_name"],
                    "source_segment_count": row["source_segment_count"],
                    "updated_at": self._datetime(row["updated_at"], timezone.now()),
                },
            )
            count += 1
            self._write_row_progress("media summaries", count)
        return count

    def _upsert_download(self, payload: dict[str, Any]) -> Download:
        lookup = {
            "profile_id": payload["profile_id"],
            "source_type": payload["source_type"],
            "source_name": payload["source_name"],
            "item_uid": payload["item_uid"],
        }
        existing = Download.objects.filter(**lookup).order_by("id").first()
        if existing is None:
            return Download.objects.create(**payload)
        for key, value in payload.items():
            setattr(existing, key, value)
        existing.save()
        return existing

    def _download_payload(
        self, row: sqlite3.Row, profile_id: str, media_root: Path
    ) -> dict[str, Any]:
        now = timezone.now()
        payload = {"profile_id": profile_id}
        columns = self._row_columns(row)
        for field in DOWNLOAD_FIELDS:
            value = row[field] if field in columns else None
            if field in {"first_seen_at", "last_seen_at"}:
                value = self._datetime(value, now)
            elif field in {"completed_at", "played_at", "last_position_updated_at"}:
                value = self._datetime(value, None)
            elif field in {"played", "favorite"}:
                value = bool(value)
            elif field == "download_status":
                value = value or "downloaded"
            elif field == "item_uid":
                value = (
                    value
                    or row["item_id"]
                    or row["item_url"]
                    or row["media_url"]
                    or f"legacy:{row['id']}"
                )
            elif field in {"source_type", "source_name"}:
                value = value or "legacy"
            elif field in {"last_position_seconds", "total_listened_seconds"}:
                value = float(value or 0)
            payload[field] = value
        self._rewrite_media_paths(payload, media_root)
        return payload

    def _media_root(self, value: Any, profile_id: str) -> Path:
        if value:
            return Path(str(value)).expanduser().resolve()
        return Path("/app/downloads") / profile_id

    def _set_profile_output_root(self, profile_id: str, media_root: Path) -> None:
        ProfileConfigValue.objects.update_or_create(
            profile_id=profile_id,
            key="output_root",
            defaults={"value": str(media_root), "updated_at": timezone.now()},
        )

    def _rewrite_media_paths(self, payload: dict[str, Any], media_root: Path) -> None:
        self._rewrite_payload_path(
            payload, "file_path", "file_path_relative", media_root
        )
        self._rewrite_payload_path(
            payload, "subtitle_path", "subtitle_path_relative", media_root
        )

    def _rewrite_payload_path(
        self,
        payload: dict[str, Any],
        absolute_key: str,
        relative_key: str,
        media_root: Path,
    ) -> None:
        relative_path = payload.get(relative_key)
        if not relative_path:
            return
        payload[absolute_key] = str(media_root / str(relative_path))

    def _source_payload(self, row: sqlite3.Row, now: Any) -> dict[str, Any]:
        columns = self._row_columns(row)
        media_type = None
        if "media_type" in columns:
            media_type = row["media_type"]
        elif "download_type" in columns:
            media_type = row["download_type"]

        enabled = True
        if "enabled" in columns:
            enabled = bool(row["enabled"])

        max_downloads = None
        if "max_downloads" in columns:
            max_downloads = row["max_downloads"]

        delete_explicit_content = False
        if "delete_explicit_content" in columns:
            delete_explicit_content = bool(row["delete_explicit_content"])

        include_shorts = False
        if "include_shorts" in columns:
            include_shorts = bool(row["include_shorts"])

        include_livestreams = False
        if "include_livestreams" in columns:
            include_livestreams = bool(row["include_livestreams"])

        updated_at = now
        if "updated_at" in columns:
            updated_at = self._datetime(row["updated_at"], now)

        return {
            "source_type": row["source_type"],
            "position": int(row["position"]),
            "name": row["name"],
            "url": row["url"],
            "media_type": media_type,
            "subtitles": bool(row["subtitles"]),
            "subtitle_offset_seconds": row["subtitle_offset_seconds"],
            "enabled": enabled,
            "max_downloads": max_downloads,
            "delete_explicit_content": delete_explicit_content,
            "include_shorts": include_shorts,
            "include_livestreams": include_livestreams,
            "updated_at": updated_at,
        }

    def _has_table(self, legacy: sqlite3.Connection, table_name: str) -> bool:
        return (
            legacy.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
            is not None
        )

    def _source_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        defaults = {}
        for key in SOURCE_FIELDS:
            if key == "source_type" or key == "position":
                continue
            defaults[key] = payload[key]
        return defaults

    def _row_columns(self, row: sqlite3.Row) -> set[str]:
        columns = set()
        for key in row.keys():
            columns.add(key)
        return columns

    def _write_progress(self, message: str) -> None:
        self.stdout.write(message)
        self.stdout.flush()

    def _write_row_progress(self, label: str, count: int) -> None:
        if count % self.progress_interval != 0:
            return
        self._write_progress(f"Imported {count} {label}...")

    def _datetime(self, value: Any, fallback: Optional[Any]) -> Optional[Any]:
        if value in (None, ""):
            return fallback
        if hasattr(value, "tzinfo"):
            return value
        parsed = parse_datetime(str(value))
        if parsed is None:
            return fallback
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, datetime_timezone.utc)
        return parsed
