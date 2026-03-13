import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import (  # noqa: E402
    apply_migrations,
    get_stored_config,
    init_database,
    replace_sources,
    seed_sources_from_config,
    update_download_settings,
    update_stored_defaults,
)


class DatabaseMigrationsTests(unittest.TestCase):
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
                ],
            )
            self.assertIn("played", columns)
            self.assertIn("last_position_seconds", columns)
            self.assertIn("total_listened_seconds", columns)

    def test_apply_migrations_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")

            apply_migrations(db_path)
            apply_migrations(db_path)

            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

            self.assertEqual(count, 5)

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


if __name__ == "__main__":
    unittest.main()
