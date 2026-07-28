"""Audio profanity censoring with FFmpeg filters based on Whisper transcripts."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from workers.logger import get_logger

log = get_logger("censor")


@dataclass(frozen=True)
class AudioSegment:
    """Represents an audio segment to be censored."""

    start_seconds: float
    end_seconds: float
    text: str

    @property
    def duration_seconds(self) -> float:
        """Return duration of this segment."""
        return self.end_seconds - self.start_seconds


def _parse_srt_timestamp(timestamp_str: str) -> float:
    """Convert SRT timestamp (HH:MM:SS,mmm) to seconds.

    Args:
        timestamp_str: SRT timestamp like "00:00:15,200" or "00:00:15.200"

    Returns:
        Time in seconds as float
    """
    # Normalize both comma and period as decimal separator
    timestamp = timestamp_str.replace(",", ".")
    parts = timestamp.split(":")
    if len(parts) != 3:
        raise ValueError(f"Invalid SRT timestamp format: {timestamp_str}")

    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _extract_srt_blocks(srt_content: str) -> list[tuple[str, str, str, str]]:
    """Extract SRT blocks as (index, start_time, end_time, text) tuples.

    Uses a line-based state parser to handle edge cases like empty cues,
    various line endings, and optional SRT cue settings.

    Args:
        srt_content: Raw SRT file content

    Returns:
        List of (index, start_time, end_time, text) tuples
    """
    # Normalize line endings
    content = srt_content.replace("\r\n", "\n").replace("\r", "\n")
    lines = content.split("\n")

    blocks = []
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Skip empty lines
        if not line:
            i += 1
            continue

        # Try to parse as cue index (should be numeric)
        try:
            index = int(line)
        except ValueError:
            # Not a cue index, skip this line
            i += 1
            continue

        # Next line should be timecode
        i += 1
        if i >= len(lines):
            break

        timecode_line = lines[i].strip()

        # Parse timecode: HH:MM:SS,mmm --> HH:MM:SS,mmm [optional settings]
        timecode_match = re.match(
            r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[,.]\d{3})",
            timecode_line,
        )
        if not timecode_match:
            i += 1
            continue

        start_time = timecode_match.group(1)
        end_time = timecode_match.group(2)

        # Collect cue text (can be multiple lines until empty line or EOF)
        text_lines = []
        i += 1
        while i < len(lines):
            text_line = lines[i].rstrip()
            # Stop at empty line
            if not text_line.strip():
                i += 1
                break
            text_lines.append(text_line.strip())
            i += 1

        # Join text lines with spaces, normalize whitespace
        text = " ".join(text_lines)
        text = " ".join(text.split())  # Normalize multiple spaces

        # Only add non-empty blocks
        if text:
            blocks.append((str(index), start_time, end_time, text))

    return blocks


def extract_profanity_segments(
    subtitle_path: Path, profane_texts: list[str]
) -> list[AudioSegment]:
    """Extract audio segments containing profanity from SRT subtitle file.

    Args:
        subtitle_path: Path to SRT subtitle file from Whisper
        profane_texts: List of profane sentence texts to match

    Returns:
        List of AudioSegment objects with start/end times and text
    """
    subtitle_path = Path(subtitle_path).expanduser().resolve()

    if not subtitle_path.exists():
        log.warning("Subtitle file not found: %s", subtitle_path)
        return []

    try:
        srt_content = subtitle_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.error("Failed to read subtitle file %s: %s", subtitle_path, exc)
        return []

    if not srt_content.strip():
        log.warning("Subtitle file is empty: %s", subtitle_path)
        return []

    try:
        blocks = _extract_srt_blocks(srt_content)
    except Exception as exc:
        log.error("Failed to parse SRT blocks from %s: %s", subtitle_path, exc)
        return []

    if not blocks:
        log.warning("No SRT blocks found in %s", subtitle_path)
        return []

    segments = []
    for index, start_str, end_str, text in blocks:
        try:
            start_seconds = _parse_srt_timestamp(start_str)
            end_seconds = _parse_srt_timestamp(end_str)

            # Normalize and check if text contains any profane sentence
            normalized_block_text = text.lower()
            for profane_text in profane_texts:
                normalized_profane_text = profane_text.strip().lower()
                # Use substring matching instead of exact match
                if normalized_profane_text in normalized_block_text:
                    segment = AudioSegment(
                        start_seconds=start_seconds,
                        end_seconds=end_seconds,
                        text=text,
                    )
                    segments.append(segment)
                    log.info(
                        "Found profanity segment in SRT index=%s start=%.2f end=%.2f text=%r",
                        index,
                        start_seconds,
                        end_seconds,
                        text,
                    )
                    break

        except (ValueError, IndexError) as exc:
            log.warning(
                "Failed to parse SRT block index=%s: %s", index, exc, exc_info=False
            )
            continue

    log.info(
        "Extracted %d profanity segments from %s", len(segments), subtitle_path
    )
    return segments


def _merge_overlapping_segments(
    segments: list[AudioSegment], gap_threshold: float = 0.5
) -> list[AudioSegment]:
    """Merge overlapping or closely-spaced segments.

    Args:
        segments: List of audio segments
        gap_threshold: Minimum gap in seconds between segments (smaller gaps merged)

    Returns:
        Merged list of segments
    """
    if not segments:
        return []

    # Sort by start time
    sorted_segments = sorted(segments, key=lambda s: s.start_seconds)
    merged = [sorted_segments[0]]

    for current in sorted_segments[1:]:
        last = merged[-1]
        gap = current.start_seconds - last.end_seconds

        if gap <= gap_threshold:
            # Merge: extend end time and concatenate text
            merged_segment = AudioSegment(
                start_seconds=last.start_seconds,
                end_seconds=max(last.end_seconds, current.end_seconds),
                text=f"{last.text} {current.text}",
            )
            merged[-1] = merged_segment
            log.info(
                "Merged overlapping segments: [%.2f-%.2f] + [%.2f-%.2f] -> [%.2f-%.2f]",
                last.start_seconds,
                last.end_seconds,
                current.start_seconds,
                current.end_seconds,
                merged_segment.start_seconds,
                merged_segment.end_seconds,
            )
        else:
            merged.append(current)

    return merged


def build_duck_filter(segments: list[AudioSegment]) -> str | None:
    """Generate FFmpeg audio filter to mute (duck) profane segments.

    Uses volume filter with conditional enabling based on timestamps.
    Format: volume=0:enable='between(t,start,end)+between(t,start2,end2)+...'

    Args:
        segments: List of audio segments to censor

    Returns:
        FFmpeg filter string for -af option, or None if no segments
    """
    if not segments:
        return None

    # Merge segments to avoid overlaps/gaps
    merged = _merge_overlapping_segments(segments)

    # Build enable expression: between(t,start,end)+between(t,start2,end2)+...
    # The '+' operator in FFmpeg filter expressions means logical OR
    enable_conditions = []
    for segment in merged:
        condition = f"between(t,{segment.start_seconds:.3f},{segment.end_seconds:.3f})"
        enable_conditions.append(condition)

    enable_expr = "+".join(enable_conditions)

    # FFmpeg filter to set volume to 0 when condition is true
    filter_str = f"volume=0:enable='{enable_expr}'"

    log.info(
        "Generated duck filter for %d segments: %s",
        len(merged),
        filter_str,
    )

    return filter_str


def build_beep_filter(
    segments: list[AudioSegment],
    beep_frequency: int = 1000,
    beep_amplitude: float = 0.5,
) -> str | None:
    """Generate FFmpeg audio filter to overlay beep tones on profane segments.

    Uses sine source to generate beep tones that overlay the muted original audio.

    Args:
        segments: List of audio segments to censor
        beep_frequency: Frequency of beep in Hz (default 1000)
        beep_amplitude: Amplitude of beep 0.0-1.0 (default 0.5)

    Returns:
        FFmpeg filter_complex string, or None if no segments
    """
    if not segments:
        return None

    merged = _merge_overlapping_segments(segments)

    # Build enable expression: between(t,start,end)+between(t,start2,end2)+...
    enable_conditions = []
    for segment in merged:
        condition = f"between(t,{segment.start_seconds:.3f},{segment.end_seconds:.3f})"
        enable_conditions.append(condition)

    enable_expr = "+".join(enable_conditions)

    # Build complete filter graph:
    # 1. Mute the original audio during profane segments
    # 2. Generate beep tones from sine source
    # 3. Mix the muted audio with the beep

    filter_graph = (
        f"[0:a]volume=0:enable='{enable_expr}'[muted];"
        f"sine=frequency={beep_frequency}:sample_rate=48000,"
        f"volume={beep_amplitude}:enable='{enable_expr}'[beep];"
        f"[muted][beep]amix=inputs=2:duration=first:normalize=0[outa]"
    )

    log.info(
        "Generated beep filter for %d segments at %dHz: %s",
        len(merged),
        beep_frequency,
        filter_graph,
    )

    return filter_graph


def build_censor_filter(
    segments: list[AudioSegment],
    method: Literal["duck", "beep"] = "duck",
    beep_frequency: int = 1000,
    beep_amplitude: float = 0.5,
) -> str | None:
    """Generate FFmpeg audio filter for profanity censoring.

    Args:
        segments: List of audio segments to censor
        method: Censoring method - "duck" (mute) or "beep" (tone overlay)
        beep_frequency: Frequency of beep tone in Hz (only for beep method)
        beep_amplitude: Amplitude of beep 0.0-1.0 (only for beep method)

    Returns:
        FFmpeg filter string ready for -af or -filter_complex option
    """
    if not segments:
        log.warning("No segments provided for censoring filter")
        return None

    if method == "duck":
        return build_duck_filter(segments)
    elif method == "beep":
        return build_beep_filter(segments, beep_frequency, beep_amplitude)
    else:
        log.error("Unknown censoring method: %s", method)
        return None
