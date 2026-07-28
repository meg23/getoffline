"""Unit tests for audio profanity censoring module."""

import tempfile
import unittest
from pathlib import Path

from workers.censor import (
    AudioSegment,
    build_beep_filter,
    build_duck_filter,
    extract_profanity_segments,
)


class TestSrtParsing(unittest.TestCase):
    """Test SRT file parsing and segment extraction."""

    def test_extract_profanity_segments_valid_srt(self):
        """Test parsing valid SRT with profanity."""
        srt_content = """1
00:00:00,000 --> 00:00:05,000
This is clean content

2
00:00:05,000 --> 00:00:10,000
This is fucking bad

3
00:00:10,000 --> 00:00:15,000
Another clean segment
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".srt", delete=False, encoding="utf-8"
        ) as f:
            f.write(srt_content)
            f.flush()
            srt_path = Path(f.name)

        try:
            segments = extract_profanity_segments(srt_path)
            self.assertIsInstance(segments, list)
            # Should have at least one segment with profanity
            self.assertTrue(
                any("fucking" in seg.text.lower() for seg in segments),
                "Should find profane segment",
            )
            # Check that profane segment has correct timing
            profane_seg = [s for s in segments if "fucking" in s.text.lower()][0]
            self.assertAlmostEqual(profane_seg.start_seconds, 5.0, places=2)
            self.assertAlmostEqual(profane_seg.end_seconds, 10.0, places=2)
        finally:
            srt_path.unlink()

    def test_extract_profanity_segments_all_clean(self):
        """Test SRT with no profanity returns empty list."""
        srt_content = """1
00:00:00,000 --> 00:00:05,000
This is clean content

2
00:00:05,000 --> 00:00:10,000
More clean content
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".srt", delete=False, encoding="utf-8"
        ) as f:
            f.write(srt_content)
            f.flush()
            srt_path = Path(f.name)

        try:
            segments = extract_profanity_segments(srt_path)
            self.assertEqual(len(segments), 0, "Should return empty list for clean SRT")
        finally:
            srt_path.unlink()

    def test_extract_profanity_segments_nonexistent_file(self):
        """Test handling of nonexistent SRT file."""
        segments = extract_profanity_segments(Path("/nonexistent/file.srt"))
        self.assertEqual(len(segments), 0)

    def test_extract_profanity_segments_wrong_extension(self):
        """Test that non-SRT files are ignored."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("some content")
            f.flush()
            txt_path = Path(f.name)

        try:
            segments = extract_profanity_segments(txt_path)
            self.assertEqual(len(segments), 0)
        finally:
            txt_path.unlink()

    def test_extract_profanity_segments_timestamp_variations(self):
        """Test SRT with different timestamp formats (comma vs period)."""
        # SRT format allows both comma and period for milliseconds
        srt_content = """1
00:00:15,200 --> 00:00:18,500
This shit is bad

2
00:00:42.100 --> 00:00:45.300
More profanity here
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".srt", delete=False, encoding="utf-8"
        ) as f:
            f.write(srt_content)
            f.flush()
            srt_path = Path(f.name)

        try:
            segments = extract_profanity_segments(srt_path)
            self.assertGreater(len(segments), 0)
            # Verify first segment timing (both formats should work)
            seg1 = [s for s in segments if s.start_seconds < 20][0]
            self.assertAlmostEqual(seg1.start_seconds, 15.2, places=1)
            self.assertAlmostEqual(seg1.end_seconds, 18.5, places=1)
        finally:
            srt_path.unlink()


class TestFilterGeneration(unittest.TestCase):
    """Test FFmpeg filter string generation."""

    def test_build_duck_filter_single_segment(self):
        """Test duck filter generation for single profanity segment."""
        segments = [AudioSegment(start_seconds=5.0, end_seconds=10.0, text="bad")]
        filter_str = build_duck_filter(segments)

        self.assertIsNotNone(filter_str)
        self.assertIn("volume=0", filter_str)
        self.assertIn("between(t,5", filter_str)
        self.assertIn("10", filter_str)
        self.assertIn("enable=", filter_str)

    def test_build_duck_filter_multiple_segments(self):
        """Test duck filter with multiple non-overlapping segments."""
        segments = [
            AudioSegment(start_seconds=5.0, end_seconds=8.0, text="bad1"),
            AudioSegment(start_seconds=15.0, end_seconds=18.0, text="bad2"),
        ]
        filter_str = build_duck_filter(segments)

        self.assertIsNotNone(filter_str)
        self.assertIn("between(t,5", filter_str)
        self.assertIn("between(t,15", filter_str)

    def test_build_duck_filter_overlapping_segments(self):
        """Test duck filter merges overlapping segments."""
        segments = [
            AudioSegment(start_seconds=5.0, end_seconds=10.0, text="bad1"),
            AudioSegment(start_seconds=8.0, end_seconds=12.0, text="bad2"),
        ]
        filter_str = build_duck_filter(segments)

        self.assertIsNotNone(filter_str)
        # Should merge to single segment: 5.0 - 12.0
        # Should only have one between() condition
        count = filter_str.count("between(t,")
        self.assertEqual(count, 1, "Should merge overlapping segments into one")

    def test_build_duck_filter_empty_segments(self):
        """Test duck filter with empty segment list returns None."""
        filter_str = build_duck_filter([])
        self.assertIsNone(filter_str)

    def test_build_duck_filter_custom_volume_level(self):
        """Test duck filter with custom volume level."""
        segments = [AudioSegment(start_seconds=5.0, end_seconds=10.0, text="bad")]
        filter_str = build_duck_filter(segments, volume_level=0.5)

        self.assertIsNotNone(filter_str)
        self.assertIn("volume=0.5", filter_str)

    def test_build_beep_filter_single_segment(self):
        """Test beep filter generation."""
        segments = [AudioSegment(start_seconds=5.0, end_seconds=10.0, text="bad")]
        filter_str = build_beep_filter(segments)

        self.assertIsNotNone(filter_str)
        self.assertIn("atone", filter_str)  # FFmpeg tone generator
        self.assertIn("amix", filter_str)  # Audio mixer
        self.assertIn("f=1000", filter_str)  # Default 1000 Hz

    def test_build_beep_filter_custom_frequency(self):
        """Test beep filter with custom frequency."""
        segments = [AudioSegment(start_seconds=5.0, end_seconds=10.0, text="bad")]
        filter_str = build_beep_filter(segments, frequency=2000)

        self.assertIsNotNone(filter_str)
        self.assertIn("f=2000", filter_str)

    def test_build_beep_filter_empty_segments(self):
        """Test beep filter with empty segment list returns None."""
        filter_str = build_beep_filter([])
        self.assertIsNone(filter_str)

    def test_build_beep_filter_custom_duration(self):
        """Test beep filter with custom beep duration."""
        segments = [AudioSegment(start_seconds=5.0, end_seconds=10.0, text="bad")]
        filter_str = build_beep_filter(segments, beep_duration_ms=1000)

        self.assertIsNotNone(filter_str)
        # Should have the custom duration value
        self.assertIn("1000", filter_str.replace("1000Hz", ""))  # Avoid frequency


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and boundary conditions."""

    def test_adjacent_segments_merge(self):
        """Test that adjacent segments within grace period are merged."""
        segments = [
            AudioSegment(start_seconds=5.0, end_seconds=10.0, text="bad1"),
            AudioSegment(start_seconds=10.05, end_seconds=15.0, text="bad2"),
        ]
        filter_str = build_duck_filter(segments)

        # Should merge into single segment due to 0.1s grace period
        count = filter_str.count("between(t,")
        self.assertEqual(count, 1, "Should merge adjacent segments")

    def test_far_apart_segments_separate(self):
        """Test that far-apart segments are not merged."""
        segments = [
            AudioSegment(start_seconds=5.0, end_seconds=10.0, text="bad1"),
            AudioSegment(start_seconds=20.0, end_seconds=25.0, text="bad2"),
        ]
        filter_str = build_duck_filter(segments)

        # Should keep as separate segments
        count = filter_str.count("between(t,")
        self.assertEqual(count, 2, "Should not merge far-apart segments")

    def test_very_short_segments(self):
        """Test handling of very short profanity segments."""
        segments = [AudioSegment(start_seconds=5.0, end_seconds=5.1, text="bad")]
        filter_str = build_duck_filter(segments)

        self.assertIsNotNone(filter_str)
        self.assertIn("between(t,5.0,5.1)", filter_str)

    def test_millisecond_precision(self):
        """Test that millisecond-precision timestamps are preserved."""
        segments = [AudioSegment(start_seconds=5.123, end_seconds=10.456, text="bad")]
        filter_str = build_duck_filter(segments)

        self.assertIsNotNone(filter_str)
        self.assertIn("5.123", filter_str)
        self.assertIn("10.456", filter_str)


if __name__ == "__main__":
    unittest.main()
