import math
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from workers.logger import get_logger

log = get_logger("transcription")

_WHISPER_MODEL_CACHE = {}
_TRANSCRIPTION_CACHE = {}
_TRANSCRIPTION_CACHE_LOCK = threading.Lock()
_WHISPER_MODEL_LOCK = threading.Lock()


class TranscriptionError(RuntimeError):
    """Raised when Whisper transcription cannot be completed for a media file."""


def _env_float(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError:
        log.warning("Ignoring invalid %s=%r; using %s", name, raw_value, default)
        return default


def _probe_audio_duration(input_file: Path) -> float | None:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(input_file),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        log.warning("Unable to probe audio duration for %s: %s", input_file, exc)
        return None
    try:
        duration = float(completed.stdout.strip())
    except ValueError:
        log.warning(
            "Unable to parse ffprobe duration for %s: %r", input_file, completed.stdout
        )
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    return duration


def _extract_audio_chunk(
    input_file: Path, output_file: Path, start: float, duration: float
) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(input_file),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "wav",
            str(output_file),
        ],
        check=True,
    )


def _offset_transcription_result(result: dict, offset_seconds: float) -> dict:
    offset_segments = []
    for segment in result.get("segments", []):
        offset_segment = dict(segment)
        offset_segment["start"] = (
            float(offset_segment.get("start", 0.0)) + offset_seconds
        )
        offset_segment["end"] = float(offset_segment.get("end", 0.0)) + offset_seconds
        offset_segments.append(offset_segment)
    return {"text": result.get("text", ""), "segments": offset_segments}


def _normalize_faster_whisper_result(
    segments_iterable, *, log_prefix: str = "transcription"
):
    segments = []
    text_parts = []
    last_end = 0.0
    for index, segment in enumerate(segments_iterable, start=1):
        segment_text = (segment.text or "").strip()
        segment_start = float(segment.start)
        segment_end = float(segment.end)
        last_end = max(last_end, segment_end)
        if segment_text:
            text_parts.append(segment_text)
        segments.append(
            {
                "start": segment_start,
                "end": segment_end,
                "text": segment_text,
            }
        )
        if index == 1 or index % 10 == 0:
            log.info(
                "Whisper transcription progress prefix=%s segments=%s latest_start=%.2fs latest_end=%.2fs text_chars=%s",
                log_prefix,
                index,
                segment_start,
                segment_end,
                sum(len(part) for part in text_parts),
            )
    log.info(
        "Whisper transcription complete prefix=%s segments=%s duration_seen=%.2fs text_chars=%s",
        log_prefix,
        len(segments),
        last_end,
        sum(len(part) for part in text_parts),
    )
    return {"text": " ".join(text_parts).strip(), "segments": segments}


def _transcribe_in_process(
    input_file: Path,
    model_name: str,
    language: str = None,
    log_prefix: str = "transcription",
):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is required for transcription.") from exc

    with _WHISPER_MODEL_LOCK:
        model = _WHISPER_MODEL_CACHE.get(model_name)
        if model is None:
            cache_dir = Path(
                os.getenv("GETOFFLINE_MODEL_CACHE_DIR")
                or os.getenv("HF_HOME")
                or "/app/model-cache"
            ).expanduser()
            cache_dir.mkdir(parents=True, exist_ok=True)
            load_started_at = time.monotonic()
            log.info(
                "Loading faster-whisper model=%s cache_dir=%s", model_name, cache_dir
            )
            model = WhisperModel(
                model_name,
                device="cpu",
                compute_type="int8",
                download_root=str(cache_dir),
            )
            _WHISPER_MODEL_CACHE[model_name] = model
            log.info(
                "Loaded faster-whisper model=%s elapsed_seconds=%.2f",
                model_name,
                time.monotonic() - load_started_at,
            )
        else:
            log.info("Using cached faster-whisper model=%s", model_name)

    transcribe_kwargs = {"vad_filter": True}
    if language:
        transcribe_kwargs["language"] = language
    input_size = input_file.stat().st_size if input_file.exists() else None
    duration_seconds = _probe_audio_duration(input_file)
    chunk_threshold_seconds = _env_float(
        "GETOFFLINE_TRANSCRIPTION_CHUNK_THRESHOLD_SECONDS", 30 * 60
    )
    chunk_seconds = _env_float("GETOFFLINE_TRANSCRIPTION_CHUNK_SECONDS", 15 * 60)
    if (
        duration_seconds is not None
        and chunk_seconds > 0
        and duration_seconds > chunk_threshold_seconds
    ):
        log.info(
            "Transcribing long audio in chunks prefix=%s input=%s duration=%.2fs chunk_seconds=%.2fs model=%s language=%s size_bytes=%s kwargs=%s",
            log_prefix,
            input_file,
            duration_seconds,
            chunk_seconds,
            model_name,
            language,
            input_size,
            transcribe_kwargs,
        )
        all_text_parts = []
        all_segments = []
        chunk_count = int(math.ceil(duration_seconds / chunk_seconds))
        with tempfile.TemporaryDirectory(prefix="getoffline-transcription-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            for chunk_index in range(chunk_count):
                start = chunk_index * chunk_seconds
                remaining = max(duration_seconds - start, 0)
                current_duration = min(chunk_seconds, remaining)
                if current_duration <= 0:
                    break
                chunk_file = tmpdir_path / f"chunk-{chunk_index:05d}.wav"
                log.info(
                    "Extracting transcription chunk prefix=%s chunk=%s/%s start=%.2fs duration=%.2fs",
                    log_prefix,
                    chunk_index + 1,
                    chunk_count,
                    start,
                    current_duration,
                )
                _extract_audio_chunk(input_file, chunk_file, start, current_duration)
                chunk_result = _transcribe_audio_file(
                    model,
                    chunk_file,
                    transcribe_kwargs,
                    f"{log_prefix}-chunk-{chunk_index + 1}",
                )
                offset_result = _offset_transcription_result(chunk_result, start)
                if offset_result["text"]:
                    all_text_parts.append(offset_result["text"])
                all_segments.extend(offset_result["segments"])
                chunk_file.unlink(missing_ok=True)
        log.info(
            "Chunked faster-whisper transcription complete prefix=%s chunks=%s segments=%s text_chars=%s",
            log_prefix,
            chunk_count,
            len(all_segments),
            sum(len(part) for part in all_text_parts),
        )
        return {"text": " ".join(all_text_parts).strip(), "segments": all_segments}

    log.info(
        "Starting faster-whisper transcription prefix=%s input=%s model=%s language=%s size_bytes=%s duration=%s kwargs=%s",
        log_prefix,
        input_file,
        model_name,
        language,
        input_size,
        duration_seconds,
        transcribe_kwargs,
    )
    return _transcribe_audio_file(model, input_file, transcribe_kwargs, log_prefix)


def _transcribe_audio_file(
    model, input_file: Path, transcribe_kwargs: dict, log_prefix: str
):
    try:
        segments, info = model.transcribe(str(input_file), **transcribe_kwargs)
    except IndexError as exc:
        if "tuple index out of range" in str(exc):
            raise RuntimeError(
                f"No decodable audio stream found in media file: {input_file}"
            ) from exc
        raise
    log.info(
        "faster-whisper transcription iterator ready prefix=%s detected_language=%s language_probability=%s duration=%s duration_after_vad=%s",
        log_prefix,
        getattr(info, "language", None),
        getattr(info, "language_probability", None),
        getattr(info, "duration", None),
        getattr(info, "duration_after_vad", None),
    )
    return _normalize_faster_whisper_result(segments, log_prefix=log_prefix)


def transcribe_with_whisper(
    input_file: Path,
    model_name: str,
    log_prefix: str,
    language: str = None,
    mode: str = "in_process",
):
    input_file = Path(input_file).resolve()
    cache_key = (str(input_file), input_file.stat().st_mtime_ns, model_name, language)
    with _TRANSCRIPTION_CACHE_LOCK:
        cached = _TRANSCRIPTION_CACHE.get(cache_key)
    if cached is not None:
        log.info(
            "Reusing cached transcription for %s: %s (%s)",
            log_prefix,
            input_file.name,
            model_name,
        )
        return cached

    try:
        if mode != "in_process":
            log.info(
                "Ignoring deprecated transcription mode=%s; using native in-process transcription",
                mode,
            )
        result = _transcribe_in_process(
            input_file, model_name, language=language, log_prefix=log_prefix
        )
    except Exception as exc:
        log.exception(
            "Whisper transcription failed prefix=%s input=%s model=%s language=%s mode=%s",
            log_prefix,
            input_file,
            model_name,
            language,
            mode,
        )
        raise TranscriptionError(
            f"Transcription failed for {input_file.name} ({model_name}): {exc}"
        ) from exc

    with _TRANSCRIPTION_CACHE_LOCK:
        _TRANSCRIPTION_CACHE[cache_key] = result
    return result
