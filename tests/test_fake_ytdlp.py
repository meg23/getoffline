from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workers.fake_ytdlp import YoutubeDL


class FakeYtDlpTests(unittest.TestCase):
    def test_non_youtube_urls_get_distinct_output_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_template = str(
                Path(temporary_directory) / "%(title)s [%(id)s].%(ext)s"
            )
            with YoutubeDL({"outtmpl": output_template}) as ydl:
                first = ydl.extract_info("https://cdn.example.test/episode-one.mp3")
                second = ydl.extract_info("https://cdn.example.test/episode-two.mp3")

            self.assertNotEqual(first["id"], second["id"])
            self.assertNotEqual(
                ydl.prepare_filename(first), ydl.prepare_filename(second)
            )
            self.assertTrue(Path(ydl.prepare_filename(first)).is_file())
            self.assertTrue(Path(ydl.prepare_filename(second)).is_file())

    def test_youtube_fixture_keeps_expected_id(self):
        with YoutubeDL() as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=BB49x_uMlGA")

        self.assertEqual(info["id"], "BB49x_uMlGA")


if __name__ == "__main__":
    unittest.main()
