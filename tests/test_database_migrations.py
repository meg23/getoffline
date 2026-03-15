import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import (  # noqa: E402
    HAS_SQLALCHEMY,
    _record_revision,
    apply_migrations,
    get_download_position_seconds,
    get_stored_config,
    init_database,
    update_download_position_seconds,
    replace_sources,
    seed_sources_from_config,
    update_download_settings,
    update_source_config,
    update_stored_defaults,
)


class DatabaseMigrationsTests(unittest.TestCase):
    def test_logs_when_sqlite_write_hits_lock(self):
        with mock.patch("database.sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
            with mock.patch("database.log.warning") as warning_mock:
                with self.assertRaises(sqlite3.OperationalError):
                    _record_revision("/tmp/test-lock.sqlite3", "0001_create_downloads")

                warning_mock.assert_called_once()
                self.assertIn("recording schema revision", str(warning_mock.call_args))

    def test_does_not_log_non_lock_sqlite_write_errors(self):
        with mock.patch("database.sqlite3.connect", side_effect=sqlite3.OperationalError("no such table")):
            with mock.patch("database.log.warning") as warning_mock:
                with self.assertRaises(sqlite3.OperationalError):
                    _record_revision("/tmp/test-lock.sqlite3", "0001_create_downloads")

                warning_mock.assert_not_called()

    def test_init_database_applies_schema_migrations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")

            init_database(db_path)

            with sqlite3.connect(db_path) as conn:
                migration_rows = conn.execute("SELECT revision FROM schema_migrations ORDER BY revision").fetchall()
                columns = {row[1] for row in conn.execute("PRAGMA table_info(downloads)").fetchall()}

            self.assertEqual(
                [row[0] for row in migration_rows],
                [
                    "0001_create_downloads",
                    "0002_add_playback_columns",
                    "0003_add_config_tables",
                    "0004_add_source_configs",
                    "0005_add_source_enabled",
                    "0006_add_favorite_column",
                ],
            )
            self.assertIn("played", columns)
            self.assertIn("last_position_seconds", columns)
            self.assertIn("total_listened_seconds", columns)
            self.assertIn("favorite", columns)

    def test_apply_migrations_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")

            apply_migrations(db_path)
            apply_migrations(db_path)

            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

            self.assertEqual(count, 6)

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
                },
            )
            update_download_settings(db_path, "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\txyz")

            config = get_stored_config(db_path)
            self.assertEqual(config["defaults"]["audio_format"], "m4a")
            self.assertEqual(config["defaults"]["max_downloads"], 7)
            self.assertEqual(config["defaults"]["playlist_end"], 9)
            self.assertIn("SID", config["download_settings"]["youtube_cookie_text"])

    def test_sources_seed_and_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)
            seed_sources_from_config(
                db_path,
                {
                    "defaults": {"output_root": tmpdir},
                    "youtube": [{"name": "YT 1", "url": "https://youtube.com/@one", "type": "audio", "subtitles": True}],
                    "podcasts": [{"name": "Pod 1", "url": "https://example.com/rss", "subtitles": False}],
                },
            )

            first = get_stored_config(db_path)
            self.assertEqual(len(first["youtube"]), 1)
            self.assertEqual(len(first["podcasts"]), 1)

            replace_sources(db_path, [{"name": "YT 2", "url": "https://youtube.com/@two", "type": "video"}], [])
            replaced = get_stored_config(db_path)
            self.assertEqual(replaced["youtube"][0]["name"], "YT 2")
            self.assertEqual(replaced["youtube"][0]["type"], "video")
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
            )

            self.assertTrue(updated)
            config = get_stored_config(db_path)
            self.assertEqual(config["youtube"][0]["name"], "YT Updated")
            self.assertEqual(config["youtube"][0]["url"], "https://youtube.com/@updated")
            self.assertEqual(config["youtube"][0]["type"], "video")
            self.assertFalse(config["youtube"][0]["subtitles"])
            self.assertEqual(config["youtube"][0]["subtitle_offset_seconds"], 1.25)

    def test_get_download_position_seconds_returns_zero_when_locked(self):
        if HAS_SQLALCHEMY:
            patch_target = "database.Session"
            side_effect = Exception("database is locked")
        else:
            patch_target = "database.sqlite3.connect"
            side_effect = sqlite3.OperationalError("database is locked")

        with mock.patch(patch_target, side_effect=side_effect):
            with mock.patch("database.log.warning") as warning_mock:
                result = get_download_position_seconds("/tmp/test-lock.sqlite3", 42)

        self.assertEqual(result, 0.0)
        warning_mock.assert_called_once()

    def test_update_download_position_seconds_returns_false_when_locked(self):
        if HAS_SQLALCHEMY:
            patch_target = "database.Session"
            side_effect = Exception("database is locked")
        else:
            patch_target = "database.sqlite3.connect"
            side_effect = sqlite3.OperationalError("database is locked")

        with mock.patch(patch_target, side_effect=side_effect):
            with mock.patch("database.log.warning") as warning_mock:
                result = update_download_position_seconds("/tmp/test-lock.sqlite3", 42, 12.3)

        self.assertFalse(result)
        warning_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
