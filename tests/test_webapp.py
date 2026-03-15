import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import init_database, mark_all_downloads_played, mark_download_favorite, mark_download_played, upsert_download  # noqa: E402
from webapp import (  # noqa: E402
    AppState,
    _LAST_DISCONNECT_LOGGED_AT,
    _log_stream_disconnect,
    _parse_range_header,
    _resolve_safe_subtitle_path,
    _render_index,
    _render_settings,
    _render_player,
    _srt_to_vtt,
    _resolve_safe_media_path,
    fetch_downloaded_media_row_by_id,
    _stream_media,
    _enqueue_progress_update,
    _flush_pending_progress_updates,
    fetch_downloaded_media_rows,
    get_download_position_seconds,
    get_total_listened_seconds,
    trigger_background_update,
    update_download_position_seconds,
    trigger_single_youtube_download,
)


class WebAppHelpersTests(unittest.TestCase):
    def setUp(self):
        _LAST_DISCONNECT_LOGGED_AT.clear()

    def test_flush_pending_progress_updates_batches_in_memory(self):
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
                    "source_name": "QueueTest",
                    "item_uid": "uid-queue-1",
                    "item_url": "https://example.com/episode.mp3",
                    "title": "Queued Progress Episode",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "download_status": "downloaded",
                },
            )

            row = fetch_downloaded_media_rows(db_path)[0]
            state = AppState(
                output_root=root,
                database_path=db_path,
                config={"defaults": {"output_root": str(root), "database_path": str(db_path)}},
                update_runner=lambda config, items: None,
            )
            _enqueue_progress_update(state, row.row_id, 5.0)
            _enqueue_progress_update(state, row.row_id, 9.5)

            updated_count = _flush_pending_progress_updates(state)
            self.assertEqual(updated_count, 1)
            self.assertAlmostEqual(get_download_position_seconds(str(db_path), row.row_id), 9.5, places=3)

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

    def test_stream_media_without_range_is_chunked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "big.mp3"
            media.write_bytes(b"a" * (700 * 1024))

            write_mock = mock.Mock()
            handler = SimpleNamespace(
                headers={},
                send_response=mock.Mock(),
                send_header=mock.Mock(),
                end_headers=mock.Mock(),
                wfile=SimpleNamespace(write=write_mock),
            )

            _stream_media(handler, media)

            self.assertGreater(write_mock.call_count, 1)

    def test_stream_media_ignores_client_disconnect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp3"
            media.write_bytes(b"a" * 4096)

            handler = SimpleNamespace(
                headers={"Range": "bytes=0-1023"},
                send_response=mock.Mock(),
                send_header=mock.Mock(),
                end_headers=mock.Mock(),
                wfile=SimpleNamespace(write=mock.Mock(side_effect=ConnectionResetError("peer reset"))),
            )

            _stream_media(handler, media)


    def test_fetch_downloaded_media_row_by_id_returns_none_when_locked(self):
        with mock.patch("webapp.init_database"), mock.patch(
            "webapp.sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")
        ), mock.patch("webapp.log.warning") as warning_mock:
            row = fetch_downloaded_media_row_by_id(Path("/tmp/test.sqlite3"), 1)

        self.assertIsNone(row)
        warning_mock.assert_called_once()

    def test_fetch_downloaded_media_rows_returns_empty_when_locked(self):
        with mock.patch("webapp.init_database"), mock.patch(
            "webapp.sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")
        ), mock.patch("webapp.log.warning") as warning_mock:
            rows = fetch_downloaded_media_rows(Path("/tmp/test.sqlite3"))

        self.assertEqual(rows, [])
        warning_mock.assert_called()

    def test_fetch_downloaded_media_row_by_id_returns_single_row(self):
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
                    "source_name": "SingleRowTest",
                    "item_uid": "uid-single-row",
                    "item_url": "https://example.com/episode.mp3",
                    "title": "Single Row Episode",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "download_status": "downloaded",
                },
            )

            rows = fetch_downloaded_media_rows(db_path)
            row = fetch_downloaded_media_row_by_id(db_path, rows[0].row_id)
            self.assertIsNotNone(row)
            self.assertEqual(row.row_id, rows[0].row_id)
            self.assertEqual(row.title, "Single Row Episode")

    def test_stream_disconnect_logging_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp3"
            media.write_bytes(b"a")

            with mock.patch("webapp.log.info") as info_mock:
                with mock.patch("webapp.time.monotonic", side_effect=[100.0, 101.0, 132.0]):
                    _log_stream_disconnect(media)
                    _log_stream_disconnect(media)
                    _log_stream_disconnect(media)

            self.assertEqual(info_mock.call_count, 2)

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
            self.assertIn('aria-label="Sync downloads"', body)
            self.assertIn("action=\"/update\"", body)
            self.assertIn("event.preventDefault();", body)
            self.assertIn("fetch('/update', { method: 'POST', keepalive: true })", body)
            self.assertIn("Mark all as played", body)
            self.assertIn("Show played", body)
            self.assertIn("Show favorites", body)
            self.assertIn('<th class="channel-col">Channel</th>', body)
            self.assertIn('<th class="episode-col">Episode</th>', body)
            self.assertIn('<th aria-label="Actions"></th>', body)
            self.assertIn("/settings", body)
            self.assertIn("/quick-add-youtube", body)
            self.assertIn("Add single YouTube link", body)
            self.assertIn('id="quick-add-open"', body)
            self.assertIn('id="quick-add-backdrop"', body)
            self.assertIn('id="quick-add-url"', body)
            self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr));", body)
            self.assertIn('persistedMiniPlayerState.paused === false', body)


    def test_index_play_link_includes_resume_seconds(self):
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
                    "item_uid": "uid-resume-1",
                    "item_url": "https://cdn.example.com/episode.mp3",
                    "media_url": "https://cdn.example.com/episode.mp3",
                    "title": "Episode Resume",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "subtitle_enabled": True,
                    "download_status": "downloaded",
                    "raw_metadata": {"title": "Episode Resume"},
                },
            )

            rows = fetch_downloaded_media_rows(db_path)
            update_download_position_seconds(str(db_path), rows[0].row_id, 97.25)

            body = _render_index(
                rows=rows,
                output_root=root,
                database_path=db_path,
                status={
                    "is_running": "no",
                    "last_started_at": "never",
                    "last_finished_at": "never",
                    "last_result": "idle",
                    "last_error": "none",
                    "last_items_count": "0",
                },
            )
            self.assertIn('data-resume-seconds="97.250"', body)

    def test_trigger_single_youtube_download_uses_single_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "downloads.sqlite3"
            init_database(str(db_path))
            state = AppState(
                output_root=root,
                database_path=db_path,
                config={"defaults": {"output_root": str(root), "database_path": str(db_path)}},
                update_runner=lambda config, items: None,
            )

            captured = {}

            def _fake_download(config, downloaded_items):
                captured["config"] = config
                downloaded_items.append("one")

            with mock.patch("youtube.resolve_youtube_source_name", return_value="MyChannel"), mock.patch(
                "youtube.download_youtube_items", side_effect=_fake_download
            ):
                started = trigger_single_youtube_download(
                    state,
                    url="https://www.youtube.com/watch?v=abc123",
                    media_type="audio",
                )
                self.assertTrue(started)

                deadline = time.time() + 2
                while time.time() < deadline:
                    with state.update_status.lock:
                        if not state.update_status.is_running and state.update_status.last_result == "ok":
                            break
                    time.sleep(0.05)

            cfg = captured["config"]
            self.assertEqual(cfg["podcasts"], [])
            self.assertEqual(len(cfg["youtube"]), 1)
            self.assertEqual(cfg["youtube"][0]["name"], "MyChannel")
            self.assertEqual(cfg["youtube"][0]["url"], "https://www.youtube.com/watch?v=abc123")
            self.assertEqual(cfg["youtube"][0]["type"], "audio")

    def test_render_settings_contains_cookie_field(self):
        body = _render_settings(
            {
                "defaults": {
                    "output_root": "/tmp/downloads",
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "max_downloads": 3,
                    "playlist_end": 3,
                    "processing_workers": 2,
                },
                "download_settings": {
                    "youtube_cookie_text": "# Netscape HTTP Cookie File\n.youtube.com\tTRUE",
                },
                "youtube": [{"name": "YT", "url": "https://youtube.com/@yt", "type": "audio", "subtitles": True}],
                "podcasts": [{"name": "Pod", "url": "https://example.com/rss", "subtitles": True}],
            }
        )
        self.assertIn("YouTube cookie text", body)
        self.assertIn("/settings", body)
        self.assertIn("youtube_cookie_text", body)
        self.assertIn("Add YouTube source", body)
        self.assertIn("Add podcast source", body)
        self.assertIn("Disable", body)

    def test_index_includes_listened_summary_panel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "downloads.sqlite3"
            init_database(str(db_path))
            body = _render_index(
                rows=[],
                output_root=root,
                database_path=db_path,
                status={
                    "is_running": "no",
                    "last_started_at": "never",
                    "last_finished_at": "never",
                    "last_result": "idle",
                    "last_error": "none",
                    "last_items_count": "0",
                },
            )
            self.assertIn("Listened", body)
            self.assertIn("0m", body)


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
            self.assertEqual(rows[0].item_url, "https://cdn.example.com/episode.mp3")
            self.assertEqual(Path(rows[0].file_path), media)
            self.assertIsNone(rows[0].subtitle_path)

    def test_fetch_downloaded_media_rows_repairs_normalized_file_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "downloads.sqlite3"
            stale_media_path = root / "20260312-They_re..FINALLY_Doing_It_-_BIG_Xbox_News.mp3"
            normalized_media_path = root / "20260312-They_re.FINALLY_Doing_It_-_BIG_Xbox_News.mp3"
            normalized_media_path.write_text("audio", encoding="utf-8")

            init_database(str(db_path))
            upsert_download(
                str(db_path),
                {
                    "source_type": "youtube",
                    "source_name": "XboxReady",
                    "item_uid": "uid-repair-1",
                    "item_url": "https://youtube.com/watch?v=uid-repair-1",
                    "media_url": "https://youtube.com/watch?v=uid-repair-1",
                    "title": "Repair test",
                    "file_path": str(stale_media_path),
                    "file_ext": "mp3",
                    "file_size_bytes": normalized_media_path.stat().st_size,
                    "subtitle_enabled": True,
                    "download_status": "downloaded",
                    "raw_metadata": {"title": "Repair test"},
                },
            )

            rows = fetch_downloaded_media_rows(db_path, root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(Path(rows[0].file_path), normalized_media_path)



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

    def test_download_position_persists_for_resume(self):
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
                    "item_uid": "uid-pos",
                    "item_url": "https://cdn.example.com/episode-pos.mp3",
                    "media_url": "https://cdn.example.com/episode-pos.mp3",
                    "title": "Episode Position",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "subtitle_enabled": True,
                    "download_status": "downloaded",
                    "raw_metadata": {"title": "Episode Position"},
                },
            )

            row = fetch_downloaded_media_rows(db_path)[0]
            ok = update_download_position_seconds(str(db_path), row.row_id, 123.456)
            self.assertTrue(ok)
            self.assertAlmostEqual(get_download_position_seconds(str(db_path), row.row_id), 123.456, places=2)

    def test_total_listened_seconds_accumulates_positive_progress(self):
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
                    "item_uid": "uid-pos-total",
                    "item_url": "https://cdn.example.com/episode-pos-total.mp3",
                    "media_url": "https://cdn.example.com/episode-pos-total.mp3",
                    "title": "Episode Position Total",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "subtitle_enabled": True,
                    "download_status": "downloaded",
                    "raw_metadata": {"title": "Episode Position Total"},
                },
            )

            row = fetch_downloaded_media_rows(db_path)[0]
            self.assertTrue(update_download_position_seconds(str(db_path), row.row_id, 120.0))
            self.assertTrue(update_download_position_seconds(str(db_path), row.row_id, 90.0))
            self.assertTrue(update_download_position_seconds(str(db_path), row.row_id, 150.0))

            self.assertAlmostEqual(get_total_listened_seconds(str(db_path)), 180.0, places=2)


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
                favorite=False,
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
                favorite=False,
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

    def test_index_can_show_played_items_with_toggle(self):
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
                favorite=False,
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
                favorite=False,
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
                show_played=True,
            )

            self.assertIn("Played Item", body)
            self.assertIn("Hide played", body)

    def test_index_uses_icons_and_tooltips_for_item_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            row = SimpleNamespace(
                row_id=1,
                source_type="podcast",
                source_name="ShowA",
                title="New Item",
                file_path=str(root / "new.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=False,
                played_at=None,
            )
            (root / "new.mp3").write_text("x", encoding="utf-8")

            body = _render_index(
                rows=[row],
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

            self.assertIn('aria-label="Play this item"', body)
            self.assertIn('title="Play this item"', body)
            self.assertIn('href="#bi-play-fill"', body)
            self.assertIn('title="Mark played"', body)
            self.assertIn('aria-label="Mark played"', body)
            self.assertIn('href="#bi-check2-circle"', body)
            self.assertIn('title="Sync downloads"', body)
            self.assertIn('href="#bi-download"', body)
            self.assertIn('title="Mark all as played"', body)
            self.assertIn('aria-label="Mark all as played"', body)
            self.assertIn('title="Show played"', body)
            self.assertIn('aria-label="Show played"', body)
            self.assertIn('href="#bi-eye"', body)
            self.assertIn('title="Settings"', body)
            self.assertIn('aria-label="Settings"', body)
            self.assertIn('href="#bi-gear"', body)
            self.assertIn('id="mini-player"', body)
            self.assertIn('id="mini-player-audio"', body)
            self.assertIn('id="mini-player-video"', body)
            self.assertIn('id="mini-player-open"', body)
            self.assertIn('data-play-link="1"', body)

    def test_index_hides_new_label_for_previously_played_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            row = SimpleNamespace(
                row_id=1,
                source_type="podcast",
                source_name="ShowA",
                title="Item",
                file_path=str(root / "item.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=False,
                favorite=False,
                played_at="2026-01-01T00:00:00Z",
            )
            (root / "item.mp3").write_text("x", encoding="utf-8")

            body = _render_index(
                rows=[row],
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

            self.assertNotIn('status-new">new</span>', body)

    def test_player_page_includes_resume_progress_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "item.mp3"
            media.write_text("x", encoding="utf-8")
            row = SimpleNamespace(
                row_id=11,
                source_type="podcast",
                source_name="Show",
                title="Sample",
            )
            body = _render_player(row, media, 42.5, has_subtitles=False)
            self.assertIn("/progress", body)
            self.assertIn("startSeconds = 42.500000", body)
            self.assertIn("shouldAutoPlay", body)
            self.assertIn("get('autoplay') === '1'", body)
            self.assertIn("navigator.sendBeacon('/progress'", body)
            self.assertIn("let progressInFlight = false", body)
            self.assertIn("queuedProgressSeconds = safe", body)
            self.assertIn("const periodicProgressSeconds = 5.0", body)
            self.assertIn("/progress request failed", body)
            self.assertIn("body.set('reason'", body)
            self.assertIn("body.set('forced'", body)
            self.assertIn("reason === 'page-exit'", body)
            self.assertIn("reason === 'back-link'", body)
            self.assertIn("reason === 'pause'", body)

    def test_index_open_button_has_navigation_fallback_when_state_is_missing(self):
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
            self.assertIn("if (miniOpenNavigationPending) {", body)
            self.assertIn("miniOpen.setAttribute('aria-disabled', 'true');", body)
            self.assertIn("suppressSyncAutoReload = true;", body)
            self.assertIn("window.clearTimeout(syncReloadTimer);", body)
            self.assertIn("miniOpen.href = state.playUrl + (state.paused ? '' : '&autoplay=1');", body)
            self.assertIn("postMiniProgress(state, state.currentTime || 0, true, 'mini-open');", body)
            self.assertIn("postMiniProgress(state, active.currentTime || 0, true, 'mini-close');", body)
            self.assertIn("postMiniProgress(state, active.currentTime || 0, true, 'mini-pause');", body)
            self.assertIn("active.removeAttribute('src');", body)
            self.assertIn("while (active.firstChild) active.removeChild(active.firstChild);", body)

    def test_player_page_includes_transcript_for_audio_with_subtitles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "item.mp3"
            media.write_text("x", encoding="utf-8")
            row = SimpleNamespace(
                row_id=12,
                source_type="podcast",
                source_name="Show",
                title="Sample",
            )
            body = _render_player(row, media, 0, has_subtitles=True)
            self.assertIn("/subtitle?id=12", body)
            self.assertIn("Transcript", body)
            self.assertIn("Loading transcript…", body)
            self.assertIn("pageshow", body)


    def test_player_page_ignores_subtitles_for_video(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "item.mp4"
            media.write_text("x", encoding="utf-8")
            row = SimpleNamespace(
                row_id=13,
                source_type="youtube",
                source_name="Channel",
                title="Sample Video",
            )
            body = _render_player(row, media, 0, has_subtitles=True)
            self.assertNotIn('/subtitle?id=13', body)

    def test_srt_to_vtt_conversion(self):
        content = "1\n00:00:00,500 --> 00:00:02,000\nHello\n"
        converted = _srt_to_vtt(content)
        self.assertTrue(converted.startswith("WEBVTT"))
        self.assertIn("00:00:00.500 --> 00:00:02.000", converted)

    def test_resolve_safe_subtitle_path_prefers_row_subtitle_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "episode.mp3"
            media.write_text("audio", encoding="utf-8")
            subtitle = root / "episode.custom.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")

            row = SimpleNamespace(subtitle_path=str(subtitle))
            resolved = _resolve_safe_subtitle_path(root, row, media)
            self.assertEqual(resolved, subtitle.resolve())


    def test_index_shows_missing_file_with_redownload_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            row = SimpleNamespace(
                row_id=1,
                source_type="podcast",
                source_name="ShowA",
                title="Missing Item",
                file_path=str(root / "missing.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=False,
                favorite=False,
            )

            body = _render_index(
                rows=[row],
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
            self.assertIn('>missing</span>', body)
            self.assertIn('/redownload?id=1', body)
            self.assertIn('/delete-file?id=1', body)

    def test_mark_download_favorite_updates_row_state(self):
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
                    "item_uid": "uid-fav",
                    "item_url": "https://cdn.example.com/episode-fav.mp3",
                    "media_url": "https://cdn.example.com/episode-fav.mp3",
                    "title": "Episode Fav",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "subtitle_enabled": True,
                    "download_status": "downloaded",
                    "raw_metadata": {"title": "Episode Fav"},
                },
            )

            row = fetch_downloaded_media_rows(db_path)[0]
            self.assertTrue(mark_download_favorite(str(db_path), row.row_id, favorite=True))
            favorited = fetch_downloaded_media_rows(db_path)[0]
            self.assertTrue(favorited.favorite)

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
