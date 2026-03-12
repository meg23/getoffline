import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import init_database, upsert_download  # noqa: E402
from webapp import (  # noqa: E402
    _parse_range_header,
    _resolve_safe_media_path,
    fetch_downloaded_media_rows,
)


class WebAppHelpersTests(unittest.TestCase):
    def test_parse_range_header(self):
        self.assertEqual(_parse_range_header("bytes=0-99", 1000), {"start": 0, "end": 99})
        self.assertEqual(_parse_range_header("bytes=100-", 1000), {"start": 100, "end": 999})
        self.assertEqual(_parse_range_header("bytes=-50", 1000), {"start": 950, "end": 999})
        self.assertIsNone(_parse_range_header("bytes=1200-1500", 1000))

    def test_resolve_safe_media_path(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside_dir:
            root = Path(tmpdir)
            media = root / "test.mp3"
            media.write_text("x", encoding="utf-8")
            unsafe = Path(outside_dir) / "outside.mp3"
            unsafe.write_text("x", encoding="utf-8")

            self.assertEqual(_resolve_safe_media_path(root, str(media)), media.resolve())
            self.assertIsNone(_resolve_safe_media_path(root, str(unsafe)))


class WebAppDatabaseRowsTests(unittest.TestCase):
    def test_fetch_downloaded_media_rows_reads_saved_downloads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "downloads.sqlite3"
            media = root / "episode.mp3"
            media.write_text("audio", encoding="utf-8")

            init_database(str(db_path))
            upsert_download(
                str(db_path),
                {
                    "source_type": "podcast",
                    "source_name": "TestPodcast",
                    "item_uid": "uid-1",
                    "item_url": "https://cdn.example.com/episode.mp3",
                    "media_url": "https://cdn.example.com/episode.mp3",
                    "title": "Episode 1",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "subtitle_enabled": True,
                    "download_status": "downloaded",
                    "raw_metadata": {"title": "Episode 1"},
                },
            )

            rows = fetch_downloaded_media_rows(db_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].title, "Episode 1")
            self.assertEqual(Path(rows[0].file_path), media)


if __name__ == "__main__":
    unittest.main()
