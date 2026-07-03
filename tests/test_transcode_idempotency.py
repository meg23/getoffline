import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

try:
    import django

    django.setup()
except Exception:  # pragma: no cover - import guard matches existing tests
    django = None

if django is not None:
    from workers.handlers import _transcode_idempotency_key, _transcode_lock_key


@unittest.skipIf(django is None, "Django is not installed")
class TranscodeIdempotencyTests(unittest.TestCase):
    def test_download_id_key_ignores_parent_job(self):
        payload = {"download_id": 42, "source_file_path": "/tmp/input.webm"}

        self.assertEqual(
            _transcode_idempotency_key("default", payload),
            "transcode_media:default:download:42",
        )

    def test_file_key_is_stable_for_same_source_and_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "video.webm"
            target = Path(tmpdir) / "video.mp4"
            source.write_bytes(b"video")
            payload = {
                "source_file_path": str(source),
                "source_file_paths": [str(source)],
                "target_file_path": str(target),
                "item_uid": "abc123",
            }

            first = _transcode_idempotency_key("default", payload)
            second = _transcode_idempotency_key("default", dict(payload))

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("transcode_media:default:file:"))

    def test_lock_key_is_stable_for_duplicate_file_payloads(self):
        payload = {
            "source_file_path": "/tmp/video.webm",
            "target_file_path": "/tmp/video.mp4",
            "item_uid": "same-video",
        }

        self.assertEqual(
            _transcode_lock_key("default", payload),
            _transcode_lock_key("default", dict(payload)),
        )


if __name__ == "__main__":
    unittest.main()
