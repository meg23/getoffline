import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workers.download_store import (  # noqa: E402
    _record_revision,
    apply_migrations,
    close_cached_descriptors,
    get_download_position_seconds,
    get_stored_config,
    init_database,
    resolve_download_artifact_path,
    resolve_database_path,
    update_download_position_seconds,
    replace_sources,
    seed_sources_from_config,
    update_download_settings,
    update_source_config,
    update_stored_defaults,
)


class DatabaseMigrationsTests(unittest.TestCase):
    def test_resolve_database_path_returns_absolute_path_for_default_location(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = resolve_database_path(
                {"output_root": os.path.join(tmpdir, "downloads")}
            )
            self.assertTrue(os.path.isabs(path))
            self.assertTrue(
                path.endswith(os.path.join("downloads", "downloads.sqlite3"))
            )

    def test_resolve_database_path_normalizes_configured_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            configured = os.path.join(tmpdir, "nested", "..", "db.sqlite3") + "\n"
            path = resolve_database_path(
                {"output_root": tmpdir, "database_path": configured}
            )
            self.assertTrue(os.path.isabs(path))
            self.assertEqual(path, os.path.abspath(os.path.join(tmpdir, "db.sqlite3")))

    def test_logs_when_sqlite_write_hits_lock(self):
        with mock.patch(
            "workers.download_store.sqlite3.connect",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with mock.patch("workers.download_store.log.warning") as warning_mock:
                with self.assertRaises(sqlite3.OperationalError):
                    _record_revision("/tmp/test-lock.sqlite3", "0001_create_downloads")

                warning_mock.assert_called_once()
                self.assertIn("recording schema revision", str(warning_mock.call_args))

    def test_does_not_log_non_lock_sqlite_write_errors(self):
        with mock.patch(
            "workers.download_store.sqlite3.connect",
            side_effect=sqlite3.OperationalError("no such table"),
        ):
            with mock.patch("workers.download_store.log.warning") as warning_mock:
                with self.assertRaises(sqlite3.OperationalError):
                    _record_revision("/tmp/test-lock.sqlite3", "0001_create_downloads")

                warning_mock.assert_not_called()

    def test_init_database_applies_schema_migrations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")

            init_database(db_path)

            with sqlite3.connect(db_path) as conn:
                migration_rows = conn.execute(
                    "SELECT revision FROM schema_migrations ORDER BY revision"
                ).fetchall()
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(downloads)").fetchall()
                }
                source_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(source_configs)"
                    ).fetchall()
                }

            self.assertEqual(
                [row[0] for row in migration_rows],
                [
                    "0001_create_downloads",
                    "0002_add_playback_columns",
                    "0003_add_config_tables",
                    "0004_add_source_configs",
                    "0005_add_source_enabled",
                    "0006_add_favorite_column",
                    "0007_add_relative_media_paths",
                    "0008_add_transcript_search_tables",
                    "0010_add_source_max_downloads",
                    "0011_add_source_explicit_content_filter",
                    "0012_add_youtube_include_flags",
                    "0013_add_source_title_exclude_filter",
                ],
            )
            self.assertIn("played", columns)
            self.assertIn("last_position_seconds", columns)
            self.assertIn("total_listened_seconds", columns)
            self.assertIn("favorite", columns)
            self.assertIn("file_path_relative", columns)
            self.assertIn("subtitle_path_relative", columns)
            self.assertIn("max_downloads", source_columns)
            self.assertIn("delete_explicit_content", source_columns)
            self.assertIn("include_shorts", source_columns)
            self.assertIn("include_livestreams", source_columns)
            self.assertIn("title_exclude", source_columns)

    def test_apply_migrations_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")

            apply_migrations(db_path)
            apply_migrations(db_path)

            with sqlite3.connect(db_path) as conn:
                count = conn.execute(
                    "SELECT COUNT(*) FROM schema_migrations"
                ).fetchone()[0]

            self.assertEqual(count, 12)

    def test_download_artifact_path_prefers_relative_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_root = os.path.join(tmpdir, "downloads")
            os.makedirs(output_root, exist_ok=True)
            resolved = resolve_download_artifact_path(
                output_root, "/tmp/old/location/item.mp3", "channel/item.mp3"
            )
            self.assertEqual(resolved, os.path.join(output_root, "channel", "item.mp3"))

    def test_config_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)

            update_stored_defaults(
                db_path,
                {
                    "audio_format": "m4a",
                    "max_downloads": "7",
                    "playlist_end": "9",
                    "js_runtime_path": "/usr/bin/qjs",
                    "manual_upload_delete_explicit_content": "1",
                },
            )
            update_download_settings(
                db_path,
                "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\txyz",
            )

            config = get_stored_config(db_path)
            self.assertEqual(config["defaults"]["audio_format"], "m4a")
            self.assertEqual(config["defaults"]["max_downloads"], 7)
            self.assertEqual(config["defaults"]["playlist_end"], 9)
            self.assertEqual(config["defaults"]["js_runtime_path"], "/usr/bin/qjs")
            self.assertTrue(config["defaults"]["manual_upload_delete_explicit_content"])
            self.assertIn("SID", config["download_settings"]["youtube_cookie_text"])

    def test_sources_seed_and_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)
            seed_sources_from_config(
                db_path,
                {
                    "defaults": {"output_root": tmpdir},
                    "youtube": [
                        {
                            "name": "YT 1",
                            "url": "https://youtube.com/@one",
                            "type": "audio",
                            "subtitles": True,
                            "max_downloads": 4,
                            "delete_explicit_content": True,
                        }
                    ],
                    "podcasts": [
                        {
                            "name": "Pod 1",
                            "url": "https://example.com/rss",
                            "subtitles": False,
                            "max_downloads": 2,
                        }
                    ],
                },
            )

            first = get_stored_config(db_path)
            self.assertEqual(first["defaults"]["auto_delete_content_days"], 0)
            self.assertEqual(len(first["youtube"]), 1)
            self.assertEqual(len(first["podcasts"]), 1)
            self.assertEqual(first["youtube"][0]["max_downloads"], 4)
            self.assertTrue(first["youtube"][0]["delete_explicit_content"])
            self.assertEqual(first["podcasts"][0]["max_downloads"], 2)

            replace_sources(
                db_path,
                [
                    {
                        "name": "YT 2",
                        "url": "https://youtube.com/@two",
                        "type": "video",
                        "max_downloads": 5,
                    }
                ],
                [],
            )
            replaced = get_stored_config(db_path)
            self.assertEqual(replaced["youtube"][0]["name"], "YT 2")
            self.assertEqual(replaced["youtube"][0]["type"], "video")
            self.assertEqual(replaced["youtube"][0]["max_downloads"], 5)
            self.assertTrue(replaced["youtube"][0]["enabled"])
            self.assertEqual(replaced["podcasts"], [])

    def test_update_source_config_updates_existing_row(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)
            seed_sources_from_config(
                db_path,
                {
                    "defaults": {"output_root": tmpdir},
                    "youtube": [
                        {
                            "name": "YT 1",
                            "url": "https://youtube.com/@one",
                            "type": "audio",
                            "subtitles": True,
                        }
                    ],
                    "podcasts": [],
                },
            )

            row_id = get_stored_config(db_path)["youtube"][0]["id"]
            updated = update_source_config(
                db_path,
                row_id=row_id,
                name="YT Updated",
                url="https://youtube.com/@updated",
                media_type="video",
                subtitles=False,
                subtitle_offset_seconds=1.25,
                max_downloads=6,
                delete_explicit_content=True,
            )

            self.assertTrue(updated)
            config = get_stored_config(db_path)
            self.assertEqual(config["youtube"][0]["name"], "YT Updated")
            self.assertEqual(
                config["youtube"][0]["url"], "https://youtube.com/@updated"
            )
            self.assertEqual(config["youtube"][0]["type"], "video")
            self.assertFalse(config["youtube"][0]["subtitles"])
            self.assertEqual(config["youtube"][0]["subtitle_offset_seconds"], 1.25)
            self.assertEqual(config["youtube"][0]["max_downloads"], 6)
            self.assertTrue(config["youtube"][0]["delete_explicit_content"])

    def test_get_download_position_seconds_returns_zero_when_locked(self):
        patch_target = "workers.download_store.sqlite3.connect"
        side_effect = sqlite3.OperationalError("database is locked")

        with mock.patch(patch_target, side_effect=side_effect):
            with mock.patch("workers.download_store.log.warning") as warning_mock:
                result = get_download_position_seconds("/tmp/test-lock.sqlite3", 42)

        self.assertEqual(result, 0.0)
        warning_mock.assert_not_called()

    def test_update_download_position_seconds_returns_false_when_locked(self):
        patch_target = "workers.download_store.sqlite3.connect"
        side_effect = sqlite3.OperationalError("database is locked")

        with mock.patch(patch_target, side_effect=side_effect):
            with mock.patch("workers.download_store.log.warning") as warning_mock:
                result = update_download_position_seconds(
                    "/tmp/test-lock.sqlite3", 42, 12.3
                )

        self.assertFalse(result)
        warning_mock.assert_not_called()

    def test_close_cached_descriptors_closes_django_connections(self):
        self.assertEqual(close_cached_descriptors(), 0)


if __name__ == "__main__":
    unittest.main()
