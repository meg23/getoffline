import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import init_database, mark_download_played, upsert_download  # noqa: E402
from webapp import (  # noqa: E402
    AppState,
    _parse_range_header,
    _render_index,
    _resolve_safe_media_path,
    fetch_downloaded_media_rows,
    trigger_background_update,
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

    def test_index_contains_update_button(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            body = _render_index(
                rows=[],
                output_root=root,
                database_path=root / "downloads.sqlite3",
                status={
                    "is_running": "no",
                    "last_started_at": "never",
                    "last_finished_at": "never",
                    "last_result": "idle",
                    "last_error": "none",
                    "last_items_count": "0",
                },
            )
            self.assertIn("Update Downloads", body)
            self.assertIn("action=\"/update\"", body)


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



    def test_mark_download_played_updates_row_state(self):
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
                    "item_uid": "uid-2",
                    "item_url": "https://cdn.example.com/episode2.mp3",
                    "media_url": "https://cdn.example.com/episode2.mp3",
                    "title": "Episode 2",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "subtitle_enabled": True,
                    "download_status": "downloaded",
                    "raw_metadata": {"title": "Episode 2"},
                },
            )

            before = fetch_downloaded_media_rows(db_path)
            self.assertFalse(before[0].played)

            updated = mark_download_played(str(db_path), before[0].row_id, played=True)
            self.assertTrue(updated)

            after = fetch_downloaded_media_rows(db_path)
            self.assertTrue(after[0].played)

class WebAppUpdateThreadTests(unittest.TestCase):
    def test_trigger_background_update_runs_downloads_once(self):
        calls = []

        def _runner(config, downloaded_items):
            _ = config
            calls.append("run")
            downloaded_items.append("item")
            time.sleep(0.2)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = AppState(
                output_root=root,
                database_path=root / "downloads.sqlite3",
                config={"defaults": {"output_root": str(root), "database_path": str(root / 'downloads.sqlite3')}},
                update_runner=_runner,
            )

            started = trigger_background_update(state)
            self.assertTrue(started)

            started_again = trigger_background_update(state)
            self.assertFalse(started_again)

            deadline = time.time() + 2
            while time.time() < deadline:
                with state.update_status.lock:
                    if not state.update_status.is_running and state.update_status.last_result == "ok":
                        break
                time.sleep(0.05)

            with state.update_status.lock:
                self.assertEqual(state.update_status.last_result, "ok")
                self.assertEqual(state.update_status.last_items_count, 1)
            self.assertEqual(calls, ["run"])


if __name__ == "__main__":
    unittest.main()
