import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")

import django

django.setup()

from workers.media_fetch import ensure_local_media


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc_value, _traceback):
        return False

    def read(self, size: int) -> bytes:
        content, self.content = self.content[:size], self.content[size:]
        return content


class WorkerMediaFetchTests(unittest.TestCase):
    def test_existing_local_file_is_used_without_api_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "local.mp3"
            path.write_bytes(b"local")
            download = SimpleNamespace(file_path=str(path), profile_id="alice", id=1)
            with patch("workers.media_fetch.urlopen") as urlopen:
                self.assertEqual(ensure_local_media(download), path.resolve())
            urlopen.assert_not_called()

    def test_missing_local_file_is_fetched_and_cached_atomically(self):
        download = SimpleNamespace(
            file_path="/mounted/alice/episode.mp3",
            profile_id="alice",
            id=7,
            file_size_bytes=7,
        )
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                "GETOFFLINE_WORKER_MEDIA_CACHE_DIR": tmpdir,
                "GETOFFLINE_WORKER_API_TOKEN": "secret",
                "GETOFFLINE_WORKER_API_URL": "http://api.test/api",
            },
            clear=False,
        ), patch("workers.media_fetch.urlopen", return_value=_Response(b"fetched")) as urlopen:
            path = ensure_local_media(download)
            self.assertEqual(path.read_bytes(), b"fetched")

        self.assertIn("/internal/worker/media/alice/7", urlopen.call_args.args[0].full_url)
