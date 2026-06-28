import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workers import transcription  # noqa: E402


class FakeSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


class TranscriptionChunkingTests(unittest.TestCase):
    def test_long_audio_is_transcribed_in_bounded_chunks_with_offsets(self):
        calls = []

        class FakeModel:
            def transcribe(self, audio, **kwargs):
                calls.append((Path(audio).name, dict(kwargs)))
                chunk_number = len(calls)
                return (
                    [FakeSegment(1.0, 2.5, f"chunk {chunk_number}")],
                    SimpleNamespace(
                        language="en",
                        language_probability=1.0,
                        duration=10.0,
                        duration_after_vad=9.0,
                    ),
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            input_file = Path(tmpdir) / "episode.mp3"
            input_file.write_bytes(b"audio")

            fake_module = types.SimpleNamespace(
                WhisperModel=lambda *args, **kwargs: FakeModel()
            )
            with patch.dict(sys.modules, {"faster_whisper": fake_module}), patch.object(
                transcription, "_WHISPER_MODEL_CACHE", {}
            ), patch.object(
                transcription, "_probe_audio_duration", return_value=25.0
            ), patch.object(
                transcription,
                "_extract_audio_chunk",
                side_effect=lambda _src, dst, _start, _duration: dst.write_bytes(
                    b"chunk"
                ),
            ), patch.dict(
                os.environ,
                {
                    "GETOFFLINE_TRANSCRIPTION_CHUNK_THRESHOLD_SECONDS": "10",
                    "GETOFFLINE_TRANSCRIPTION_CHUNK_SECONDS": "10",
                },
            ):
                result = transcription._transcribe_in_process(
                    input_file, "base", language="en", log_prefix="test"
                )

        self.assertEqual(
            [segment["text"] for segment in result["segments"]],
            ["chunk 1", "chunk 2", "chunk 3"],
        )
        self.assertEqual(
            [segment["start"] for segment in result["segments"]], [1.0, 11.0, 21.0]
        )
        self.assertEqual(
            [segment["end"] for segment in result["segments"]], [2.5, 12.5, 22.5]
        )
        self.assertEqual(result["text"], "chunk 1 chunk 2 chunk 3")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(kwargs["language"] == "en" for _, kwargs in calls))


if __name__ == "__main__":
    unittest.main()
