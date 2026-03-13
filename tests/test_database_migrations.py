import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import apply_migrations, get_stored_config, init_database, update_download_settings, update_stored_defaults  # noqa: E402


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
                ["0001_create_downloads", "0002_add_playback_columns", "0003_add_config_tables"],
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

            self.assertEqual(count, 3)

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


if __name__ == "__main__":
    unittest.main()
