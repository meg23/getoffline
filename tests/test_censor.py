import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workers.censor import (
    AudioSegment,
    _extract_srt_blocks,
    _merge_overlapping_segments,
    _parse_srt_timestamp,
    build_beep_filter,
    build_censor_filter,
    build_duck_filter,
    extract_profanity_segments,
)


class SRTTimestampParsingTests(unittest.TestCase):
    """Tests for SRT timestamp parsing."""

    def test_parses_srt_timestamp_with_comma(self):
        """Test parsing SRT timestamp with comma decimal separator."""
        result = _parse_srt_timestamp("00:00:15,200")
        self.assertAlmostEqual(result, 15.2, places=2)

    def test_parses_srt_timestamp_with_period(self):
        """Test parsing SRT timestamp with period decimal separator."""
        result = _parse_srt_timestamp("00:00:15.200")
        self.assertAlmostEqual(result, 15.2, places=2)

    def test_parses_srt_timestamp_with_hours_and_minutes(self):
        """Test parsing SRT timestamp with hours and minutes."""
        result = _parse_srt_timestamp("01:23:45,678")
        self.assertAlmostEqual(result, 1 * 3600 + 23 * 60 + 45.678, places=2)

    def test_parses_srt_timestamp_zero(self):
        """Test parsing SRT timestamp at zero."""
        result = _parse_srt_timestamp("00:00:00,000")
        self.assertAlmostEqual(result, 0.0, places=2)

    def test_raises_on_invalid_timestamp_format(self):
        """Test that invalid timestamp format raises ValueError."""
        with self.assertRaises(ValueError):
            _parse_srt_timestamp("invalid")

        with self.assertRaises(ValueError):
            _parse_srt_timestamp("00:00")


class SRTBlockExtractionTests(unittest.TestCase):
    """Tests for SRT block extraction."""

    def test_extracts_single_srt_block(self):
        """Test extracting a single SRT block."""
        srt_content = """1
00:00:00,000 --> 00:00:03,500
Welcome to the podcast."""

        blocks = _extract_srt_blocks(srt_content)
        self.assertEqual(len(blocks), 1)
        index, start, end, text = blocks[0]
        self.assertEqual(index, "1")
        self.assertEqual(start, "00:00:00,000")
        self.assertEqual(end, "00:00:03,500")
        self.assertEqual(text, "Welcome to the podcast.")

    def test_extracts_multiple_srt_blocks(self):
        """Test extracting multiple SRT blocks."""
        srt_content = """1
00:00:00,000 --> 00:00:03,500
Welcome to the podcast.

2
00:00:04,200 --> 00:00:08,900
Today we're discussing something fucking important.

3
00:00:09,100 --> 00:00:12,600
Let's dive right in."""

        blocks = _extract_srt_blocks(srt_content)
        self.assertEqual(len(blocks), 3)

        # Check second block (the profane one)
        index, start, end, text = blocks[1]
        self.assertEqual(index, "2")
        self.assertEqual(text, "Today we're discussing something fucking important.")

    def test_normalizes_whitespace_in_blocks(self):
        """Test that multi-line text in SRT blocks is normalized."""
        srt_content = """1
00:00:00,000 --> 00:00:03,500
This is
a multi-line
subtitle block."""

        blocks = _extract_srt_blocks(srt_content)
        self.assertEqual(len(blocks), 1)
        _, _, _, text = blocks[0]
        self.assertEqual(text, "This is a multi-line subtitle block.")

    def test_skips_empty_text_blocks(self):
        """Test that empty blocks are skipped."""
        srt_content = """1
00:00:00,000 --> 00:00:03,500


2
00:00:04,200 --> 00:00:08,900
Valid text here."""

        blocks = _extract_srt_blocks(srt_content)
        self.assertEqual(len(blocks), 1)
        _, _, _, text = blocks[0]
        self.assertEqual(text, "Valid text here.")


class AudioSegmentTests(unittest.TestCase):
    """Tests for AudioSegment dataclass."""

    def test_segment_duration_calculation(self):
        """Test that segment duration is calculated correctly."""
        segment = AudioSegment(start_seconds=10.0, end_seconds=15.0, text="test")
        self.assertEqual(segment.duration_seconds, 5.0)

    def test_segment_is_frozen(self):
        """Test that AudioSegment is immutable."""
        segment = AudioSegment(start_seconds=10.0, end_seconds=15.0, text="test")
        with self.assertRaises(AttributeError):
            segment.start_seconds = 11.0


class ExtractProfanitySegmentsTests(unittest.TestCase):
    """Tests for profanity segment extraction."""

    def test_extracts_profanity_segments_from_srt(self):
        """Test extracting profanity segments from SRT file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_path = Path(tmpdir) / "test.srt"
            subtitle_path.write_text(
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

            segments = extract_profanity_segments(
                subtitle_path, ["That was fucking ridiculous."]
            )

            self.assertEqual(len(segments), 1)
            segment = segments[0]
            self.assertAlmostEqual(segment.start_seconds, 4.2, places=1)
            self.assertAlmostEqual(segment.end_seconds, 8.9, places=1)
            self.assertEqual(segment.text, "That was fucking ridiculous.")

    def test_handles_missing_subtitle_file(self):
        """Test handling of missing subtitle file."""
        missing_path = Path("/nonexistent/path/test.srt")
        segments = extract_profanity_segments(missing_path, ["profanity"])
        self.assertEqual(segments, [])

    def test_handles_empty_subtitle_file(self):
        """Test handling of empty subtitle file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_path = Path(tmpdir) / "empty.srt"
            subtitle_path.write_text("", encoding="utf-8")

            segments = extract_profanity_segments(subtitle_path, ["profanity"])
            self.assertEqual(segments, [])

    def test_no_match_for_different_profanity_text(self):
        """Test that unmatched profanity text is not extracted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_path = Path(tmpdir) / "test.srt"
            subtitle_path.write_text(
                """1
00:00:04,200 --> 00:00:08,900
That was fucking ridiculous.""",
                encoding="utf-8",
            )

            segments = extract_profanity_segments(subtitle_path, ["different profanity"])
            self.assertEqual(segments, [])

    def test_case_insensitive_matching(self):
        """Test that profanity matching is case-insensitive."""
        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_path = Path(tmpdir) / "test.srt"
            subtitle_path.write_text(
                """1
00:00:04,200 --> 00:00:08,900
THAT WAS FUCKING RIDICULOUS.""",
                encoding="utf-8",
            )

            segments = extract_profanity_segments(
                subtitle_path, ["that was fucking ridiculous."]
            )
            self.assertEqual(len(segments), 1)


class MergeOverlappingSegmentsTests(unittest.TestCase):
    """Tests for segment merging."""

    def test_merges_overlapping_segments(self):
        """Test that overlapping segments are merged."""
        segments = [
            AudioSegment(start_seconds=10.0, end_seconds=15.0, text="first"),
            AudioSegment(start_seconds=14.0, end_seconds=20.0, text="second"),
        ]

        merged = _merge_overlapping_segments(segments)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].start_seconds, 10.0)
        self.assertEqual(merged[0].end_seconds, 20.0)

    def test_merges_closely_spaced_segments(self):
        """Test that segments within gap threshold are merged."""
        segments = [
            AudioSegment(start_seconds=10.0, end_seconds=15.0, text="first"),
            AudioSegment(start_seconds=15.2, end_seconds=20.0, text="second"),
        ]

        merged = _merge_overlapping_segments(segments, gap_threshold=0.5)
        self.assertEqual(len(merged), 1)

    def test_keeps_distant_segments_separate(self):
        """Test that distant segments are not merged."""
        segments = [
            AudioSegment(start_seconds=10.0, end_seconds=15.0, text="first"),
            AudioSegment(start_seconds=20.0, end_seconds=25.0, text="second"),
        ]

        merged = _merge_overlapping_segments(segments, gap_threshold=0.5)
        self.assertEqual(len(merged), 2)

    def test_sorts_segments_by_start_time(self):
        """Test that segments are sorted before merging."""
        segments = [
            AudioSegment(start_seconds=20.0, end_seconds=25.0, text="second"),
            AudioSegment(start_seconds=10.0, end_seconds=15.0, text="first"),
        ]

        merged = _merge_overlapping_segments(segments)
        self.assertEqual(merged[0].start_seconds, 10.0)
        self.assertEqual(merged[1].start_seconds, 20.0)


class BuildDuckFilterTests(unittest.TestCase):
    """Tests for duck (mute) filter generation."""

    def test_generates_duck_filter_for_single_segment(self):
        """Test generating duck filter for single segment."""
        segment = AudioSegment(
            start_seconds=15.2, end_seconds=18.5, text="profanity"
        )
        filter_str = build_duck_filter([segment])

        self.assertIsNotNone(filter_str)
        self.assertIn("volume=0", filter_str)
        self.assertIn("between(t,15.200,18.500)", filter_str)

    def test_generates_duck_filter_for_multiple_segments(self):
        """Test generating duck filter for multiple segments."""
        segments = [
            AudioSegment(start_seconds=15.2, end_seconds=18.5, text="first"),
            AudioSegment(start_seconds=42.1, end_seconds=45.3, text="second"),
        ]
        filter_str = build_duck_filter(segments)

        self.assertIsNotNone(filter_str)
        self.assertIn("volume=0", filter_str)
        self.assertIn("between(t,15.200,18.500)", filter_str)
        self.assertIn("between(t,42.100,45.300)", filter_str)
        # Multiple conditions joined with +
        self.assertIn("+", filter_str)

    def test_returns_none_for_empty_segments(self):
        """Test that None is returned for empty segment list."""
        filter_str = build_duck_filter([])
        self.assertIsNone(filter_str)


class BuildBeepFilterTests(unittest.TestCase):
    """Tests for beep filter generation."""

    def test_generates_beep_filter_for_single_segment(self):
        """Test generating beep filter for single segment."""
        segment = AudioSegment(
            start_seconds=15.2, end_seconds=18.5, text="profanity"
        )
        filter_str = build_beep_filter([segment])

        self.assertIsNotNone(filter_str)
        self.assertIn("atone=f=1000", filter_str)
        self.assertIn("amix", filter_str)

    def test_respects_beep_frequency_parameter(self):
        """Test that custom beep frequency is used."""
        segment = AudioSegment(
            start_seconds=15.2, end_seconds=18.5, text="profanity"
        )
        filter_str = build_beep_filter([segment], beep_frequency=2000)

        self.assertIsNotNone(filter_str)
        self.assertIn("atone=f=2000", filter_str)

    def test_respects_beep_amplitude_parameter(self):
        """Test that custom beep amplitude is used."""
        segment = AudioSegment(
            start_seconds=15.2, end_seconds=18.5, text="profanity"
        )
        filter_str = build_beep_filter([segment], beep_amplitude=0.8)

        self.assertIsNotNone(filter_str)
        self.assertIn("a=0.8", filter_str)

    def test_returns_none_for_empty_segments(self):
        """Test that None is returned for empty segment list."""
        filter_str = build_beep_filter([])
        self.assertIsNone(filter_str)


class BuildCensorFilterTests(unittest.TestCase):
    """Tests for generic censor filter generation."""

    def test_builds_duck_filter_by_default(self):
        """Test that duck filter is built by default."""
        segment = AudioSegment(
            start_seconds=15.2, end_seconds=18.5, text="profanity"
        )
        filter_str = build_censor_filter([segment])

        self.assertIsNotNone(filter_str)
        self.assertIn("volume=0", filter_str)

    def test_builds_duck_filter_when_requested(self):
        """Test building duck filter explicitly."""
        segment = AudioSegment(
            start_seconds=15.2, end_seconds=18.5, text="profanity"
        )
        filter_str = build_censor_filter([segment], method="duck")

        self.assertIsNotNone(filter_str)
        self.assertIn("volume=0", filter_str)

    def test_builds_beep_filter_when_requested(self):
        """Test building beep filter explicitly."""
        segment = AudioSegment(
            start_seconds=15.2, end_seconds=18.5, text="profanity"
        )
        filter_str = build_censor_filter([segment], method="beep")

        self.assertIsNotNone(filter_str)
        self.assertIn("atone", filter_str)

    def test_returns_none_for_unknown_method(self):
        """Test that unknown method returns None."""
        segment = AudioSegment(
            start_seconds=15.2, end_seconds=18.5, text="profanity"
        )
        filter_str = build_censor_filter([segment], method="unknown")

        self.assertIsNone(filter_str)

    def test_returns_none_for_empty_segments(self):
        """Test that None is returned for empty segment list."""
        filter_str = build_censor_filter([], method="duck")
        self.assertIsNone(filter_str)


if __name__ == "__main__":
    unittest.main()
