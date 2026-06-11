import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from content_filter import (  # noqa: E402
    ExplicitContentMatch,
    delete_media_artifacts,
    find_explicit_content,
    log_filtered_deletion,
    screen_transcript,
    transcript_text,
)


class ContentFilterTests(unittest.TestCase):
    def test_detects_profanity_as_a_whole_word(self):
        match = find_explicit_content("That was fucking ridiculous.")
        self.assertIsNotNone(match)
        self.assertEqual(match.category, "profanity")
        self.assertIsNone(find_explicit_content("The shiitake mushrooms were delicious."))

    def test_detects_sexual_phrase(self):
        match = find_explicit_content("The discussion included oral sex and consent.")
        self.assertIsNotNone(match)
        self.assertEqual(match.category, "sexual content")

    def test_reads_srt_without_timestamps_or_sequence_numbers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle = Path(tmpdir) / "episode.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nClean spoken words.\n",
                encoding="utf-8",
            )
            self.assertEqual(transcript_text(subtitle), "Clean spoken words.")
            self.assertIsNone(screen_transcript(subtitle))

    def test_delete_media_artifacts_removes_same_stem_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            media = folder / "episode.mp3"
            subtitle = folder / "episode.srt"
            thumbnail = folder / "episode.webp"
            other = folder / "another.mp3"
            for path in (media, subtitle, thumbnail, other):
                path.write_text("data", encoding="utf-8")

            deleted_paths = delete_media_artifacts(media)

            self.assertFalse(media.exists())
            self.assertFalse(subtitle.exists())
            self.assertFalse(thumbnail.exists())
            self.assertTrue(other.exists())
            self.assertEqual(set(deleted_paths), {media.resolve(), subtitle.resolve(), thumbnail.resolve()})

    def test_filtered_deletion_writes_stable_audit_event(self):
        media_path = Path("/tmp/episode.mp3")
        deleted_paths = [media_path, media_path.with_suffix(".srt")]
        match = ExplicitContentMatch(category="profanity", term="fucking")

        with patch("content_filter.log.warning") as warning:
            log_filtered_deletion(
                source_type="podcast",
                source_name="Example Show",
                title="Episode 7",
                media_path=media_path,
                match=match,
                deleted_paths=deleted_paths,
            )

        warning.assert_called_once()
        message, *arguments = warning.call_args.args
        self.assertIn("CONTENT_FILTER_DELETION", message)
        self.assertEqual(arguments[0], "podcast")
        self.assertEqual(arguments[1], "Example Show")
        self.assertEqual(arguments[2], "Episode 7")
        self.assertEqual(arguments[3], "profanity")
        self.assertEqual(arguments[4], "fucking")
        self.assertIn("episode.mp3", arguments[6])
        self.assertIn("episode.srt", arguments[6])


if __name__ == "__main__":
    unittest.main()
