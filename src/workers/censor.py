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


def _extract_srt_blocks(srt_content: str) -> list[tuple[str, str, str]]:
    """Extract SRT blocks as (index, timecode, text) tuples.

    Args:
        srt_content: Raw SRT file content

    Returns:
        List of (index, timecode, text) tuples
    """
    # Pattern: index (number), timecode (HH:MM:SS,mmm --> HH:MM:SS,mmm), text
    srt_block_pattern = re.compile(
        r"(\d+)\s+(\d{2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[.,]\d{3})\s+(.*?)(?=\n\n|\Z)",
        re.DOTALL,
    )

    blocks = []
    for match in srt_block_pattern.finditer(srt_content):
        index = match.group(1)
        start_time = match.group(2)
        end_time = match.group(3)
        text = match.group(4).strip()

        # Clean up text (remove extra whitespace, join lines)
        text = " ".join(text.split())

        if text:  # Only include non-empty blocks
            blocks.append((index, start_time, end_time, text))

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

            # Check if text matches any profane sentence
            for profane_text in profane_texts:
                if profane_text.strip().lower() == text.lower():
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

    Uses atone to generate a 1000Hz beep, volume filter to mute original audio,
    and amix to combine them.

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

    # Generate beep definitions for each segment
    # Each segment gets a unique beep that starts and ends exactly at segment boundaries
    beep_segments = []
    for i, segment in enumerate(merged):
        duration = segment.duration_seconds
        beep_segments.append(
            f"atone=f={beep_frequency}:d={duration:.3f}:a={beep_amplitude}[beep{i}]"
        )

    # Build the mute conditions (same as duck filter)
    enable_conditions = []
    for segment in merged:
        condition = f"between(t,{segment.start_seconds:.3f},{segment.end_seconds:.3f})"
        enable_conditions.append(condition)
    enable_expr = "+".join(enable_conditions)

    # Mute the original audio during profane segments
    mute_filter = f"[0:a]volume=0:enable='{enable_expr}'[muted]"

    # Concatenate beep generation filters
    # This is complex because we need to generate multiple beeps synchronized to the timeline
    # For now, we use a simpler approach: generate one composite beep and mix it

    # Alternative approach: Use anullsrc to generate silence, then use aformat to match audio
    # Then overlay the muted audio with clicks at the profane segments

    # Simplified approach: Generate a single beep filter that covers all segments
    total_duration = merged[-1].end_seconds if merged else 0
    if total_duration == 0:
        return None

    # Create enable expression for beep generation to align with profane segments
    beep_enable = enable_expr

    # Generate beep tone synchronized to profane segments
    # anullsrc generates silence, we then apply tone during profane times
    beep_definition = f"[0:a]atone=f={beep_frequency}:d={total_duration:.3f}:a={beep_amplitude},volume=0:enable='{beep_enable}'[beep]"

    # Mix muted original with beep tone
    filter_complex = f"{mute_filter};[muted][beep]amix=inputs=2:duration=first[out]"

    log.info(
        "Generated beep filter for %d segments at %dHz: %s",
        len(merged),
        beep_frequency,
        filter_complex,
    )

    return filter_complex


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
