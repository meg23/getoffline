import threading
from pathlib import Path

from workers.logger import get_logger

log = get_logger("transcription")

_WHISPER_MODEL_CACHE = {}
_TRANSCRIPTION_CACHE = {}
_TRANSCRIPTION_CACHE_LOCK = threading.Lock()
_WHISPER_MODEL_LOCK = threading.Lock()


class TranscriptionError(RuntimeError):
    """Raised when Whisper transcription cannot be completed for a media file."""


def _normalize_faster_whisper_result(segments_iterable, *, log_prefix: str = "transcription"):
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


def _transcribe_in_process(input_file: Path, model_name: str, language: str = None, log_prefix: str = "transcription"):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is required for transcription.") from exc

    with _WHISPER_MODEL_LOCK:
        model = _WHISPER_MODEL_CACHE.get(model_name)
        if model is None:
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _WHISPER_MODEL_CACHE[model_name] = model

    transcribe_kwargs = {"vad_filter": True}
    if language:
        transcribe_kwargs["language"] = language
    try:
        segments, _info = model.transcribe(str(input_file), **transcribe_kwargs)
    except IndexError as exc:
        if "tuple index out of range" in str(exc):
            raise RuntimeError(f"No decodable audio stream found in media file: {input_file}") from exc
        raise
    return _normalize_faster_whisper_result(segments, log_prefix=log_prefix)


def transcribe_with_whisper(
    input_file: Path, model_name: str, log_prefix: str, language: str = None, mode: str = "in_process"
):
    input_file = Path(input_file).resolve()
    cache_key = (str(input_file), input_file.stat().st_mtime_ns, model_name, language)
    with _TRANSCRIPTION_CACHE_LOCK:
        cached = _TRANSCRIPTION_CACHE.get(cache_key)
    if cached is not None:
        log.info("Reusing cached transcription for %s: %s (%s)", log_prefix, input_file.name, model_name)
        return cached

    try:
        if mode != "in_process":
            log.info("Ignoring deprecated transcription mode=%s; using native in-process transcription", mode)
        result = _transcribe_in_process(input_file, model_name, language=language, log_prefix=log_prefix)
    except Exception as exc:
        raise TranscriptionError(
            f"Transcription failed for {input_file.name} ({model_name}): {exc}"
        ) from exc

    with _TRANSCRIPTION_CACHE_LOCK:
        _TRANSCRIPTION_CACHE[cache_key] = result
    return result
