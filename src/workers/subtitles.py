import re
from pathlib import Path

from workers.logger import get_logger
from workers.transcription import transcribe_with_whisper

log = get_logger("subtitles")


_SUBTITLE_SIDECAR_SUFFIXES = {
    ".srt", ".vtt", ".ass", ".ssa", ".lrc", ".ttml", ".srv1", ".srv2", ".srv3", ".json3"
}

_KNOWN_EMPTY_AUDIO_FAILURE_PATTERNS = (
    "cannot reshape tensor of 0 elements",
    "Output file does not contain any stream",
    "Stream map 'a' matches no streams",
    "No decodable audio stream found in media file",
)


def _cleanup_subtitle_sidecars(media_file: Path, keep_subtitle: Path):
    stem = media_file.stem
    parent = media_file.parent
    keep_subtitle = Path(keep_subtitle).resolve()

    for path in parent.glob(f"{stem}*.*"):
        suffix = path.suffix.lower()
        if suffix not in _SUBTITLE_SIDECAR_SUFFIXES:
            continue
        if path.resolve() == keep_subtitle:
            continue

        # Keep only the canonical subtitle sidecar matching the media basename.
        # Remove downloaded language variants like .en.srt / .en-orig.srt / .en.vtt.
        if path.name.startswith(f"{stem}."):
            try:
                path.unlink(missing_ok=True)
                log.info("Removed extra subtitle sidecar: %s", path.name)
            except Exception as cleanup_exc:
                log.warning("Could not remove subtitle sidecar %s: %s", path, cleanup_exc)


def _normalize_existing_sidecars_for_media(media_file: Path):
    subtitle_path = media_file.with_suffix(".srt")
    if subtitle_path.exists():
        _cleanup_subtitle_sidecars(media_file, subtitle_path)
        return subtitle_path

    return None


def cleanup_subtitle_sidecars_for_folder(folder: Path):
    folder = Path(folder)
    if not folder.exists():
        return

    media_exts = {".mp3", ".mp4", ".m4a", ".webm", ".wav", ".flac", ".ogg", ".opus"}
    for media_file in folder.iterdir():
        if not media_file.is_file() or media_file.suffix.lower() not in media_exts:
            continue
        _normalize_existing_sidecars_for_media(media_file)


def _find_existing_whisper_subtitle(media_file: Path):
    return _normalize_existing_sidecars_for_media(media_file)


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

    model_name = settings.get("subtitle_model", settings.get("model", "base"))
    subtitle_language = settings.get("subtitle_language", "en")
    transcription_mode = str(settings.get("subtitle_transcription_mode", "in_process")).strip().lower()
    if transcription_mode != "in_process":
        log.info("Ignoring deprecated subtitle transcription mode: %s; using in_process", transcription_mode)
        transcription_mode = "in_process"
    log.info(
        "Generating subtitles: %s (%s, language=%s, mode=%s)",
        input_file.name,
        model_name,
        subtitle_language,
        transcription_mode,
    )
    try:
        result = transcribe_with_whisper(
            input_file,
            model_name,
            "subtitle-generation",
            language=subtitle_language,
            mode=transcription_mode,
        )
    except Exception as exc:
        error_message = str(exc)
        if any(pattern in error_message for pattern in _KNOWN_EMPTY_AUDIO_FAILURE_PATTERNS):
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

    segments = result.get("segments", [])
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    with subtitle_path.open("w", encoding="utf-8") as srt_file:
        for index, segment in enumerate(segments, start=1):
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start + 0.01))
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            if end <= start:
                end = start + 0.01
            srt_file.write(f"{index}\n")
            srt_file.write(f"{_format_srt_timestamp(start)} --> {_format_srt_timestamp(end)}\n")
            srt_file.write(f"{text}\n\n")

    if not subtitle_path.exists():
        raise RuntimeError(f"Subtitle output file was not created: {subtitle_path}")

    if failed_marker_path.exists():
        failed_marker_path.unlink()

    subtitle_offset = float(settings.get("subtitle_time_offset_seconds", 0.0))
    _shift_srt_timestamps(subtitle_path, subtitle_offset)

    _cleanup_subtitle_sidecars(input_file, subtitle_path)
    log.info("Subtitles generated: %s (offset: %.3fs)", subtitle_path.name, subtitle_offset)
    return subtitle_path


def create_subtitles(
    media_file,
    subtitle_offset_seconds,
    entry_subtitles_enabled: bool,
    logger,
    context_name: str,
    context_label: str,
    subtitle_transcription_mode: str = "in_process",
):
    if entry_subtitles_enabled and media_file.exists():
        try:
            subtitle_settings = {"subtitle_language": "en"}
            if subtitle_offset_seconds is not None:
                subtitle_settings["subtitle_time_offset_seconds"] = float(subtitle_offset_seconds)
            if subtitle_transcription_mode:
                subtitle_settings["subtitle_transcription_mode"] = str(subtitle_transcription_mode)

            logger.info(
                "Subtitle generation requested context=%s label=%s media=%s offset=%s mode=%s",
                context_name,
                context_label,
                media_file,
                subtitle_settings.get("subtitle_time_offset_seconds"),
                subtitle_settings.get("subtitle_transcription_mode"),
            )
            subtitle_path = _find_existing_whisper_subtitle(media_file)
            reused_existing = subtitle_path is not None
            if subtitle_path is None:
                logger.info("No existing subtitle sidecar found; generating Whisper subtitles for %s", media_file)
                subtitle_path = generate_whisper_subtitles(media_file, subtitle_settings)
            if subtitle_path is None:
                logger.warning("Subtitle generation returned no subtitle path for %s", media_file)
                return None
            if reused_existing:
                logger.info("Reused existing %s subtitles: %s", context_label, subtitle_path.name)
            else:
                logger.info("Generated %s subtitles: %s", context_label, subtitle_path.name)
            return subtitle_path
        except Exception as subtitle_exc:
            logger.warning("Subtitle generation failed for %s: %s", media_file, subtitle_exc)
            return None

    if not media_file.exists():
        logger.warning("Subtitles skipped for %s because media file is missing: %s", context_name, media_file)
    else:
        logger.info("Subtitles skipped for %s because subtitles are disabled", context_name)
    return None
