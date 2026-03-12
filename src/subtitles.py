import re
import shutil
import tempfile
from pathlib import Path

from logger import get_logger
from transcription import transcribe_with_whisper

log = get_logger("subtitles")


def _find_existing_english_subtitle(media_file: Path):
    subtitle_path = media_file.with_suffix(".srt")
    if subtitle_path.exists():
        return subtitle_path

    pattern = f"{media_file.stem}*.en*.srt"
    candidates = sorted(media_file.parent.glob(pattern))
    if not candidates:
        return None

    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(candidates[0], subtitle_path)
    log.info("Reused downloaded English subtitle: %s -> %s", candidates[0].name, subtitle_path.name)
    return subtitle_path


def _parse_srt_timestamp(value: str) -> float:
    hours, minutes, seconds_millis = value.split(":")
    seconds, millis = seconds_millis.split(",")
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000.0
    )


def _format_srt_timestamp(value: float) -> str:
    value = max(0.0, value)
    hours = int(value // 3600)
    value -= hours * 3600
    minutes = int(value // 60)
    value -= minutes * 60
    seconds = int(value)
    millis = int(round((value - seconds) * 1000))

    if millis == 1000:
        millis = 0
        seconds += 1
    if seconds == 60:
        seconds = 0
        minutes += 1
    if minutes == 60:
        minutes = 0
        hours += 1

    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _shift_srt_timestamps(srt_path: Path, offset_seconds: float):
    if abs(offset_seconds) < 1e-6:
        return

    lines = srt_path.read_text(encoding="utf-8", errors="replace").splitlines()
    shifted = []
    timestamp_re = re.compile(
        r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})(.*)$"
    )

    for line in lines:
        match = timestamp_re.match(line)
        if not match:
            shifted.append(line)
            continue

        start_raw, end_raw, tail = match.groups()
        start = _parse_srt_timestamp(start_raw) + offset_seconds
        end = _parse_srt_timestamp(end_raw) + offset_seconds

        start = max(0.0, start)
        end = max(start + 0.01, end)

        shifted.append(
            f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}{tail}"
        )

    srt_path.write_text("\n".join(shifted) + "\n", encoding="utf-8")


def generate_whisper_subtitles(input_file: Path, settings: dict, subtitle_path: Path = None):
    input_file = Path(input_file)
    subtitle_path = Path(subtitle_path) if subtitle_path else input_file.with_suffix(".srt")
    failed_marker_path = subtitle_path.with_suffix(f"{subtitle_path.suffix}.failed")

    if subtitle_path.exists() and subtitle_path.stat().st_mtime >= input_file.stat().st_mtime:
        log.info("Subtitle generation skipped (already up to date): %s", subtitle_path.name)
        return subtitle_path

    if failed_marker_path.exists() and failed_marker_path.stat().st_mtime >= input_file.stat().st_mtime:
        log.info(
            "Subtitle generation skipped after previous known Whisper failure: %s",
            failed_marker_path.name,
        )
        return None

    try:
        from whisper.utils import get_writer
    except ImportError as exc:
        raise RuntimeError("openai-whisper is required for subtitle generation.") from exc

    model_name = settings.get("subtitle_model", settings.get("model", "base"))
    subtitle_language = settings.get("subtitle_language", "en")
    log.info("Generating subtitles: %s (%s, language=%s)", input_file.name, model_name, subtitle_language)
    try:
        result = transcribe_with_whisper(input_file, model_name, "subtitle-generation", language=subtitle_language)
    except Exception as exc:
        error_message = str(exc)
        if "cannot reshape tensor of 0 elements" in error_message:
            failed_marker_path.parent.mkdir(parents=True, exist_ok=True)
            failed_marker_path.write_text(
                f"Known Whisper empty-audio failure for {input_file.name}\n{error_message}\n",
                encoding="utf-8",
            )
            log.warning(
                "Skipping subtitles for %s due to known Whisper empty-audio failure; marker created: %s",
                input_file,
                failed_marker_path,
            )
            return None
        raise

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        temp_stem = "subtitle_output"
        writer = get_writer("srt", str(tmp_dir_path))
        writer(result, temp_stem)

        generated_subtitle_path = tmp_dir_path / f"{temp_stem}.srt"
        if not generated_subtitle_path.exists():
            srt_candidates = sorted(tmp_dir_path.glob("*.srt"))
            if srt_candidates:
                generated_subtitle_path = srt_candidates[0]
            else:
                raise RuntimeError(f"Whisper did not produce subtitle file in {tmp_dir_path}")

        subtitle_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated_subtitle_path, subtitle_path)

    if not subtitle_path.exists():
        raise RuntimeError(f"Subtitle output file was not created: {subtitle_path}")

    if failed_marker_path.exists():
        failed_marker_path.unlink()

    subtitle_offset = float(settings.get("subtitle_time_offset_seconds", 0.0))
    _shift_srt_timestamps(subtitle_path, subtitle_offset)

    log.info("Subtitles generated: %s (offset: %.3fs)", subtitle_path.name, subtitle_offset)
    return subtitle_path


def create_subtitles(
    media_file,
    scrubber_cfg: dict,
    subtitle_offset_seconds,
    entry_subtitles_enabled: bool,
    logger,
    context_name: str,
    context_label: str,
    skip_subtitles_after_scrub_failure: bool = False,
):
    if entry_subtitles_enabled and skip_subtitles_after_scrub_failure:
        logger.warning(
            "Skipping subtitle generation for %s because transcription failed during ad scrub",
            media_file,
        )
        return None

    if entry_subtitles_enabled and media_file.exists():
        try:
            subtitle_settings = dict(scrubber_cfg)
            subtitle_settings.setdefault("subtitle_language", "en")
            if subtitle_offset_seconds is not None:
                subtitle_settings["subtitle_time_offset_seconds"] = float(subtitle_offset_seconds)

            subtitle_path = _find_existing_english_subtitle(media_file)
            if subtitle_path is None:
                subtitle_path = generate_whisper_subtitles(media_file, subtitle_settings)
            if subtitle_path is None:
                return None
            logger.info("Generated %s subtitles: %s", context_label, subtitle_path.name)
            return subtitle_path
        except Exception as subtitle_exc:
            logger.warning("Subtitle generation failed for %s: %s", media_file, subtitle_exc)
            return None

    logger.info("Subtitles skipped for %s because subtitles are disabled", context_name)
    return None
