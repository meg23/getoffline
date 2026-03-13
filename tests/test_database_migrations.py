import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from database import apply_migrations, init_database  # noqa: E402


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
                ["0001_create_downloads", "0002_add_playback_columns"],
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

            self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()
