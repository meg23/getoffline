import json
import subprocess
import sys
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


def _transcribe_in_process(input_file: Path, model_name: str, language: str = None):
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
    return _normalize_faster_whisper_result(segments)


def _transcribe_in_subprocess(input_file: Path, model_name: str, language: str = None):
    payload = {"input_file": str(input_file), "model_name": model_name, "language": language}
    cmd = [sys.executable, "-m", "workers.transcription_worker", json.dumps(payload)]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"faster-whisper subprocess failed: {details}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("faster-whisper subprocess returned invalid JSON output") from exc


def transcribe_with_whisper(
    input_file: Path, model_name: str, log_prefix: str, language: str = None, mode: str = "subprocess"
):
    input_file = Path(input_file).resolve()
    cache_key = (str(input_file), input_file.stat().st_mtime_ns, model_name, language)
    with _TRANSCRIPTION_CACHE_LOCK:
        cached = _TRANSCRIPTION_CACHE.get(cache_key)
    if cached is not None:
        log.info("Reusing cached transcription for %s: %s (%s)", log_prefix, input_file.name, model_name)
        return cached

    try:
        if mode == "in_process":
            result = _transcribe_in_process(input_file, model_name, language=language)
        else:
            result = _transcribe_in_subprocess(input_file, model_name, language=language)
    except Exception as exc:
        raise TranscriptionError(
            f"Transcription failed for {input_file.name} ({model_name}): {exc}"
        ) from exc

    with _TRANSCRIPTION_CACHE_LOCK:
        _TRANSCRIPTION_CACHE[cache_key] = result
    return result
