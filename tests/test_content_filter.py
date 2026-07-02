import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workers.content_filter import (  # noqa: E402
    ExplicitContentMatch,
    _patch_profanity_check_compat,
    delete_media_artifacts,
    find_explicit_content,
    log_filtered_deletion,
    main,
    screen_transcript,
    transcript_text,
)


class ContentFilterTests(unittest.TestCase):
    def test_detects_profanity_with_profanity_check_model(self):
        with patch("workers.content_filter._predict_profanity", return_value=[1]):
            match = find_explicit_content("That was fucking ridiculous.")

        self.assertIsNotNone(match)
        self.assertEqual(match.category, "profanity")
        self.assertEqual(match.term, "profanity-check")
        self.assertEqual(match.sentence, "That was fucking ridiculous.")

        with patch("workers.content_filter._predict_profanity", return_value=[0]):
            self.assertIsNone(
                find_explicit_content("The shiitake mushrooms were delicious.")
            )

    def test_returns_only_the_sentence_containing_the_match(self):
        with patch("workers.content_filter._predict_profanity", return_value=[0, 1, 0]):
            match = find_explicit_content(
                "This sentence is clean. The next sentence contains bullshit! This is also clean."
            )

        self.assertIsNotNone(match)
        self.assertEqual(match.term, "profanity-check")
        self.assertEqual(match.sentence, "The next sentence contains bullshit!")

    def test_falls_back_to_explicit_term_list_when_model_unavailable(self):
        with patch("workers.content_filter._predict_profanity", return_value=None):
            match = find_explicit_content("That was fucking ridiculous.")

        self.assertIsNotNone(match)
        self.assertEqual(match.category, "profanity")
        self.assertEqual(match.term, "fucking")
        self.assertEqual(match.sentence, "That was fucking ridiculous.")

    def test_reads_srt_without_timestamps_or_sequence_numbers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle = Path(tmpdir) / "episode.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nClean spoken words.\n",
                encoding="utf-8",
            )
            self.assertEqual(transcript_text(subtitle), "Clean spoken words.")
            with patch("workers.content_filter._predict_profanity", return_value=[0]):
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
            self.assertEqual(
                set(deleted_paths),
                {media.resolve(), subtitle.resolve(), thumbnail.resolve()},
            )

    def test_delete_media_artifacts_escapes_glob_characters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            media = folder / "Episode [abc123].mp4"
            subtitle = folder / "Episode [abc123].srt"
            similarly_named = folder / "Episode a.srt"
            for path in (media, subtitle, similarly_named):
                path.write_text("data", encoding="utf-8")

            deleted_paths = delete_media_artifacts(media)

            self.assertFalse(media.exists())
            self.assertFalse(subtitle.exists())
            self.assertTrue(similarly_named.exists())
            self.assertEqual(set(deleted_paths), {media.resolve(), subtitle.resolve()})

    def test_profanity_check_compat_exposes_legacy_sklearn_modules(self):
        try:
            import joblib  # noqa: F401
            import sklearn.externals  # noqa: F401
            import sklearn.svm._classes  # noqa: F401
        except Exception as exc:
            self.skipTest(f"optional sklearn/joblib compatibility deps unavailable: {exc}")

        sys.modules.pop("sklearn.externals.joblib", None)
        sys.modules.pop("sklearn.svm.classes", None)

        _patch_profanity_check_compat()

        self.assertIn("sklearn.externals.joblib", sys.modules)
        self.assertIn("sklearn.svm.classes", sys.modules)

    def test_cli_reports_clean_and_matched_text(self):
        with (
            patch("workers.content_filter.find_explicit_content", return_value=None),
            patch("builtins.print") as print_call,
        ):
            self.assertEqual(main(["--text", "plain words"]), 0)
        print_call.assert_called_once_with("clean", flush=True)

        match = ExplicitContentMatch(
            category="profanity",
            term="profanity-check",
            sentence="flagged words",
        )
        with (
            patch("workers.content_filter.find_explicit_content", return_value=match),
            patch("builtins.print") as print_call,
        ):
            self.assertEqual(main(["--text", "flagged words"]), 0)
        print_call.assert_any_call(
            "matched category=profanity term='profanity-check'", flush=True
        )
        print_call.assert_any_call("sentence=flagged words", flush=True)

        with (
            patch("workers.content_filter.find_explicit_content", return_value=match),
            patch("builtins.print"),
        ):
            self.assertEqual(
                main(["--fail-on-match", "--text", "flagged words"]), 1
            )

    def test_cli_check_model_reports_active_model(self):
        with (
            patch("workers.content_filter._predict_profanity", return_value=[0]),
            patch("builtins.print") as print_call,
        ):
            self.assertEqual(main(["--check-model"]), 0)
        print_call.assert_called_once_with("model=profanity-check", flush=True)

        with (
            patch("workers.content_filter._predict_profanity", return_value=None),
            patch("builtins.print") as print_call,
        ):
            self.assertEqual(main(["--check-model"]), 2)
        self.assertIn("model=fallback", print_call.call_args.args[0])

    def test_filtered_deletion_writes_stable_audit_event(self):
        media_path = Path("/tmp/episode.mp3")
        deleted_paths = [media_path, media_path.with_suffix(".srt")]
        match = ExplicitContentMatch(
            category="profanity",
            term="fucking",
            sentence="That was fucking ridiculous.",
        )

        with patch("workers.content_filter.log.warning") as warning:
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
        self.assertEqual(arguments[5], "That was fucking ridiculous.")
        self.assertIn("episode.mp3", arguments[7])
        self.assertIn("episode.srt", arguments[7])


if __name__ == "__main__":
    unittest.main()
