"""Audio profanity censoring via FFmpeg filters based on Whisper SRT timestamps."""

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from profanityfilter import ProfanityFilter


@dataclass(frozen=True)
class AudioSegment:
    """Represents a censurable audio segment with profanity timestamp."""

    start_seconds: float
    end_seconds: float
    text: str


_PROFANITY_FILTER = ProfanityFilter()

_SRT_METADATA_RE = re.compile(
    r"^(?:\d+|\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}.*)$"
)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")


def _srt_timestamp_to_seconds(timestamp_str: str) -> float:
    """Convert SRT timestamp (HH:MM:SS,mmm or HH:MM:SS.mmm) to seconds (float)."""
    # Replace comma with period for consistency
    timestamp_str = timestamp_str.strip().replace(",", ".")

    # Parse HH:MM:SS.mmm format
    parts = timestamp_str.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid SRT timestamp format: {timestamp_str}")

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds_and_millis = parts[2].split(".")
        seconds = int(seconds_and_millis[0])
        millis = int(seconds_and_millis[1]) if len(seconds_and_millis) > 1 else 0
        return hours * 3600 + minutes * 60 + seconds + millis / 1000
    except (ValueError, IndexError) as e:
        raise ValueError(f"Invalid SRT timestamp format: {timestamp_str}") from e


def _parse_srt_block(block: str) -> tuple[float, float, str] | None:
    """Parse a single SRT block and return (start_seconds, end_seconds, text) or None."""
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    # Find the timestamp line (usually line 1, but be flexible)
    ts_index = None
    for i, line in enumerate(lines):
        if "-->" in line:
            ts_index = i
            break

    if ts_index is None:
        return None

    try:
        ts_line = lines[ts_index]
        parts = ts_line.split("-->")
        if len(parts) != 2:
            return None
        start_str = parts[0].strip()
        end_str = parts[1].strip()
        start_seconds = _srt_timestamp_to_seconds(start_str)
        end_seconds = _srt_timestamp_to_seconds(end_str)
    except (ValueError, IndexError):
        return None

    text = " ".join(lines[ts_index + 1 :]).strip()
    if not text:
        return None

    return start_seconds, end_seconds, text


def _is_sentence_profane(sentence: str) -> bool:
    """Check if a sentence contains profanity using profanityfilter."""
    try:
        return bool(_PROFANITY_FILTER.is_profane(sentence))
    except Exception:  # noqa: BLE001
        return False


def _redact_word(value: str) -> str:
    return _WORD_RE.sub("****", value)


def censor_segments_from_transcription(
    result: dict,
    *,
    padding_ms: int = 150,
    duration_seconds: float | None = None,
) -> tuple[list[AudioSegment], dict]:
    """Return precise profanity intervals and a redacted transcription result.

    A profanity-positive segment without usable word timings is rejected so a
    caller operating fail-closed cannot accidentally publish uncensored media.
    """
    redacted = deepcopy(result)
    intervals: list[AudioSegment] = []
    padding = max(0, min(1000, int(padding_ms))) / 1000.0
    for segment in redacted.get("segments", []):
        text = str(segment.get("text") or "")
        if not _is_sentence_profane(text):
            continue
        words = segment.get("words")
        if not isinstance(words, list) or not words:
            raise ValueError("Profanity detected without word timestamps")
        found = False
        for word in words:
            if not isinstance(word, dict):
                continue
            value = str(word.get("word") or "")
            token_match = _WORD_RE.search(value)
            if token_match is None or not _is_sentence_profane(token_match.group(0)):
                continue
            try:
                start = float(word["start"])
                end = float(word["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Profanity word has invalid timestamps") from exc
            if not math.isfinite(start) or not math.isfinite(end) or end <= start:
                raise ValueError("Profanity word has invalid timestamps")
            start = max(0.0, start - padding)
            end += padding
            if duration_seconds is not None and math.isfinite(duration_seconds):
                end = min(end, duration_seconds)
            if end <= start:
                raise ValueError("Profanity interval is outside media duration")
            intervals.append(AudioSegment(start, end, ""))
            word["word"] = _redact_word(value)
            found = True
        if not found:
            raise ValueError("Profanity detected but no timed profanity word matched")
        segment["text"] = "".join(
            str(word.get("word") or "") for word in words if isinstance(word, dict)
        ).strip()
    redacted["text"] = " ".join(
        str(segment.get("text") or "").strip()
        for segment in redacted.get("segments", [])
        if str(segment.get("text") or "").strip()
    )
    return _merge_overlapping_segments(intervals), redacted


def extract_profanity_segments(subtitle_path: Path) -> list[AudioSegment]:
    """Parse SRT file and return audio segments containing profanity.

    Args:
        subtitle_path: Path to SRT subtitle file

    Returns:
        List of AudioSegment with start/end times and profane text
    """
    if not Path(subtitle_path).exists():
        return []

    if Path(subtitle_path).suffix.lower() != ".srt":
        return []

    segments = []
    text = Path(subtitle_path).read_text(encoding="utf-8", errors="replace")

    # Split by double newlines to get SRT blocks
    for block in re.split(r"\n\s*\n", text.strip()):
        parsed = _parse_srt_block(block)
        if parsed is None:
            continue

        start_seconds, end_seconds, text_content = parsed

        # Split text into sentences for granular profanity detection
        sentences = [
            s.strip()
            for s in _SENTENCE_BOUNDARY_RE.split(text_content)
            if s.strip()
        ]
        if not sentences:
            sentences = [text_content]

        # Check each sentence; if any are profane, mark the entire segment
        has_profanity = any(_is_sentence_profane(s) for s in sentences)
        if has_profanity:
            segments.append(
                AudioSegment(
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    text=text_content,
                )
            )

    return segments


def _merge_overlapping_segments(
    segments: list[AudioSegment],
) -> list[AudioSegment]:
    """Merge overlapping or adjacent audio segments.

    Args:
        segments: List of segments (must be sorted by start_seconds)

    Returns:
        List of merged segments without overlaps
    """
    if not segments:
        return []

    sorted_segments = sorted(segments, key=lambda s: s.start_seconds)
    merged = [sorted_segments[0]]

    for current in sorted_segments[1:]:
        last = merged[-1]
        # Merge if segments overlap or touch (with 0.1s grace period)
        if current.start_seconds <= last.end_seconds + 0.1:
            merged_segment = AudioSegment(
                start_seconds=last.start_seconds,
                end_seconds=max(current.end_seconds, last.end_seconds),
                text=f"{last.text} {current.text}",
            )
            merged[-1] = merged_segment
        else:
            merged.append(current)

    return merged


def build_duck_filter(
    segments: list[AudioSegment],
    volume_level: float = 0.0,
) -> str | None:
    """Generate FFmpeg audio filter for ducking (muting) profane segments.

    Uses FFmpeg's 'volume' filter with 'enable' expression to mute audio
    during specified time ranges.

    Args:
        segments: List of AudioSegment with profanity
        volume_level: Volume level for muted segments (0.0 = silent, default)

    Returns:
        FFmpeg -af filter string, or None if no segments
    """
    if not segments:
        return None

    merged = _validated_segments(segments)

    # Build enable expression: between(t,start,end) OR between(t,start,end) OR ...
    # Commas in FFmpeg filter expressions must be escaped with backslash
    conditions = [
        f"between(t\\,{seg.start_seconds}\\,{seg.end_seconds})"
        for seg in merged
    ]
    enable_expr = "+".join(conditions)

    # FFmpeg audio filter: volume=0:enable='expression'
    # Use single quotes to avoid shell interpretation issues
    return f"volume={volume_level}:enable='{enable_expr}'"


def compose_audio_filters(*filters: str | None) -> str | None:
    """Compose simple FFmpeg audio filter chains in their configured order."""
    configured = [str(value).strip() for value in filters if str(value or "").strip()]
    return ",".join(configured) or None


def build_beep_filter(
    segments: list[AudioSegment],
    frequency: int = 1000,
    *,
    input_label: str = "0:a:0",
    output_label: str = "censored_audio",
    source_filter: str | None = None,
) -> str | None:
    """Generate FFmpeg audio filter for beeping profane segments.

    Creates delayed sine tones and mixes them over a source muted only during
    the profanity intervals.

    Args:
        segments: List of AudioSegment with profanity
        frequency: Beep frequency in Hz (default 1000)
        input_label: FFmpeg label for the source audio stream
        output_label: FFmpeg label assigned to the censored result
        source_filter: Existing simple audio filter chain applied before censoring

    Returns:
        FFmpeg -filter_complex string, or None if no segments
    """
    if not segments:
        return None

    merged = _validated_segments(segments)
    silence_expr = "+".join(
        f"between(t\\,{seg.start_seconds}\\,{seg.end_seconds})" for seg in merged
    )
    muted_filter = compose_audio_filters(
        source_filter, f"volume=0:enable='{silence_expr}'"
    )
    if muted_filter is None:  # pragma: no cover - volume filter is always present
        raise RuntimeError("Could not build muted source filter")
    parts = [f"[{input_label}]{muted_filter}[censor_muted]"]
    beep_labels = []
    for index, seg in enumerate(merged):
        duration = seg.end_seconds - seg.start_seconds
        delay_ms = round(seg.start_seconds * 1000)
        label = f"censor_beep_{index}"
        parts.append(
            f"sine=f={int(frequency)}:d={duration},adelay={delay_ms}|{delay_ms}[{label}]"
        )
        beep_labels.append(f"[{label}]")
    inputs = "[censor_muted]" + "".join(beep_labels)
    parts.append(
        f"{inputs}amix=inputs={len(beep_labels) + 1}:duration=first:dropout_transition=0[{output_label}]"
    )
    return ";".join(parts)


def _validated_segments(segments: list[AudioSegment]) -> list[AudioSegment]:
    valid = []
    for segment in segments:
        start = float(segment.start_seconds)
        end = float(segment.end_seconds)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("Invalid censor interval")
        valid.append(AudioSegment(start, end, ""))
    return _merge_overlapping_segments(valid)
