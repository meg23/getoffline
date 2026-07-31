import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("GETOFFLINE_LOG_FILE", "/tmp/getoffline-censor-tests.log")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")

import django

django.setup()

from workers.handlers import _ffmpeg_censor_filter_plan, censor_audio


class InitialCensorFilterCompositionTests(unittest.TestCase):
    def _profile_setting(self, _profile_id: str, key: str, default: str) -> str:
        if key == "ffmpeg_audio_filter":
            return "loudnorm=I=-14:TP=-1.5:LRA=11"
        return default

    def test_split_beep_uses_audio_input_one_after_configured_filter(self):
        codec_args = [
            "-map",
            "0:v:0?",
            "-map",
            "1:a:0?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
        ]
        payload = {
            "pre_transcode_censor": True,
            "censor_method": "beep",
            "censor_segments": [
                {"start_seconds": 0.4, "end_seconds": 0.8}
            ],
        }

        with patch(
            "workers.handlers._profile_setting", side_effect=self._profile_setting
        ):
            filter_args, filtered_codec_args, segments = _ffmpeg_censor_filter_plan(
                profile_id="default",
                payload=payload,
                media_kind="video",
                input_count=2,
                codec_args=codec_args,
            )

        self.assertEqual(filter_args[0], "-filter_complex")
        self.assertIn(
            "[1:a:0]loudnorm=I=-14:TP=-1.5:LRA=11,volume=0",
            filter_args[1],
        )
        self.assertEqual(filter_args[-2:], ["-map", "[censored_audio]"])
        self.assertNotIn("1:a:0?", filtered_codec_args)
        self.assertIn("0:v:0?", filtered_codec_args)
        self.assertEqual(len(segments), 1)

    def test_duck_replaces_existing_audio_option_with_one_composed_chain(self):
        codec_args = ["-c:a", "aac", "-af", "old-filter"]
        payload = {
            "pre_transcode_censor": True,
            "censor_method": "duck",
            "censor_segments": [
                {"start_seconds": 1.0, "end_seconds": 1.5}
            ],
        }

        with patch(
            "workers.handlers._profile_setting", side_effect=self._profile_setting
        ):
            filter_args, filtered_codec_args, _segments = _ffmpeg_censor_filter_plan(
                profile_id="default",
                payload=payload,
                media_kind="video",
                input_count=1,
                codec_args=codec_args,
            )

        self.assertEqual(filter_args[0], "-filter:a")
        self.assertEqual(
            filter_args[1],
            "loudnorm=I=-14:TP=-1.5:LRA=11,"
            "volume=0.0:enable='between(t\\,1.0\\,1.5)'",
        )
        self.assertNotIn("-af", filtered_codec_args)
        self.assertNotIn("old-filter", filtered_codec_args)

    def test_clean_pre_transcode_video_still_uses_configured_filter(self):
        with patch(
            "workers.handlers._profile_setting", side_effect=self._profile_setting
        ):
            filter_args, codec_args, segments = _ffmpeg_censor_filter_plan(
                profile_id="default",
                payload={"pre_transcode_censor": True},
                media_kind="video",
                input_count=1,
                codec_args=["-c:a", "aac"],
            )

        self.assertEqual(
            filter_args,
            ["-filter:a", "loudnorm=I=-14:TP=-1.5:LRA=11"],
        )
        self.assertEqual(codec_args, ["-c:a", "aac"])
        self.assertEqual(segments, [])


class PostDownloadCensorFilterCompositionTests(unittest.TestCase):
    def _run_censor(self, method: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            media_path = root / "video.mp4"
            media_path.write_bytes(b"original-video")
            job = SimpleNamespace(
                id=17,
                profile_id="default",
                payload={
                    "download_id": 23,
                    "media_path": str(media_path),
                    "censor_method": method,
                    "segments": [
                        {"start_seconds": 0.25, "end_seconds": 0.75}
                    ],
                },
            )
            download = SimpleNamespace(title="Video", save=Mock())
            queryset = Mock()
            queryset.first.return_value = download
            command: list[str] = []

            def profile_setting(
                _profile_id: str, key: str, default: str
            ) -> str:
                values = {
                    "ffmpeg_path": "configured-ffmpeg",
                    "ffmpeg_audio_filter": "loudnorm=I=-14:TP=-1.5:LRA=11",
                }
                return values.get(key, default)

            def run_ffmpeg(args, **_kwargs):
                command.extend(str(value) for value in args)
                Path(str(args[-1])).write_bytes(b"censored-video")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch(
                    "workers.handlers.Download.objects.filter",
                    return_value=queryset,
                ),
                patch("workers.handlers._touch_active_job"),
                patch("workers.handlers._download_output_root", return_value=root),
                patch(
                    "workers.handlers._profile_setting",
                    side_effect=profile_setting,
                ),
                patch("workers.handlers.subprocess.run", side_effect=run_ffmpeg),
                patch("workers.handlers._validate_media_output") as validate,
            ):
                censor_audio(job)

            validate.assert_called_once()
            self.assertEqual(media_path.read_bytes(), b"censored-video")
            download.save.assert_called_once()
            return command

    def test_post_download_duck_composes_configured_filter_and_copies_video(self):
        command = self._run_censor("duck")

        filter_index = command.index("-filter:a")
        self.assertEqual(
            command[filter_index + 1],
            "loudnorm=I=-14:TP=-1.5:LRA=11,"
            "volume=0.0:enable='between(t\\,0.25\\,0.75)'",
        )
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[0], "configured-ffmpeg")

    def test_post_download_beep_composes_configured_filter_and_copies_video(self):
        command = self._run_censor("beep")

        graph = command[command.index("-filter_complex") + 1]
        self.assertIn(
            "[0:a:0]loudnorm=I=-14:TP=-1.5:LRA=11,volume=0",
            graph,
        )
        self.assertIn("adelay=250|250", graph)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertIn("[censored_audio]", command)


if __name__ == "__main__":
    unittest.main()
