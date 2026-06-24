import os
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
    log.info(
        "Starting faster-whisper transcription prefix=%s input=%s model=%s language=%s size_bytes=%s kwargs=%s",
        log_prefix,
        input_file,
        model_name,
        language,
        input_size,
        transcribe_kwargs,
    )
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
