import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import apply_migrations, init_database, load_runtime_config, store_runtime_config  # noqa: E402


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
                ["0001_create_downloads", "0002_add_playback_columns", "0003_create_config_tables"],
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

    def test_runtime_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "downloads.sqlite3")
            init_database(db_path)

            config = {
                "defaults": {
                    "output_root": os.path.join(tmpdir, "downloads"),
                    "cookie_path": os.path.join(tmpdir, "cookies.txt"),
                    "audio_format": "mp3",
                    "audio_quality": 0,
                    "max_downloads": 2,
                    "playlist_end": 2,
                    "database_path": db_path,
                },
                "youtube": [
                    {
                        "name": "SampleChannel",
                        "url": "https://youtube.com/@sample",
                        "type": "audio",
                        "subtitles": True,
                    }
                ],
                "podcasts": [
                    {
                        "name": "SamplePodcast",
                        "url": "https://example.com/feed.xml",
                        "subtitles": False,
                    }
                ],
            }

            store_runtime_config(db_path, config)
            loaded = load_runtime_config(db_path)

            self.assertEqual(loaded["defaults"]["database_path"], db_path)
            self.assertEqual(loaded["youtube"][0]["name"], "SampleChannel")
            self.assertTrue(loaded["youtube"][0]["subtitles"])
            self.assertEqual(loaded["podcasts"][0]["name"], "SamplePodcast")
            self.assertFalse(loaded["podcasts"][0]["subtitles"])


if __name__ == "__main__":
    unittest.main()
