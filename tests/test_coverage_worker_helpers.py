from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

os.environ.setdefault("GETOFFLINE_LOG_FILE", "/tmp/getoffline-coverage-tests.log")

from workers import subtitles, utils, ytdlp_helpers


class WorkerUtilityCoverageTests(unittest.TestCase):
    def test_filename_and_title_helpers_cover_normalization_and_matches(self):
        self.assertEqual(utils.sanitize("../Bad title?.mp3"), "_Bad_title_.mp3")
        self.assertEqual(utils.sanitize("..."), "item")
        self.assertEqual(utils.sanitize_channel_name("My_Channel"), "MyChannel")
        self.assertEqual(utils.sanitize_channel_name("___"), "channel")
        self.assertEqual(utils.split_title_filter_terms("  spoiler,\nNews "), ["spoiler", "news"])
        self.assertEqual(utils.title_matches_filter("Daily News", ["news"]), "news")
        self.assertEqual(utils.title_matches_filter("Daily News", []), "")
        self.assertEqual(utils.title_matches_filter(None, ["news"]), "")

    def test_normalize_media_filename_handles_same_path_and_collisions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            unchanged = root / "clean.mp3"
            unchanged.write_bytes(b"clean")
            self.assertEqual(utils.normalize_media_filename(unchanged), unchanged)

            source = root / "messy..name.mp3"
            source.write_bytes(b"source")
            normalized = root / "messy.name.mp3"
            normalized.write_bytes(b"existing")
            renamed = utils.normalize_media_filename(source)

            self.assertEqual(renamed.name, "messy.name_1.mp3")
            self.assertEqual(renamed.read_bytes(), b"source")

    def test_ensure_dir_creates_nested_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "nested" / "media"
            utils.ensure_dir(target)
            self.assertTrue(target.is_dir())


class YtdlpHelperCoverageTests(unittest.TestCase):
    def test_logger_normalizes_messages_and_records_categories(self):
        stats: dict[str, int] = {}
        logger = ytdlp_helpers.YoutubeDlQuietLogger(stats)
        with patch.object(ytdlp_helpers.log, "debug") as debug, patch.object(
            ytdlp_helpers.log, "info"
        ) as info, patch.object(ytdlp_helpers.log, "warning") as warning, patch.object(
            ytdlp_helpers.log, "error"
        ) as error:
            logger.debug("[youtube] [debug] details")
            logger.debug("[youtube] [download] downloading item 1")
            logger.warning("[youtube] Video unavailable; sign in")
            logger.error("[youtube] private video")
            logger.warning("")

        self.assertEqual(stats["playlist_item_announced"], 1)
        self.assertEqual(stats["messages_unavailable"], 1)
        self.assertEqual(stats["messages_auth"], 1)
        self.assertEqual(stats["messages_private"], 1)
        self.assertEqual(stats["warnings"], 1)
        self.assertEqual(stats["errors"], 1)
        debug.assert_called_once()
        info.assert_called_once()
        warning.assert_called_once()
        error.assert_called_once()

    def test_quickjs_resolution_and_remote_component_configuration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            binary = Path(tmpdir) / "qjs"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(
                ytdlp_helpers._resolve_quickjs_binary(str(binary)), str(binary.resolve())
            )
            self.assertIsNone(ytdlp_helpers._resolve_quickjs_binary(str(binary) + "-missing"))

        with patch("workers.ytdlp_helpers.shutil.which", return_value="/usr/bin/qjs"):
            self.assertEqual(ytdlp_helpers._resolve_quickjs_binary("qjs"), "/usr/bin/qjs")

        options = {"remote_components": "ejs:github,other"}
        with patch.object(ytdlp_helpers, "_resolve_quickjs_binary", return_value="/opt/qjs"):
            ytdlp_helpers.enable_youtube_quickjs_remote_component(options, "test")
        self.assertEqual(options["js_runtimes"], {"quickjs": {"path": "/opt/qjs"}})
        self.assertEqual(options["remote_components"], ["ejs:github", "other"])

        options = {"remote_components": ["other"]}
        with patch.object(ytdlp_helpers, "_resolve_quickjs_binary", return_value=None):
            ytdlp_helpers.enable_youtube_quickjs_remote_component(options, "test")
        self.assertNotIn("js_runtimes", options)
        self.assertEqual(options["remote_components"], ["other"])

    def test_ytdlp_options_and_video_id_parsing(self):
        options: dict = {"extractor_args": {"youtube": {"foo": ["bar"]}}}
        ytdlp_helpers.apply_ytdlp_player_js_variant_workaround(options)
        self.assertEqual(options["extractor_args"]["youtube"]["player_js_variant"], ["main"])

        self.assertEqual(ytdlp_helpers.clean_log_title("  😀  A   title  "), "A title")
        self.assertEqual(ytdlp_helpers.clean_log_title("  "), "unknown title")
        self.assertIsNone(ytdlp_helpers.extract_youtube_video_id(None))
        self.assertEqual(
            ytdlp_helpers.extract_youtube_video_id("https://youtu.be/abc123"), "abc123"
        )
        self.assertEqual(
            ytdlp_helpers.extract_youtube_video_id(
                "https://www.youtube.com/watch?v=watch123"
            ),
            "watch123",
        )
        self.assertEqual(
            ytdlp_helpers.extract_youtube_video_id(
                "https://youtube.com/shorts/short123"
            ),
            "short123",
        )
        self.assertEqual(
            ytdlp_helpers.extract_youtube_video_id(
                "https://youtube.com/embed/embed123"
            ),
            "embed123",
        )
        self.assertIsNone(ytdlp_helpers.extract_youtube_video_id("https://example.com/x"))

    def test_source_name_resolution_handles_playlist_channel_title_and_fallback(self):
        class FakeYoutubeDL:
            captured_options: ClassVar[dict | None] = None
            response: ClassVar[dict] = {}

            def __init__(self, options):
                type(self).captured_options = options

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def extract_info(self, _url, download=False):
                self.download = download
                return type(self).response

        with (
            patch.object(ytdlp_helpers, "_get_youtubedl", return_value=FakeYoutubeDL),
            patch.object(ytdlp_helpers, "enable_youtube_quickjs_remote_component"),
            patch.object(ytdlp_helpers, "apply_ytdlp_player_js_variant_workaround"),
        ):
            FakeYoutubeDL.response = {
                "_type": "playlist",
                "entries": [{"channel": "My_Channel"}],
            }
            self.assertEqual(
                ytdlp_helpers.resolve_youtube_source_name(
                    "https://youtube.com/watch?v=abc", "/tmp/cookies.txt"
                ),
                "MyChannel",
            )
            self.assertEqual(FakeYoutubeDL.captured_options["cookiefile"], "/tmp/cookies.txt")

            FakeYoutubeDL.response = {"title": "Fallback Title"}
            self.assertEqual(
                ytdlp_helpers.resolve_youtube_source_name("https://youtube.com/watch?v=abc"),
                "FallbackTitle",
            )
            FakeYoutubeDL.response = {}
            self.assertEqual(
                ytdlp_helpers.resolve_youtube_source_name("https://youtube.com/watch?v=abc"),
                "youtube-single",
            )

        with self.assertRaises(ValueError):
            ytdlp_helpers.resolve_youtube_source_name("")


class SubtitleCoverageTests(unittest.TestCase):
    def test_sidecar_cleanup_and_folder_scan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "episode.mp3"
            media.write_bytes(b"audio")
            keep = root / "episode.srt"
            keep.write_text("keep", encoding="utf-8")
            (root / "episode.en.srt").write_text("extra", encoding="utf-8")
            (root / "episode.vtt").write_text("extra", encoding="utf-8")
            (root / "unrelated.srt").write_text("keep", encoding="utf-8")

            subtitles.cleanup_subtitle_sidecars_for_folder(root)

            self.assertTrue(keep.exists())
            self.assertFalse((root / "episode.en.srt").exists())
            self.assertFalse((root / "episode.vtt").exists())
            self.assertTrue((root / "unrelated.srt").exists())
            self.assertIsNone(subtitles.cleanup_subtitle_sidecars_for_folder(root / "missing"))

    def test_timestamp_helpers_shift_and_clamp_srt_content(self):
        self.assertEqual(subtitles._parse_srt_timestamp("01:02:03,400"), 3723.4)
        self.assertEqual(subtitles._format_srt_timestamp(-1), "00:00:00,000")
        self.assertEqual(subtitles._format_srt_timestamp(59.9996), "00:01:00,000")

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "captions.srt"
            path.write_text(
                "1\n00:00:00,100 --> 00:00:00,100 align:start\ntext\n\nnot a timestamp\n",
                encoding="utf-8",
            )
            subtitles._shift_srt_timestamps(path, -0.5)
            content = path.read_text(encoding="utf-8")
            self.assertIn("00:00:00,000 --> 00:00:00,010 align:start", content)
            self.assertIn("not a timestamp", content)
            before = content
            subtitles._shift_srt_timestamps(path, 0.0)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_generate_subtitles_success_reuse_marker_and_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "episode.mp3"
            media.write_bytes(b"audio")
            old_time = media.stat().st_mtime - 10
            subtitle = root / "episode.srt"
            subtitle.write_text("old", encoding="utf-8")
            os.utime(subtitle, (old_time, old_time))

            with patch("workers.subtitles.transcribe_with_whisper") as transcribe:
                transcribe.return_value = {
                    "text": "hello",
                    "segments": [
                        {"start": 1.0, "end": 1.0, "text": " hello "},
                        {"start": 2.0, "end": 3.0, "text": ""},
                    ],
                }
                marker = subtitle.with_suffix(".srt.failed")
                marker.write_text("old failure", encoding="utf-8")
                os.utime(marker, (old_time, old_time))
                result = subtitles.generate_whisper_subtitles(
                    media,
                    {
                        "model": "tiny",
                        "subtitle_language": "en",
                        "subtitle_time_offset_seconds": -2.0,
                        "subtitle_transcription_mode": "legacy",
                    },
                )

            self.assertEqual(result, subtitle)
            self.assertFalse(marker.exists())
            self.assertIn("00:00:00,000 --> 00:00:00,010", subtitle.read_text())
            transcribe.assert_called_once_with(
                media,
                "tiny",
                "subtitle-generation",
                language="en",
                mode="in_process",
            )

            subtitle.write_text("current", encoding="utf-8")
            self.assertEqual(subtitles.generate_whisper_subtitles(media, {}), subtitle)

            subtitle.unlink()
            marker.write_text("known failure", encoding="utf-8")
            self.assertIsNone(subtitles.generate_whisper_subtitles(media, {}))

    def test_generate_subtitles_handles_known_and_unexpected_whisper_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "episode.mp3"
            media.write_bytes(b"audio")
            marker = root / "episode.srt.failed"
            with patch(
                "workers.subtitles.transcribe_with_whisper",
                side_effect=RuntimeError("No decodable audio stream found in media file"),
            ):
                self.assertIsNone(subtitles.generate_whisper_subtitles(media, {}))
            self.assertTrue(marker.exists())

            marker.unlink()
            with patch(
                "workers.subtitles.transcribe_with_whisper",
                side_effect=RuntimeError("model exploded"),
            ), self.assertRaisesRegex(RuntimeError, "model exploded"):
                subtitles.generate_whisper_subtitles(media, {})

    def test_create_subtitles_reuses_generates_skips_and_handles_errors(self):
        logger = type(
            "Logger",
            (),
            {
                "info": lambda self, *args: None,
                "warning": lambda self, *args: None,
                "exception": lambda self, *args: None,
            },
        )()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media = root / "episode.mp3"
            media.write_bytes(b"audio")
            existing = root / "episode.srt"
            existing.write_text("captions", encoding="utf-8")
            self.assertEqual(
                subtitles.create_subtitles(media, 0, True, logger, "episode", "podcast"),
                existing,
            )

            existing.unlink()
            generated = root / "episode.srt"
            with patch("workers.subtitles.generate_whisper_subtitles", return_value=generated):
                self.assertEqual(
                    subtitles.create_subtitles(media, 1, True, logger, "episode", "podcast"),
                    generated,
                )

            self.assertIsNone(
                subtitles.create_subtitles(media, 0, False, logger, "episode", "podcast")
            )
            self.assertIsNone(
                subtitles.create_subtitles(
                    root / "missing.mp3", 0, True, logger, "missing", "podcast"
                )
            )
            with patch(
                "workers.subtitles._find_existing_whisper_subtitle",
                side_effect=RuntimeError("subtitle failure"),
            ):
                self.assertIsNone(
                    subtitles.create_subtitles(media, 0, True, logger, "episode", "podcast")
                )


if __name__ == "__main__":
    unittest.main()
