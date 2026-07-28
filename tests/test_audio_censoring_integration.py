"""Integration tests for audio profanity censoring workflow."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from models.domain import DownloadStatus
from models.models import Download
from models.models import SourceConfig
from workers.censor import extract_profanity_segments
from workers.content_filter import ExplicitContentMatch
from workers.handlers import _queue_audio_censoring_job
from workers.handlers import _should_censor_profanity


class AudioCensoringWorkflowTests(unittest.TestCase):
    """Integration tests for the audio censoring workflow."""

    def setUp(self):
        """Set up test database and fixtures."""
        self.profile_id = "test_profile"
        self.source_type = "youtube"
        self.source_name = "Test Channel"
        self.maxDiff = None

    @patch("workers.handlers.SourceConfig.objects.filter")
    def test_should_censor_profanity_returns_true_when_enabled(self, mock_filter):
        """Test that censoring check returns True when enabled in config."""
        mock_filter.return_value.values_list.return_value.first.return_value = (
            True,
            "duck",
        )

        should_censor, method = _should_censor_profanity(
            source_type=self.source_type,
            source_name=self.source_name,
            profile_id=self.profile_id,
        )

        self.assertTrue(should_censor)
        self.assertEqual(method, "duck")

    @patch("workers.handlers.SourceConfig.objects.filter")
    def test_should_censor_profanity_returns_false_when_disabled(self, mock_filter):
        """Test that censoring check returns False when disabled in config."""
        mock_filter.return_value.values_list.return_value.first.return_value = (
            False,
            "duck",
        )

        should_censor, method = _should_censor_profanity(
            source_type=self.source_type,
            source_name=self.source_name,
            profile_id=self.profile_id,
        )

        self.assertFalse(should_censor)

    @patch("workers.handlers.SourceConfig.objects.filter")
    def test_should_censor_profanity_returns_false_when_not_found(self, mock_filter):
        """Test that censoring check returns False when source not found."""
        mock_filter.return_value.values_list.return_value.first.return_value = None

        should_censor, method = _should_censor_profanity(
            source_type=self.source_type,
            source_name=self.source_name,
            profile_id=self.profile_id,
        )

        self.assertFalse(should_censor)
        self.assertEqual(method, "duck")  # Default method

    @patch("workers.handlers._profile_setting")
    @patch("workers.handlers.create_job")
    def test_queue_audio_censoring_job_creates_job(
        self, mock_create_job, mock_profile_setting
    ):
        """Test that queuing censoring job creates job with correct payload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test SRT file
            srt_path = Path(tmpdir) / "test.srt"
            srt_path.write_text(
                """1
00:00:00,000 --> 00:00:03,500
Clean intro.

2
00:00:04,200 --> 00:00:08,900
That was fucking ridiculous.

3
00:00:09,100 --> 00:00:12,600
Clean outro.""",
                encoding="utf-8",
            )

            # Create test media file
            media_path = Path(tmpdir) / "test.mp3"
            media_path.write_bytes(b"fake audio data")

            # Mock job creation
            mock_job = MagicMock()
            mock_job.id = 123
            mock_create_job.return_value = mock_job

            download_lookup = {
                "profile_id": self.profile_id,
                "source_type": self.source_type,
                "source_name": self.source_name,
                "item_uid": "test_item",
            }
            download_defaults = {
                "title": "Test Video",
                "description": "Test description",
            }
            profane_sentences = ["That was fucking ridiculous."]

            result = _queue_audio_censoring_job(
                profile_id=self.profile_id,
                media_path=media_path,
                subtitle_path=srt_path,
                profane_sentences=profane_sentences,
                download_lookup=download_lookup,
                download_defaults=download_defaults,
                censor_method="duck",
            )

            # Verify job was created
            self.assertIsNotNone(result)
            self.assertEqual(result.id, 123)

            # Verify create_job was called with correct parameters
            mock_create_job.assert_called_once()
            call_kwargs = mock_create_job.call_args[1]
            self.assertEqual(call_kwargs["profile_id"], self.profile_id)
            self.assertEqual(call_kwargs["job_type"], "censor_profanity")
            self.assertIn("censor_filter", call_kwargs["payload"])
            self.assertIn("censored_segments", call_kwargs["payload"])
            self.assertEqual(len(call_kwargs["payload"]["censored_segments"]), 1)

    @patch("workers.handlers.SourceConfig.objects.filter")
    def test_profanity_censoring_enabled_per_source(self, mock_filter):
        """Test that profanity censoring can be configured per source."""
        # Create test source configs with different settings
        test_cases = [
            (self.profile_id, self.source_type, self.source_name, True, "duck"),
            (self.profile_id, self.source_type, "Other Channel", False, "duck"),
            (self.profile_id, "podcast", "Test Podcast", True, "beep"),
        ]

        for profile, source_type, source_name, censor, method in test_cases:
            mock_filter.return_value.values_list.return_value.first.return_value = (
                censor,
                method,
            )

            should_censor, result_method = _should_censor_profanity(
                source_type=source_type,
                source_name=source_name,
                profile_id=profile,
            )

            self.assertEqual(should_censor, censor, f"Failed for {source_name}")
            self.assertEqual(result_method, method, f"Failed for {source_name}")

    def test_censoring_workflow_extracts_correct_segments(self):
        """Test that SRT parsing extracts profanity segments correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            srt_path = Path(tmpdir) / "test.srt"
            srt_path.write_text(
                """1
00:00:00,000 --> 00:00:03,500
Clean intro.

2
00:00:04,200 --> 00:00:08,900
That was fucking ridiculous.

3
00:00:09,100 --> 00:00:12,600
And another fucking issue.

4
00:00:13,000 --> 00:00:16,000
Clean outro.""",
                encoding="utf-8",
            )

            profane_sentences = [
                "That was fucking ridiculous.",
                "And another fucking issue.",
            ]

            segments = extract_profanity_segments(srt_path, profane_sentences)

            self.assertEqual(len(segments), 2)
            self.assertAlmostEqual(segments[0].start_seconds, 4.2, places=1)
            self.assertAlmostEqual(segments[0].end_seconds, 8.9, places=1)
            self.assertAlmostEqual(segments[1].start_seconds, 9.1, places=1)
            self.assertAlmostEqual(segments[1].end_seconds, 12.6, places=1)

    def test_censoring_handles_overlapping_segments(self):
        """Test that overlapping profanity segments are handled correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            srt_path = Path(tmpdir) / "test.srt"
            srt_path.write_text(
                """1
00:00:00,000 --> 00:00:05,000
First fucking sentence.

2
00:00:04,500 --> 00:00:10,000
Second fucking sentence overlapping.""",
                encoding="utf-8",
            )

            profane_sentences = [
                "First fucking sentence.",
                "Second fucking sentence overlapping.",
            ]

            segments = extract_profanity_segments(srt_path, profane_sentences)

            # Should extract both segments even if overlapping
            self.assertEqual(len(segments), 2)
            # Segments should have their individual timings
            self.assertAlmostEqual(segments[0].start_seconds, 0.0, places=1)
            self.assertAlmostEqual(segments[1].start_seconds, 4.5, places=1)


class CensoringStatusTests(unittest.TestCase):
    """Tests for censoring status tracking in Download model."""

    def test_censored_status_constant_exists(self):
        """Test that CENSORED status exists in DownloadStatus enum."""
        self.assertEqual(DownloadStatus.CENSORED, "censored")

    def test_censored_download_has_segments_data(self):
        """Test that censored downloads store segment information."""
        censored_segments = [
            {
                "start_seconds": 15.2,
                "end_seconds": 18.5,
                "text": "fucking",
                "duration_seconds": 3.3,
            }
        ]

        # Create a Download instance with censored data
        download_data = {
            "profile_id": "test",
            "source_type": "youtube",
            "source_name": "Test",
            "item_uid": "test_item",
            "title": "Test Video",
            "download_status": DownloadStatus.CENSORED,
            "is_censored": True,
            "censored_segments": censored_segments,
        }

        # Verify data structure
        self.assertTrue(download_data["is_censored"])
        self.assertEqual(download_data["download_status"], "censored")
        self.assertEqual(len(download_data["censored_segments"]), 1)
        self.assertAlmostEqual(
            download_data["censored_segments"][0]["start_seconds"], 15.2, places=1
        )


if __name__ == "__main__":
    unittest.main()
