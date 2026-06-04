import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
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
    _is_playback_completion_reason,
    _parse_range_header,
    _resolve_safe_subtitle_path,
    _render_index,
    _render_settings,
    _render_player,
    _srt_to_vtt,
    _resolve_safe_media_path,
    _delete_downloaded_artifacts_for_row,
    _mark_download_played_and_delete_artifacts,
    _run_android_delete_job,
    _infer_media_type_for_redownload,
    fetch_downloaded_media_row_by_id,
    _stream_media,
    _enqueue_progress_update,
    _flush_pending_progress_updates,
    _descriptor_cleanup_loop,
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

    def test_completion_progress_reset_is_sticky_until_flush(self):
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
                    "item_uid": "uid-queue-2",
                    "item_url": "https://example.com/episode2.mp3",
                    "title": "Completion Reset Episode",
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
            _enqueue_progress_update(state, row.row_id, 120.0, reason="timeupdate")
            _enqueue_progress_update(state, row.row_id, 999.0, reason="ended", forced=True)
            _enqueue_progress_update(state, row.row_id, 119.0, reason="pause", forced=True)

            updated_count = _flush_pending_progress_updates(state)
            self.assertEqual(updated_count, 1)
            self.assertAlmostEqual(get_download_position_seconds(str(db_path), row.row_id), 0.0, places=3)

    def test_is_playback_completion_reason(self):
        self.assertTrue(_is_playback_completion_reason("ended"))
        self.assertTrue(_is_playback_completion_reason("mini-ended"))
        self.assertFalse(_is_playback_completion_reason("pause"))

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

    def test_fetch_downloaded_media_rows_returns_empty_when_db_unavailable(self):
        with mock.patch(
            "webapp.init_database", side_effect=sqlite3.OperationalError("unable to open database file")
        ), mock.patch("webapp.log.warning") as warning_mock:
            rows = fetch_downloaded_media_rows(Path("/tmp/test.sqlite3"))

        self.assertEqual(rows, [])
        warning_mock.assert_called_once()

    def test_fetch_downloaded_media_rows_uses_output_root_fallback_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fallback_db = root / "downloads.sqlite3"
            init_database(str(fallback_db))

            missing_db = root / "missing" / "downloads.sqlite3"
            rows = fetch_downloaded_media_rows(missing_db, root)

            self.assertEqual(rows, [])
            self.assertTrue(fallback_db.is_file())

    def test_fetch_downloaded_media_rows_handles_resolve_emfile_in_fallback(self):
        with mock.patch(
            "webapp.init_database", side_effect=sqlite3.OperationalError("unable to open database file")
        ), mock.patch("webapp.Path.resolve", side_effect=OSError(24, "Too many open files")):
            rows = fetch_downloaded_media_rows(Path("/tmp/missing/downloads.sqlite3"), Path("/tmp"))

        self.assertEqual(rows, [])

    def test_fetch_downloaded_media_row_by_id_returns_none_when_db_unavailable(self):
        with mock.patch(
            "webapp.init_database", side_effect=sqlite3.OperationalError("unable to open database file")
        ), mock.patch("webapp.log.warning") as warning_mock:
            row = fetch_downloaded_media_row_by_id(Path("/tmp/test.sqlite3"), 1)

        self.assertIsNone(row)
        warning_mock.assert_called_once()
        self.assertIn("db=/tmp/test.sqlite3", warning_mock.call_args.args[3])

    def test_render_index_handles_unavailable_database_stats(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "webapp.init_database", side_effect=sqlite3.OperationalError("unable to open database file")
        ):
            body = _render_index(
                rows=[],
                output_root=Path(tmpdir),
                database_path=Path(tmpdir) / "downloads.sqlite3",
                status={"is_running": "no", "last_result": "idle", "last_finished": "Never", "last_error": "", "cookie_present": "no"},
            )

        self.assertIn("GetOffline Media Library", body)

    def test_render_index_includes_mini_player_listener_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            body = _render_index(
                rows=[],
                output_root=Path(tmpdir),
                database_path=Path(tmpdir) / "downloads.sqlite3",
                status={"is_running": "no", "last_result": "idle", "last_finished": "Never", "last_error": "", "cookie_present": "no"},
            )

        self.assertIn("function detachMiniHandlers(el)", body)
        self.assertIn("existing.textTrack.removeEventListener('cuechange', existing.cuechange)", body)
        self.assertIn("player._miniPersistentHandlers.subtitleLoad = onSubtitleLoad", body)

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

    def test_descriptor_cleanup_loop_disposes_cached_descriptors(self):
        stop_event = threading.Event()
        state = AppState(
            output_root=Path("/tmp"),
            database_path=Path("/tmp/downloads.sqlite3"),
            config={"defaults": {}},
            update_runner=lambda config, items: None,
        )

        wait_mock = mock.Mock(side_effect=[False, True])
        with mock.patch.object(stop_event, "wait", wait_mock), mock.patch(
            "webapp.close_cached_descriptors", return_value=2
        ) as cleanup_mock, mock.patch("webapp.log.info") as info_mock:
            _descriptor_cleanup_loop(state, stop_event)

        cleanup_mock.assert_called_once()
        info_mock.assert_called_once()

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
            self.assertIn("fetch('/update-status', { cache: 'no-store' })", body)
            self.assertIn("const isMediaPlaybackActive = () => {", body)
            self.assertIn('id="summary-grid"', body)
            self.assertIn('id="summary-visible-items"', body)
            self.assertIn('id="summary-played-items"', body)
            self.assertIn('id="summary-new-items"', body)
            self.assertIn('id="summary-favorite-items"', body)
            self.assertIn('id="downloads-table-body"', body)
            self.assertIn("setSyncButtonRunning", body)
            self.assertIn("refreshLibraryViewWithoutReload", body)
            self.assertIn('id="library-filter"', body)
            self.assertIn('id="library-filter-mode"', body)
            self.assertIn('id="library-filter-clear"', body)
            self.assertIn('<th class="channel-col">Channel</th>', body)
            self.assertIn('<th class="episode-col">Episode</th>', body)
            self.assertIn('name="batch_action"', body)
            self.assertIn('Choose action</option>', body)
            self.assertIn('>played</option>', body)
            self.assertIn('>unplayed</option>', body)
            self.assertIn('>favorite</option>', body)
            self.assertIn('>unfavorite</option>', body)
            self.assertIn('>delete</option>', body)
            self.assertIn('>download</option>', body)
            self.assertIn('<th><input type="checkbox" id="select-all-rows" class="row-selector select-all-selector" aria-label="Select all rows" /></th>', body)
            self.assertIn("/settings", body)
            self.assertIn("/quick-add-youtube", body)
            self.assertIn('id="quick-add-form"', body)
            self.assertIn("fetch('/quick-add-youtube', {", body)
            self.assertIn("setSyncButtonRunning();", body)
            self.assertIn("Add single YouTube link", body)
            self.assertIn('id="quick-add-open"', body)
            self.assertIn('id="quick-add-backdrop"', body)
            self.assertIn('id="quick-add-url"', body)
            self.assertIn('id="quick-add-search"', body)
            self.assertIn('id="quick-add-results"', body)
            self.assertIn("fetch('/youtube-search?q=' + encodeURIComponent(q)", body)
            self.assertIn('id="mini-player-backdrop"', body)
            self.assertIn('id="mini-player-transcript"', body)
            self.assertIn('aria-label="Maximize player"', body)
            self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr));", body)
            self.assertIn("const selectAllRows = document.getElementById('select-all-rows');", body)
            self.assertIn('selectAllRows.indeterminate = selectedCount > 0 && selectedCount < rowSelectors.length;', body)
            self.assertIn("batchForm.addEventListener('submit'", body)
            self.assertIn('if (!batchAction || !batchAction.value || selectedRows.length === 0)', body)
            self.assertIn("fetch('/batch-update', {", body)
            self.assertIn("'X-Requested-With': 'fetch'", body)
            self.assertIn("let rowSelectors = [];", body)
            self.assertIn("rowSelectors = Array.from(document.querySelectorAll('.row-selector[name=\"ids\"]'));", body)
            self.assertIn("bindBatchControls();", body)
            self.assertIn("bindPlayLinks();", body)
            self.assertIn("const getFilterMode = () => String((libraryFilterMode && libraryFilterMode.value) || 'unplayed');", body)
            self.assertIn("mode === 'unplayed' && row.dataset.played !== '1' && row.dataset.fileExists === '1'", body)
            self.assertIn("libraryFilterClear.addEventListener('click'", body)
            self.assertIn("applyBatchActionLocally(requestedAction, selectedRows);", body)
            self.assertIn("scheduleDeferredLibraryRefresh();", body)
            self.assertIn("const mediaSettingsStorageKey = 'getofflineMediaElementSettings';", body)
            self.assertIn('persistedMiniPlayerState.paused === false', body)




    def test_quick_add_modal_defaults_to_video(self):
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

            self.assertIn('<option value="video" selected>video</option>', body)

    def test_infer_media_type_for_redownload_uses_file_path_suffix(self):
        row = SimpleNamespace(file_ext=None, file_path="/tmp/video_episode.webm")
        self.assertEqual(_infer_media_type_for_redownload(row), "video")

    def test_index_rows_render_checkboxes_for_batch_updates(self):
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
                    "source_name": "BatchTest",
                    "item_uid": "uid-batch-1",
                    "item_url": "https://example.com/batch.mp3",
                    "title": "Batch Episode",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "download_status": "downloaded",
                },
            )

            rows = fetch_downloaded_media_rows(db_path)
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

            self.assertIn('action="/batch-update"', body)
            self.assertIn('class="row-selector" name="ids"', body)
            self.assertIn('class="episode-link" href="/play?id=', body)
            self.assertIn('data-row-id="', body)
            self.assertIn('data-played="0"', body)
            self.assertIn('data-favorite="0"', body)
            self.assertIn('data-has-subtitles="0"', body)

    def test_index_marks_audio_row_with_sibling_subtitle_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "downloads.sqlite3"
            media = root / "episode.mp3"
            subtitle = root / "episode.srt"
            media.write_text("audio", encoding="utf-8")
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")

            init_database(str(db_path))
            upsert_download(
                str(db_path),
                {
                    "source_type": "podcast",
                    "source_name": "SubtitleTest",
                    "item_uid": "uid-subtitle-1",
                    "item_url": "https://example.com/episode.mp3",
                    "title": "Subtitle Episode",
                    "file_path": str(media),
                    "file_ext": "mp3",
                    "file_size_bytes": media.stat().st_size,
                    "download_status": "downloaded",
                },
            )

            rows = fetch_downloaded_media_rows(db_path)
            self.assertIsNone(rows[0].subtitle_path)

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

            self.assertIn('data-has-subtitles="1"', body)

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
            self.assertTrue(cfg["youtube"][0]["subtitles"])
            self.assertFalse(cfg["youtube"][0]["redownload"])


    def test_trigger_single_youtube_download_marks_forced_redownload_entry(self):
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
                    media_type="video",
                    force_redownload=True,
                )
                self.assertTrue(started)

                deadline = time.time() + 2
                while time.time() < deadline:
                    with state.update_status.lock:
                        if not state.update_status.is_running and state.update_status.last_result == "ok":
                            break
                    time.sleep(0.05)

            cfg = captured["config"]
            self.assertEqual(cfg["youtube"][0]["type"], "video")
            self.assertFalse(cfg["youtube"][0]["subtitles"])
            self.assertTrue(cfg["youtube"][0]["redownload"])

    def test_trigger_single_youtube_download_can_force_subtitles_for_video(self):
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
                    media_type="video",
                    subtitles_enabled=True,
                )
                self.assertTrue(started)

                deadline = time.time() + 2
                while time.time() < deadline:
                    with state.update_status.lock:
                        if not state.update_status.is_running and state.update_status.last_result == "ok":
                            break
                    time.sleep(0.05)

            cfg = captured["config"]
            self.assertEqual(cfg["youtube"][0]["type"], "video")
            self.assertTrue(cfg["youtube"][0]["subtitles"])

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
        self.assertIn('name="source_action" value="edit"', body)
        self.assertIn('name="media_type"', body)
        self.assertIn('form="youtube-edit-0"', body)
        self.assertIn('>Save</button>', body)

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

    def test_delete_downloaded_artifacts_for_played_row_removes_media_and_sidecars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "episode.mp3"
            subtitle = root / "episode.srt"
            thumbnail = root / "episode.webp"
            outside_artwork = root.parent / "outside-art.jpg"
            media.write_text("audio", encoding="utf-8")
            subtitle.write_text("subtitle", encoding="utf-8")
            thumbnail.write_text("thumbnail", encoding="utf-8")
            outside_artwork.write_text("outside", encoding="utf-8")
            row = SimpleNamespace(
                row_id=10,
                file_path=str(media),
                subtitle_path=str(subtitle),
                raw_metadata_json=json.dumps({"artwork_path": str(thumbnail), "thumbnail_path": str(outside_artwork)}),
            )

            deleted = _delete_downloaded_artifacts_for_row(root, row)

            self.assertEqual(deleted, 3)
            self.assertFalse(media.exists())
            self.assertFalse(subtitle.exists())
            self.assertFalse(thumbnail.exists())
            self.assertTrue(outside_artwork.exists())

    def test_mark_download_played_from_webapp_deletes_local_media(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "downloads.sqlite3"
            media = root / "episode.mp4"
            thumbnail = root / "episode.jpg"
            media.write_text("video", encoding="utf-8")
            thumbnail.write_text("thumbnail", encoding="utf-8")

            init_database(str(db_path))
            upsert_download(
                str(db_path),
                {
                    "source_type": "youtube",
                    "source_name": "Channel",
                    "item_uid": "uid-play-delete",
                    "item_url": "https://youtube.com/watch?v=uid-play-delete",
                    "media_url": "https://youtube.com/watch?v=uid-play-delete",
                    "title": "Episode",
                    "file_path": str(media),
                    "file_ext": "mp4",
                    "file_size_bytes": media.stat().st_size,
                    "download_status": "downloaded",
                    "raw_metadata": {"artwork_path": str(thumbnail)},
                },
            )
            state = AppState(
                output_root=root,
                database_path=db_path,
                config={"defaults": {}},
                update_runner=lambda config, items: None,
            )
            row = fetch_downloaded_media_rows(db_path, root)[0]

            with mock.patch("webapp._trigger_android_delete_for_rows") as android_delete_mock:
                updated = _mark_download_played_and_delete_artifacts(state, row.row_id, played=True)

            self.assertTrue(updated)
            android_delete_mock.assert_called_once()
            self.assertFalse(media.exists())
            self.assertFalse(thumbnail.exists())
            updated_row = fetch_downloaded_media_row_by_id(db_path, row.row_id)
            self.assertIsNotNone(updated_row)
            self.assertTrue(updated_row.played)

    def test_run_android_delete_job_deletes_remote_played_media_even_when_auto_sync_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "episode.mp4"
            media.write_text("video", encoding="utf-8")
            state = AppState(
                output_root=root,
                database_path=root / "downloads.sqlite3",
                config={
                    "defaults": {
                        "android_sync_enabled": "0",
                        "android_sync_adb_path": "adb",
                        "android_sync_destination": "/sdcard/Movies/GetOffline",
                    }
                },
                update_runner=lambda config, items: None,
            )
            row = SimpleNamespace(
                row_id=99,
                file_path=str(media),
                title="Episode",
                source_name="Channel",
                source_type="youtube",
                subtitle_path=None,
                last_position_seconds=0.0,
            )

            with mock.patch("webapp.delete_items_from_android") as delete_mock:
                delete_mock.return_value = SimpleNamespace(message="deleted 1", copied=1, failed=0, device_serial="ABC123")
                _run_android_delete_job(state, [row])

            delete_mock.assert_called_once()
            items_arg, config_arg = delete_mock.call_args.args
            self.assertTrue(config_arg.enabled)
            self.assertEqual(config_arg.destination, "/sdcard/Movies/GetOffline")
            self.assertEqual(len(items_arg), 1)
            self.assertEqual(items_arg[0].row_id, 99)
            self.assertEqual(items_arg[0].title, "Episode")
            self.assertEqual(items_arg[0].source_name, "Channel")
            self.assertEqual(items_arg[0].file_path, media.resolve())

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
    def test_index_includes_played_items_in_markup_for_client_filtering(self):
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
            self.assertIn("Played Item", body)
            self.assertIn('option value="unplayed" selected', body)

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
            self.assertIn('id="library-filter-mode"', body)

    def test_index_marks_started_items_with_started_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            row_started = SimpleNamespace(
                row_id=1,
                source_type="podcast",
                source_name="ShowA",
                title="In Progress",
                file_path=str(root / "progress.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=False,
                favorite=False,
                last_position_seconds=12.5,
            )
            (root / "progress.mp3").write_text("x", encoding="utf-8")

            body = _render_index(
                rows=[row_started],
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

            self.assertIn('status-started" title="Playback started">STARTED</span>', body)
            self.assertIn('.status-started { background: #e2f3ff; color: #114e78; }', body)

    def test_index_favorites_view_shows_favorites_regardless_of_played_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            row_unplayed_favorite = SimpleNamespace(
                row_id=1,
                source_type="podcast",
                source_name="FavA",
                title="Favorite Unplayed",
                file_path=str(root / "fav-unplayed.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=False,
                favorite=True,
                last_position_seconds=0,
            )
            row_played_favorite = SimpleNamespace(
                row_id=2,
                source_type="podcast",
                source_name="FavB",
                title="Favorite Played",
                file_path=str(root / "fav-played.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=True,
                favorite=True,
                last_position_seconds=5,
            )
            row_non_favorite = SimpleNamespace(
                row_id=3,
                source_type="podcast",
                source_name="Other",
                title="Not Favorite",
                file_path=str(root / "other.mp3"),
                file_ext="mp3",
                file_size_bytes=100,
                upload_date=None,
                played=False,
                favorite=False,
                last_position_seconds=0,
            )
            (root / "fav-unplayed.mp3").write_text("x", encoding="utf-8")
            (root / "fav-played.mp3").write_text("x", encoding="utf-8")
            (root / "other.mp3").write_text("x", encoding="utf-8")

            body = _render_index(
                rows=[row_unplayed_favorite, row_played_favorite, row_non_favorite],
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
                favorites_only=True,
            )

            self.assertIn("Favorite Unplayed", body)
            self.assertIn("Favorite Played", body)
            self.assertNotIn("Not Favorite", body)

    def test_index_uses_batch_controls_and_play_link_for_item_actions(self):
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

            self.assertIn('class="episode-link" href="/play?id=1"', body)
            self.assertIn('class="row-selector" name="ids" value="1"', body)
            self.assertIn('name="batch_action"', body)
            self.assertIn('class="batch-toolbar-form"', body)
            self.assertIn('id="batch-apply"', body)
            self.assertIn('<th><input type="checkbox" id="select-all-rows" class="row-selector select-all-selector" aria-label="Select all rows" /></th>', body)
            self.assertIn('title="Sync downloads"', body)
            self.assertIn('href="#bi-download"', body)
            self.assertIn('id="library-filter-mode"', body)
            self.assertIn('option value="unplayed" selected', body)
            self.assertIn('option value="played"', body)
            self.assertIn('option value="favorites"', body)
            self.assertIn('option value="all"', body)
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

            self.assertNotIn('status-unplayed">UNPLAYED</span>', body)

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
            self.assertIn("const mediaSettingsStorageKey = 'getofflineMediaElementSettings';", body)
            self.assertIn("player.addEventListener('volumechange', persistMediaSettings);", body)
            self.assertIn("if (playbackCompleted) return;", body)
            self.assertIn("try { player.currentTime = 0; } catch (_) {}", body)
            self.assertIn("if (playbackCompleted) {", body)
            self.assertIn("postProgress(0, true, 'back-link');", body)

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
            self.assertIn("postMiniProgress(state, state.currentTime || 0, true, 'mini-open');", body)
            self.assertIn("postMiniProgress(state, active.currentTime || 0, true, 'mini-close');", body)
            self.assertIn("postMiniProgress(state, active.currentTime || 0, true, 'mini-pause');", body)
            self.assertIn("function updatePlayLinkResumeHint(rowId, seconds)", body)
            self.assertIn("function setMiniExpanded(expanded)", body)
            self.assertIn("miniOpen.textContent = isExpanded ? 'Minimize' : 'Maximize';", body)
            self.assertIn("miniPlayer.classList.contains('is-maximized')", body)
            self.assertIn("link.dataset.resumeSeconds = safe.toFixed(3);", body)

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


    def test_index_includes_missing_rows_in_markup_for_client_filtering(self):
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
            self.assertIn('>MISSING</span>', body)
            self.assertIn('/redownload?id=1', body)
            self.assertIn('name="ids" value="1"', body)

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


class AndroidSyncTests(unittest.TestCase):
    def test_android_sync_items_only_include_unplayed_existing_media(self):
        from webapp import _android_sync_items_from_rows

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            unplayed = root / "unplayed.mp4"
            played = root / "played.mp4"
            artwork = root / "unplayed.webp"
            unplayed.write_text("video", encoding="utf-8")
            played.write_text("video", encoding="utf-8")
            artwork.write_text("thumbnail", encoding="utf-8")
            rows = [
                SimpleNamespace(row_id=1, played=False, file_path=str(unplayed), title="Unplayed", source_name="Channel", source_type="youtube", subtitle_path=None, last_position_seconds=42.5, raw_metadata_json=json.dumps({"artwork_path": str(artwork), "artwork_url": "https://example.com/art.jpg"})),
                SimpleNamespace(row_id=2, played=True, file_path=str(played), title="Played", source_name="Channel", source_type="youtube", subtitle_path=None, last_position_seconds=3.0, raw_metadata_json=None),
                SimpleNamespace(row_id=3, played=False, file_path=str(root / "missing.mp4"), title="Missing", source_name="Channel", source_type="youtube", subtitle_path=None, last_position_seconds=9.0, raw_metadata_json=None),
            ]

            items = _android_sync_items_from_rows(rows, root, max_items=10)

            self.assertEqual([item.row_id for item in items], [1])
            self.assertEqual(items[0].file_path, unplayed.resolve())
            self.assertEqual(items[0].artwork_path, artwork.resolve())
            self.assertEqual(items[0].artwork_url, "https://example.com/art.jpg")
            self.assertAlmostEqual(items[0].position_seconds, 42.5)


    def test_android_sync_items_filters_statuses_and_exclusion_regex(self):
        from webapp import _android_sync_items_from_rows

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            unplayed = root / "unplayed.mp4"
            started = root / "started.mp4"
            played = root / "played.mp4"
            excluded = root / "bonus.mp4"
            for media in (unplayed, started, played, excluded):
                media.write_text("video", encoding="utf-8")
            rows = [
                SimpleNamespace(row_id=1, played=False, file_path=str(unplayed), title="Unplayed", source_name="Channel", source_type="youtube", item_url=None, subtitle_path=None, last_position_seconds=0.0, raw_metadata_json=None),
                SimpleNamespace(row_id=2, played=False, file_path=str(started), title="Started", source_name="Channel", source_type="youtube", item_url=None, subtitle_path=None, last_position_seconds=10.0, raw_metadata_json=None),
                SimpleNamespace(row_id=3, played=True, file_path=str(played), title="Played", source_name="Channel", source_type="youtube", item_url=None, subtitle_path=None, last_position_seconds=0.0, raw_metadata_json=None),
                SimpleNamespace(row_id=4, played=False, file_path=str(excluded), title="Bonus Clip", source_name="Channel", source_type="youtube", item_url=None, subtitle_path=None, last_position_seconds=0.0, raw_metadata_json=None),
            ]

            items = _android_sync_items_from_rows(
                rows,
                root,
                max_items=10,
                include_unplayed=True,
                include_started=False,
                include_played=True,
                exclude_regex="bonus",
            )

            self.assertEqual([item.row_id for item in items], [1, 3])

    def test_sync_items_to_android_skips_paths_recorded_in_syncdb(self):
        from android_sync import AndroidSyncConfig, AndroidSyncItem, sync_items_to_android

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp4"
            media.write_text("video", encoding="utf-8")
            calls = []

            def fake_runner(cmd, **kwargs):
                calls.append(cmd)
                if cmd[-1] == "devices":
                    return SimpleNamespace(stdout="List of devices attached\nABC123\tdevice\n", stderr="", returncode=0)
                if any("cat '/sdcard/Movies/GetOffline/syncdb.txt'" in str(part) for part in cmd):
                    return SimpleNamespace(stdout="/sdcard/Movies/GetOffline/Channel - Episode.mp4\n", stderr="", returncode=0)
                return SimpleNamespace(stdout="ok", stderr="", returncode=0)

            with mock.patch("android_sync.shutil.which", return_value="/usr/bin/adb"):
                result = sync_items_to_android(
                    [AndroidSyncItem(row_id=1, title="Episode", source_name="Channel", file_path=media)],
                    AndroidSyncConfig(enabled=True, destination="/sdcard/Movies/GetOffline", max_items=10),
                    runner=fake_runner,
                )

            self.assertEqual(result.copied, 0)
            self.assertEqual(result.skipped, 1)
            self.assertFalse(any("push" in cmd and str(cmd[-1]).endswith("Episode.mp4") for cmd in calls))

    def test_sync_items_to_android_pushes_unplayed_file(self):
        from android_sync import AndroidSyncConfig, AndroidSyncItem, sync_items_to_android

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp4"
            media.write_text("video", encoding="utf-8")
            calls = []
            playlist_payloads = []

            def fake_runner(cmd, **kwargs):
                calls.append(cmd)
                if "-metadata" in cmd:
                    Path(cmd[-1]).write_text("tagged-media", encoding="utf-8")
                    return SimpleNamespace(stdout="", stderr="", returncode=0)
                if cmd[-1] == "devices":
                    return SimpleNamespace(stdout="List of devices attached\nABC123\tdevice\n", stderr="", returncode=0)
                if any("test -f" in str(part) for part in cmd):
                    return SimpleNamespace(stdout="", stderr="", returncode=1)
                if "push" in cmd and str(cmd[-1]).endswith("GetOffline.xspf"):
                    playlist_payloads.append(Path(cmd[-2]).read_text(encoding="utf-8"))
                return SimpleNamespace(stdout="ok", stderr="", returncode=0)

            with mock.patch("android_sync.shutil.which", return_value="/usr/bin/adb"):
                result = sync_items_to_android(
                    [AndroidSyncItem(row_id=1, title="Episode", source_name="Channel", file_path=media, position_seconds=97.25)],
                    AndroidSyncConfig(enabled=True, destination="/sdcard/Movies/GetOffline", max_items=10),
                    runner=fake_runner,
                )

            self.assertEqual(result.copied, 1)
            self.assertEqual(result.device_serial, "ABC123")
            self.assertTrue(any(cmd[:4] == ["/usr/bin/adb", "-s", "ABC123", "push"] for cmd in calls))
            metadata_cmds = []
            media_pushes = []
            for cmd in calls:
                if "-metadata" in cmd:
                    metadata_cmds.append(cmd)
                if "push" in cmd and str(cmd[-1]).endswith("Episode.mp4"):
                    media_pushes.append(cmd)
            self.assertEqual(len(metadata_cmds), 1)
            self.assertIn("title=Episode", metadata_cmds[0])
            self.assertIn("artist=Channel", metadata_cmds[0])
            self.assertIn("album_artist=Channel", metadata_cmds[0])
            self.assertIn("-metadata:s:a:0", metadata_cmds[0])
            self.assertIn("comment=GetOffline row_id=1 position_seconds=97.250", metadata_cmds[0])
            self.assertTrue(any("MEDIA_SCANNER_SCAN_FILE" in str(part) for cmd in calls for part in cmd))
            self.assertEqual(len(media_pushes), 1)
            self.assertNotEqual(Path(media_pushes[0][-2]), media)
            self.assertIn(["/usr/bin/adb", "-s", "ABC123", "shell", "mkdir -p '/sdcard/Movies/GetOffline'"], calls)
            self.assertFalse(any(cmd[3:6] == ["shell", "sh", "-c"] for cmd in calls))
            self.assertEqual(result.vlc_playlist_path, "/sdcard/Movies/GetOffline/GetOffline.xspf")
            self.assertEqual(len(playlist_payloads), 1)
            self.assertIn("<title>Episode</title>", playlist_payloads[0])
            self.assertIn("<creator>Channel</creator>", playlist_payloads[0])
            self.assertIn("<vlc:option>start-time=97</vlc:option>", playlist_payloads[0])
            self.assertIn("position_seconds=97.250", playlist_payloads[0])

    def test_sync_items_to_android_embeds_album_art_for_podcast_audio(self):
        from android_sync import AndroidSyncConfig, AndroidSyncItem, sync_items_to_android

        class FakeArtworkResponse:
            headers = {"Content-Type": "image/jpeg"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size):
                return b"fake-jpeg"

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp3"
            media.write_text("audio", encoding="utf-8")
            calls = []

            def fake_runner(cmd, **kwargs):
                calls.append(cmd)
                if "-metadata" in cmd:
                    Path(cmd[-1]).write_text("tagged-audio", encoding="utf-8")
                    return SimpleNamespace(stdout="", stderr="", returncode=0)
                if cmd[-1] == "devices":
                    return SimpleNamespace(stdout="List of devices attached\nABC123\tdevice\n", stderr="", returncode=0)
                if any("test -f" in str(part) for part in cmd):
                    return SimpleNamespace(stdout="", stderr="", returncode=1)
                return SimpleNamespace(stdout="ok", stderr="", returncode=0)

            def fake_which(name):
                return "/usr/bin/ffmpeg" if name == "ffmpeg" else "/usr/bin/adb"

            with mock.patch("android_sync.shutil.which", side_effect=fake_which), mock.patch(
                "android_sync.urllib.request.urlopen", return_value=FakeArtworkResponse()
            ) as urlopen_mock:
                result = sync_items_to_android(
                    [
                        AndroidSyncItem(
                            row_id=7,
                            title="Podcast Episode",
                            source_name="Podcast Show",
                            file_path=media,
                            position_seconds=12.5,
                            artwork_url="https://example.com/art.jpg",
                        )
                    ],
                    AndroidSyncConfig(enabled=True, destination="/sdcard/Movies/GetOffline", max_items=10),
                    runner=fake_runner,
                )

            metadata_cmds = []
            for cmd in calls:
                if "-metadata" in cmd:
                    metadata_cmds.append(cmd)
            self.assertEqual(result.copied, 1)
            self.assertEqual(len(metadata_cmds), 1)
            self.assertIn("artist=Podcast Show", metadata_cmds[0])
            self.assertIn("album=Podcast Show", metadata_cmds[0])
            self.assertIn("genre=Podcast", metadata_cmds[0])
            self.assertIn("-metadata:s:a:0", metadata_cmds[0])
            self.assertIn("-disposition:v:0", metadata_cmds[0])
            self.assertIn("attached_pic", metadata_cmds[0])
            self.assertIn("-c:v", metadata_cmds[0])
            self.assertIn("mjpeg", metadata_cmds[0])
            self.assertIn("-id3v2_version", metadata_cmds[0])
            urlopen_mock.assert_called_once_with("https://example.com/art.jpg", timeout=20)

    def test_sync_items_to_android_uses_downloaded_thumbnail_sidecar_for_album_art(self):
        from android_sync import AndroidSyncConfig, AndroidSyncItem, sync_items_to_android

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp3"
            artwork = Path(tmpdir) / "episode.webp"
            media.write_text("audio", encoding="utf-8")
            artwork.write_text("thumbnail", encoding="utf-8")
            calls = []

            def fake_runner(cmd, **kwargs):
                calls.append(cmd)
                if "-metadata" in cmd:
                    Path(cmd[-1]).write_text("tagged-audio", encoding="utf-8")
                    return SimpleNamespace(stdout="", stderr="", returncode=0)
                if cmd[-1] == "devices":
                    return SimpleNamespace(stdout="List of devices attached\nABC123\tdevice\n", stderr="", returncode=0)
                if any("test -f" in str(part) for part in cmd):
                    return SimpleNamespace(stdout="", stderr="", returncode=1)
                return SimpleNamespace(stdout="ok", stderr="", returncode=0)

            def fake_which(name):
                return "/usr/bin/ffmpeg" if name == "ffmpeg" else "/usr/bin/adb"

            with mock.patch("android_sync.shutil.which", side_effect=fake_which), mock.patch("android_sync.urllib.request.urlopen") as urlopen_mock:
                result = sync_items_to_android(
                    [AndroidSyncItem(row_id=8, title="Episode", source_name="Channel", file_path=media, artwork_path=artwork)],
                    AndroidSyncConfig(enabled=True, destination="/sdcard/Movies/GetOffline", max_items=10),
                    runner=fake_runner,
                )

            metadata_cmds = []
            for cmd in calls:
                if "-metadata" in cmd:
                    metadata_cmds.append(cmd)
            self.assertEqual(result.copied, 1)
            self.assertEqual(len(metadata_cmds), 1)
            self.assertIn(str(artwork.resolve()), metadata_cmds[0])
            self.assertIn("mjpeg", metadata_cmds[0])
            urlopen_mock.assert_not_called()

    def test_sync_items_to_android_refreshes_existing_remote_file_when_metadata_available(self):
        from android_sync import AndroidSyncConfig, AndroidSyncItem, sync_items_to_android

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp3"
            media.write_text("audio", encoding="utf-8")
            calls = []

            def fake_runner(cmd, **kwargs):
                calls.append(cmd)
                if "-metadata" in cmd:
                    Path(cmd[-1]).write_text("tagged-audio", encoding="utf-8")
                    return SimpleNamespace(stdout="", stderr="", returncode=0)
                if cmd[-1] == "devices":
                    return SimpleNamespace(stdout="List of devices attached\nABC123\tdevice\n", stderr="", returncode=0)
                if any("test -f" in str(part) for part in cmd):
                    return SimpleNamespace(stdout="", stderr="", returncode=0)
                return SimpleNamespace(stdout="ok", stderr="", returncode=0)

            def fake_which(name):
                return "/usr/bin/ffmpeg" if name == "ffmpeg" else "/usr/bin/adb"

            with mock.patch("android_sync.shutil.which", side_effect=fake_which):
                result = sync_items_to_android(
                    [AndroidSyncItem(row_id=9, title="Existing Episode", source_name="Podcast Show", file_path=media)],
                    AndroidSyncConfig(enabled=True, destination="/sdcard/Movies/GetOffline", max_items=10),
                    runner=fake_runner,
                )

            metadata_cmds = []
            media_pushes = []
            for cmd in calls:
                if "-metadata" in cmd:
                    metadata_cmds.append(cmd)
                if "push" in cmd and str(cmd[-1]).endswith("Existing Episode.mp3"):
                    media_pushes.append(cmd)
            self.assertEqual(result.copied, 1)
            self.assertEqual(result.skipped, 0)
            self.assertEqual(len(metadata_cmds), 1)
            self.assertEqual(len(media_pushes), 1)
            self.assertNotEqual(Path(media_pushes[0][-2]), media)

    def test_delete_items_from_android_removes_remote_media_and_subtitles(self):
        from android_sync import AndroidSyncConfig, AndroidSyncItem, delete_items_from_android

        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            if cmd[-1] == "devices":
                return SimpleNamespace(stdout="List of devices attached\nABC123\tdevice\n", stderr="", returncode=0)
            return SimpleNamespace(stdout="ok", stderr="", returncode=0)

        with mock.patch("android_sync.shutil.which", return_value="/usr/bin/adb"):
            result = delete_items_from_android(
                [AndroidSyncItem(row_id=5, title="Episode", source_name="Channel", file_path=Path("episode.mp4"))],
                AndroidSyncConfig(enabled=True, destination="/sdcard/Movies/GetOffline", max_items=10),
                runner=fake_runner,
            )

        rm_commands = [cmd for cmd in calls if len(cmd) >= 5 and cmd[3] == "shell" and str(cmd[4]).startswith("rm -f")]
        self.assertEqual(result.copied, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(len(rm_commands), 1)
        self.assertIn("Channel - Episode.mp4", rm_commands[0][4])
        self.assertIn("Channel - Episode.srt", rm_commands[0][4])
        self.assertIn("Channel - Episode.vtt", rm_commands[0][4])

    def test_sync_items_to_android_reports_mkdir_failure(self):
        from android_sync import AndroidSyncConfig, AndroidSyncItem, sync_items_to_android

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp4"
            media.write_text("video", encoding="utf-8")

            def fake_runner(cmd, **kwargs):
                if cmd[-1] == "devices":
                    return SimpleNamespace(stdout="List of devices attached\nABC123\tdevice\n", stderr="", returncode=0)
                if any("mkdir -p" in str(part) for part in cmd):
                    return SimpleNamespace(stdout="", stderr="permission denied", returncode=1)
                return SimpleNamespace(stdout="ok", stderr="", returncode=0)

            with mock.patch("android_sync.shutil.which", return_value="/usr/bin/adb"), mock.patch("android_sync.log.warning") as warning_mock:
                result = sync_items_to_android(
                    [AndroidSyncItem(row_id=1, title="Episode", source_name="Channel", file_path=media)],
                    AndroidSyncConfig(enabled=True, destination="/sdcard/Movies/GetOffline", max_items=10),
                    runner=fake_runner,
                )

            self.assertEqual(result.copied, 0)
            self.assertEqual(result.failed, 1)
            self.assertIn("unable to prepare Android folder", result.message)
            self.assertTrue(result.errors)
            warning_mock.assert_called()

    def test_sync_items_to_android_handles_push_timeout(self):
        from android_sync import AndroidSyncConfig, AndroidSyncItem, sync_items_to_android

        with tempfile.TemporaryDirectory() as tmpdir:
            media = Path(tmpdir) / "episode.mp4"
            media.write_text("video", encoding="utf-8")

            def fake_runner(cmd, **kwargs):
                if cmd[-1] == "devices":
                    return SimpleNamespace(stdout="List of devices attached\nABC123\tdevice\n", stderr="", returncode=0)
                if any("mkdir -p" in str(part) for part in cmd):
                    return SimpleNamespace(stdout="", stderr="", returncode=0)
                if any("test -f" in str(part) for part in cmd):
                    return SimpleNamespace(stdout="", stderr="", returncode=1)
                if "push" in cmd:
                    raise subprocess.TimeoutExpired(cmd, timeout=300)
                return SimpleNamespace(stdout="ok", stderr="", returncode=0)

            with mock.patch("android_sync.shutil.which", return_value="/usr/bin/adb"):
                result = sync_items_to_android(
                    [AndroidSyncItem(row_id=1, title="Episode", source_name="Channel", file_path=media)],
                    AndroidSyncConfig(enabled=True, destination="/sdcard/Movies/GetOffline", max_items=10),
                    runner=fake_runner,
                )

            self.assertEqual(result.copied, 0)
            self.assertEqual(result.failed, 1)
            self.assertIn("timed out", result.errors[0])



if __name__ == "__main__":
    unittest.main()
