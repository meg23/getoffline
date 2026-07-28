# ruff: noqa: E402
import os
import sys
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")

import django

django.setup()

from django.http import Http404

from frontend import views
from models.domain import DownloadStatus


class AppViewHelperTests(unittest.TestCase):
    def test_human_size_formats_empty_bytes_and_larger_units(self):
        self.assertEqual(views._human_size(None), "—")
        self.assertEqual(views._human_size(0), "—")
        self.assertEqual(views._human_size(512), "512 B")
        self.assertEqual(views._human_size(1536), "1.50 KB")
        self.assertEqual(views._human_size(2 * 1024 * 1024), "2.00 MB")

    def test_srt_to_vtt_removes_sequence_numbers_and_converts_timestamps(self):
        srt = "\ufeff1\n00:00:01,250 --> 00:00:03,500 position:50%\nHello\n\n2\nNot a timestamp\n"

        self.assertEqual(
            views._srt_to_vtt(srt),
            "WEBVTT\n\n00:00:01.250 --> 00:00:03.500 position:50%\nHello\n\nNot a timestamp\n",
        )

    def test_decorate_download_sets_display_fields_and_missing_status(self):
        item = SimpleNamespace(
            last_position_seconds=37,
            file_size_bytes=2048,
            file_ext="mp4",
            file_path="/media/video.mp4",
            played=False,
            download_status=DownloadStatus.MISSING.value,
            subtitle_path="/media/video.srt",
            subtitle_path_relative="",
        )

        decorated = views._decorate_download(item)

        self.assertIs(decorated, item)
        self.assertEqual(item.display_size, "2.00 KB")
        self.assertEqual(item.display_type, "MP4")
        self.assertEqual(item.display_kind, "video")
        self.assertEqual(item.status_label, "MISSING")
        self.assertEqual(item.status_class, "status-missing")
        self.assertTrue(item.has_subtitles)
        self.assertIsNone(item.resolved_subtitle_path)

    def test_resolve_media_path_prefers_existing_relative_path_before_absolute_path(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            relative_media = root / "relative.mp3"
            absolute_media = root / "absolute.mp3"
            relative_media.write_bytes(b"relative")
            absolute_media.write_bytes(b"absolute")
            item = SimpleNamespace(
                profile_id="default",
                file_path_relative="relative.mp3",
                file_path=str(absolute_media),
            )

            with patch("frontend.views._profile_output_root", return_value=root):
                self.assertEqual(views._resolve_media_path(item), relative_media)

    def test_resolve_subtitle_path_rejects_paths_outside_profile_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "root"
            root.mkdir()
            outside = Path(tmpdir) / "outside.srt"
            media = root / "episode.mp3"
            outside.write_text("outside", encoding="utf-8")
            media.write_bytes(b"audio")
            item = SimpleNamespace(
                profile_id="default",
                file_path_relative="episode.mp3",
                file_path="",
                subtitle_path=str(outside),
                subtitle_path_relative="",
            )

            with patch("frontend.views._profile_output_root", return_value=root):
                self.assertIsNone(views._resolve_subtitle_path(item))

    def test_safe_path_rejects_missing_or_directory_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(Http404):
                views._safe_path(str(Path(tmpdir)))
            with self.assertRaises(Http404):
                views._safe_path(str(Path(tmpdir) / "missing.mp3"))


if __name__ == "__main__":
    unittest.main()
