from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from api.services.dashboard_actions import import_manual_file

MEDIA_SUFFIXES = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".mp4", ".mkv", ".webm", ".mov", ".pdf"}


class Command(BaseCommand):
    help = "Import existing files from the downloads volume into the library."

    def add_arguments(self, parser):
        parser.add_argument(
            "downloads_root",
            nargs="?",
            default="./downloads",
            help="Root directory containing per-channel subdirectories.",
        )
        parser.add_argument(
            "--profile-id",
            default="default",
            help="Profile to import into.",
        )
        parser.add_argument(
            "--recursive",
            action="store_true",
            help="Import files recursively under each channel directory.",
        )

    def handle(self, *args, **options):
        downloads_root = Path(str(options["downloads_root"])).expanduser().resolve()
        if not downloads_root.exists() or not downloads_root.is_dir():
            raise CommandError(f"Downloads root not found: {downloads_root}")
        profile_id = str(options["profile_id"]).strip() or "default"
        recursive = bool(options["recursive"])

        channel_dirs = [p for p in downloads_root.iterdir() if p.is_dir()]
        if self._contains_media_files(downloads_root):
            channel_dirs = [downloads_root]

        imported = 0
        skipped = 0
        failed = 0
        for channel_dir in sorted(channel_dirs):
            channel_name = channel_dir.name
            candidates = (
                channel_dir.rglob("*") if recursive else channel_dir.iterdir()
            )
            for path in sorted(candidates):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in MEDIA_SUFFIXES:
                    continue
                try:
                    import_manual_file(
                        profile_id,
                        path,
                        source_name=channel_name,
                        downloads_root=downloads_root,
                    )
                    imported += 1
                except (ValueError, OSError, DatabaseError) as exc:
                    skipped += 1
                    self.stderr.write(f"Skipped {path}: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {imported} file(s) from {downloads_root} "
                f"(skipped {skipped}, failed {failed})"
            )
        )

    def _contains_media_files(self, directory: Path) -> bool:
        return any(
            self._is_importable_file(path)
            for path in directory.iterdir()
        )

    def _is_importable_file(self, path: Path) -> bool:
        return (
            path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in MEDIA_SUFFIXES
        )
