import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from content_retention import enforce_content_retention  # noqa: E402
from database import init_database, upsert_download  # noqa: E402


class ContentRetentionTests(unittest.TestCase):
    def _insert_download(self, db_path: str, media_path: Path, *, item_uid: str, source_type: str = "youtube"):
        upsert_download(
            db_path,
            {
                "source_type": source_type,
                "source_name": "Manual Uploads" if source_type == "manual" else "Test Source",
                "item_uid": item_uid,
                "title": item_uid,
                "file_path": str(media_path),
                "file_ext": media_path.suffix.lstrip("."),
                "file_size_bytes": media_path.stat().st_size if media_path.exists() else 0,
                "subtitle_enabled": False,
                "download_status": "downloaded",
                "storage_root": str(media_path.parent),
            },
        )

    def test_deletes_expired_automatic_content_and_marks_it_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = str(root / "downloads.sqlite3")
            init_database(db_path)
            media_path = root / "old.mp3"
            media_path.write_bytes(b"old")
            self._insert_download(db_path, media_path, item_uid="old")
            old_time = datetime.now(timezone.utc) - timedelta(days=31)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE downloads SET completed_at = ?, first_seen_at = ? WHERE item_uid = 'old'",
                    (old_time.isoformat(), old_time.isoformat()),
                )
                conn.commit()

            result = enforce_content_retention(db_path, str(root), 30)

            self.assertFalse(media_path.exists())
            self.assertEqual(result.deleted_files, 1)
            self.assertEqual(result.marked_missing, 1)
            with sqlite3.connect(db_path) as conn:
                status, error = conn.execute(
                    "SELECT download_status, error_message FROM downloads WHERE item_uid = 'old'"
                ).fetchone()
            self.assertEqual(status, "missing")
            self.assertEqual(error, "Media file is missing")

    def test_marks_already_absent_automatic_content_missing_even_when_recent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = str(root / "downloads.sqlite3")
            init_database(db_path)
            missing_path = root / "missing.mp4"
            self._insert_download(db_path, missing_path, item_uid="missing")

            result = enforce_content_retention(db_path, str(root), 30)

            self.assertEqual(result.deleted_files, 0)
            self.assertEqual(result.marked_missing, 1)
            with sqlite3.connect(db_path) as conn:
                status = conn.execute(
                    "SELECT download_status FROM downloads WHERE item_uid = 'missing'"
                ).fetchone()[0]
            self.assertEqual(status, "missing")

    def test_manual_content_is_ignored_even_when_expired_or_absent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = str(root / "downloads.sqlite3")
            init_database(db_path)
            media_path = root / "manual.mp3"
            media_path.write_bytes(b"manual")
            self._insert_download(db_path, media_path, item_uid="manual", source_type="manual")
            old_time = datetime.now(timezone.utc) - timedelta(days=365)
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE downloads SET completed_at = ?", (old_time.isoformat(),))
                conn.commit()

            result = enforce_content_retention(db_path, str(root), 1)

            self.assertTrue(media_path.exists())
            self.assertEqual(result.ignored_manual, 1)
            with sqlite3.connect(db_path) as conn:
                status = conn.execute("SELECT download_status FROM downloads").fetchone()[0]
            self.assertEqual(status, "downloaded")

    def test_zero_days_disables_deletion_and_missing_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = str(root / "downloads.sqlite3")
            init_database(db_path)
            missing_path = root / "missing.mp3"
            self._insert_download(db_path, missing_path, item_uid="missing")

            result = enforce_content_retention(db_path, str(root), 0)

            self.assertEqual(result.marked_missing, 0)
            with sqlite3.connect(db_path) as conn:
                status = conn.execute("SELECT download_status FROM downloads").fetchone()[0]
            self.assertEqual(status, "downloaded")


if __name__ == "__main__":
    unittest.main()
