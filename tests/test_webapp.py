import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import init_database, mark_all_downloads_played, mark_download_played, upsert_download  # noqa: E402
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
            self.assertIn("Mark all as played", body)


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

    def test_mark_all_downloads_played_updates_all_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "downloads.sqlite3"
            media1 = root / "episode1.mp3"
            media2 = root / "episode2.mp3"
            media1.write_text("audio", encoding="utf-8")
            media2.write_text("audio", encoding="utf-8")

            init_database(str(db_path))
            for uid, media in (("uid-a", media1), ("uid-b", media2)):
                upsert_download(
                    str(db_path),
                    {
                        "source_type": "podcast",
                        "source_name": "TestPodcast",
                        "item_uid": uid,
                        "item_url": f"https://cdn.example.com/{uid}.mp3",
                        "media_url": f"https://cdn.example.com/{uid}.mp3",
                        "title": uid,
                        "file_path": str(media),
                        "file_ext": "mp3",
                        "file_size_bytes": media.stat().st_size,
                        "subtitle_enabled": True,
                        "download_status": "downloaded",
                        "raw_metadata": {"title": uid},
                    },
                )

            count = mark_all_downloads_played(str(db_path))
            self.assertEqual(count, 2)
            rows = fetch_downloaded_media_rows(db_path)
            self.assertTrue(all(row.played for row in rows))


class WebAppRenderVisibilityTests(unittest.TestCase):
    def test_index_hides_played_items_from_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            row_new = SimpleNamespace(
                row_id=1,
                source_type="podcast",
                source_name="ShowA",
                title="New Item",
                file_path=str(root / "new.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=False,
            )
            row_played = SimpleNamespace(
                row_id=2,
                source_type="podcast",
                source_name="ShowB",
                title="Played Item",
                file_path=str(root / "played.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=True,
            )
            (root / "new.mp3").write_text("x", encoding="utf-8")
            (root / "played.mp3").write_text("x", encoding="utf-8")

            body = _render_index(
                rows=[row_new, row_played],
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

            self.assertIn("New Item", body)
            self.assertNotIn("Played Item", body)

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
