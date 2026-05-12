import threading
from pathlib import Path

from logger import get_logger

log = get_logger("transcription")

_WHISPER_MODEL_CACHE = {}
_TRANSCRIPTION_CACHE = {}
_TRANSCRIPTION_CACHE_LOCK = threading.Lock()
_WHISPER_MODEL_LOCK = threading.Lock()


class TranscriptionError(RuntimeError):
    """Raised when Whisper transcription cannot be completed for a media file."""


def _normalize_faster_whisper_result(segments_iterable):
    segments = []
    text_parts = []
    for segment in segments_iterable:
        segment_text = (segment.text or "").strip()
        if segment_text:
            text_parts.append(segment_text)
        segments.append(
            {
                "start": float(segment.start),
                "end": float(segment.end),
                "text": segment_text,
            }
        )
    return {"text": " ".join(text_parts).strip(), "segments": segments}


def transcribe_with_whisper(input_file: Path, model_name: str, log_prefix: str, language: str = None):
    input_file = Path(input_file).resolve()
    cache_key = (str(input_file), input_file.stat().st_mtime_ns, model_name, language)
    with _TRANSCRIPTION_CACHE_LOCK:
        cached = _TRANSCRIPTION_CACHE.get(cache_key)
    if cached is not None:
        log.info("Reusing cached transcription for %s: %s (%s)", log_prefix, input_file.name, model_name)
        return cached

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is required for transcription.") from exc

    with _WHISPER_MODEL_LOCK:
        model = _WHISPER_MODEL_CACHE.get(model_name)
        if model is None:
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            _WHISPER_MODEL_CACHE[model_name] = model

    try:
        transcribe_kwargs = {"vad_filter": True}
        if language:
            transcribe_kwargs["language"] = language
        segments, _info = model.transcribe(str(input_file), **transcribe_kwargs)
        result = _normalize_faster_whisper_result(segments)
    except Exception as exc:
        raise TranscriptionError(
            f"Transcription failed for {input_file.name} ({model_name}): {exc}"
        ) from exc

    with _TRANSCRIPTION_CACHE_LOCK:
        _TRANSCRIPTION_CACHE[cache_key] = result
    return result
