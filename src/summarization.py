import gc
import json
import re
import subprocess
import sys
import threading
import traceback
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib import error, request

from logger import get_logger

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "he", "in", "is", "it", "its",
    "of", "on", "that", "the", "to", "was", "were", "will", "with", "you", "your", "we", "they", "this", "those",
    "these", "or", "if", "but",
}


DEFAULT_OLLAMA_MODEL = "qwen2.5:0.5b"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
log = get_logger("summarization")
_MODEL_READY_LOCK = threading.Lock()
_MODEL_READY = False


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_sentences(text: str) -> List[str]:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?])\s+", normalized)
    sentences: List[str] = []
    for part in parts:
        stripped_part = part.strip()
        if stripped_part:
            sentences.append(stripped_part)
    return sentences


def _extractive_summary(text: str, max_sentences: int = 2, max_chars: int = 280) -> str:
    sentences = _split_sentences(text)
    if not sentences:
        return "No transcript content available yet."
    words = re.findall(r"[a-zA-Z']+", text.lower())
    filtered_words: List[str] = []
    for word in words:
        if word not in STOP_WORDS and len(word) > 2:
            filtered_words.append(word)
    freq = Counter(filtered_words)
    scored = []
    for idx, sentence in enumerate(sentences):
        tokens = re.findall(r"[a-zA-Z']+", sentence.lower())
        score = 0
        for token in tokens:
            score += int(freq.get(token, 0))
        scored.append((score, idx, sentence))
    def _score_key(item: tuple) -> tuple:
        return (-item[0], item[1])

    top = sorted(scored, key=_score_key)[:max_sentences]
    def _order_key(item: tuple) -> int:
        return int(item[1])

    ordered = sorted(top, key=_order_key)
    ordered_sentences: List[str] = []
    for ordered_item in ordered:
        ordered_sentences.append(str(ordered_item[2]))
    summary = " ".join(ordered_sentences).strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary


def _truncate_for_prompt(text: str, max_chars: int = 6000) -> str:
    stripped = re.sub(r"\s+", " ", text).strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars].rstrip() + "…"


def _ollama_summary(text: str, model_name: str, url: str = DEFAULT_OLLAMA_URL) -> str:
    prompt = (
        "Return strict JSON: {\"summary\": \"...\"}. "
        "Write a concise 1-2 sentence paraphrased summary (max 220 chars). "
        "Focus on topic + takeaway. Avoid filler, quotes, transcript-style wording, and any ad/promotional language. "
        "Never mention sponsors, products, offers, discounts, or marketing claims.\n\n"
        f"Transcript:\n{_truncate_for_prompt(text)}"
    )
    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"num_predict": 120, "temperature": 0.2},
    }
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(body)
        raw_response = str(parsed.get("response") or "").strip()
        parsed_response = json.loads(raw_response) if raw_response.startswith("{") else {"summary": raw_response}
        response_text = str(parsed_response.get("summary") or "").strip()
        if response_text:
            response_text = re.sub(r"\s+", " ", response_text)
            if len(response_text) > 280:
                response_text = response_text[:279].rstrip() + "…"
            return response_text
    except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        log.warning("Ollama summary request failed model=%s error=%s", model_name, exc)
        raise RuntimeError(f"ollama summary request failed: {exc}") from exc
    raise RuntimeError("ollama summary response did not include a usable summary")


def ensure_local_summary_model(model_name: str = DEFAULT_OLLAMA_MODEL, ollama_path: str = "ollama") -> bool:
    global _MODEL_READY
    with _MODEL_READY_LOCK:
        if _MODEL_READY:
            return True
        try:
            check = subprocess.run([ollama_path, "list"], capture_output=True, text=True, check=False)
            if check.returncode != 0:
                log.warning("Ollama not ready for summaries (list failed): %s", (check.stderr or check.stdout).strip())
                return False
            if model_name not in (check.stdout or ""):
                log.info("Downloading local summary model via Ollama: %s", model_name)
                pull = subprocess.run([ollama_path, "pull", model_name], capture_output=True, text=True, check=False)
                if pull.returncode != 0:
                    log.warning("Failed to download Ollama summary model %s: %s", model_name, (pull.stderr or pull.stdout).strip())
                    return False
                log.info("Downloaded Ollama summary model: %s", model_name)
            _MODEL_READY = True
            return True
        except FileNotFoundError:
            log.warning("Ollama CLI not installed; summary generation will use extractive fallback.")
            return False


def summarize_segments(segments: List[str], model_name: str = DEFAULT_OLLAMA_MODEL, mode: str = "subprocess") -> Dict[str, str]:
    cleaned_segments: List[str] = []
    for segment in segments:
        cleaned_segment = str(segment or "").strip()
        if cleaned_segment:
            cleaned_segments.append(cleaned_segment)
    joined_text = " ".join(cleaned_segments)
    model_ready = ensure_local_summary_model(model_name=model_name)
    if not model_ready:
        log.debug("Summary model readiness check failed model=%s; Ollama may still be tried.", model_name)
    if mode == "in_process":
        llm_summary = _ollama_summary(joined_text, model_name=model_name)
        return {"summary_text": llm_summary, "model_name": model_name, "updated_at": _utcnow_iso()}
    payload = {"text": joined_text, "model_name": model_name}
    cmd = [sys.executable, "-m", "summarization", "--worker", json.dumps(payload)]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        log.error("Summary subprocess failed model=%s error=%s", model_name, details)
        raise RuntimeError(f"summary subprocess failed: {details}")
    result = json.loads(completed.stdout)
    used_model = str(result.get("model_name") or "unknown")
    if used_model == "extractive-local":
        log.warning("Summary subprocess used extractive fallback requested_model=%s transcript_chars=%s", model_name, len(joined_text))
    return result


def _worker_once(text: str, model_name: str = DEFAULT_OLLAMA_MODEL) -> Dict[str, str]:
    try:
        summary = _ollama_summary(text, model_name=model_name)
        return {"summary_text": summary, "model_name": model_name, "updated_at": _utcnow_iso()}
    finally:
        gc.collect()



if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        try:
            args = json.loads(sys.argv[2])
            result = _worker_once(
                text=str(args.get("text") or ""),
                model_name=str(args.get("model_name") or DEFAULT_OLLAMA_MODEL),
            )
            sys.stdout.write(json.dumps(result))
        except Exception:
            sys.stderr.write(traceback.format_exc())
            sys.exit(1)
