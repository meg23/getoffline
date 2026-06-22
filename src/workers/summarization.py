import importlib.util
import json
import os
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List
from urllib import error, request

from workers.logger import get_logger

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "he", "in", "is", "it", "its",
    "of", "on", "that", "the", "to", "was", "were", "will", "with", "you", "your", "we", "they", "this", "those",
    "these", "or", "if", "but",
}


DEFAULT_SUMMARY_MODEL = "qwen2.5:0.5b"
DEFAULT_OLLAMA_MODEL = DEFAULT_SUMMARY_MODEL
DEFAULT_LLAMA_CPP_REPO_ID = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
DEFAULT_LLAMA_CPP_FILENAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
SUMMARY_BACKEND_ENV = "GETOFFLINE_SUMMARY_BACKEND"
OLLAMA_URL_ENV = "GETOFFLINE_OLLAMA_URL"
LLAMA_CPP_REPO_ENV = "GETOFFLINE_SUMMARY_LLAMA_CPP_REPO_ID"
LLAMA_CPP_FILENAME_ENV = "GETOFFLINE_SUMMARY_LLAMA_CPP_FILENAME"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 90
log = get_logger("summarization")
_MODEL_READY_LOCK = threading.Lock()
_MODEL_READY = False
_LLAMA_LOCK = threading.Lock()
_LLAMA_MODEL = None
_LLAMA_MODEL_KEY = None


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


def _summary_backend() -> str:
    backend = str(os.getenv(SUMMARY_BACKEND_ENV) or "internal").strip().lower()
    if backend in {"ollama", "internal", "extractive"}:
        return backend
    log.warning("Unknown summary backend=%s; using internal", backend)
    return "internal"


def _ollama_url() -> str:
    configured_url = str(os.getenv(OLLAMA_URL_ENV) or "").strip()
    return configured_url or DEFAULT_OLLAMA_URL


def _llama_cpp_model_ref(model_name: str) -> tuple[str, str]:
    repo_id = str(os.getenv(LLAMA_CPP_REPO_ENV) or "").strip() or DEFAULT_LLAMA_CPP_REPO_ID
    filename = str(os.getenv(LLAMA_CPP_FILENAME_ENV) or "").strip()
    if not filename and model_name and model_name not in {DEFAULT_SUMMARY_MODEL, DEFAULT_OLLAMA_MODEL}:
        filename = model_name
    return repo_id, filename or DEFAULT_LLAMA_CPP_FILENAME


def _load_llama_cpp_model(model_name: str):
    global _LLAMA_MODEL, _LLAMA_MODEL_KEY
    if importlib.util.find_spec("llama_cpp") is None:
        raise RuntimeError("llama-cpp-python is not installed in this environment")
    repo_id, filename = _llama_cpp_model_ref(model_name)
    model_key = (repo_id, filename)
    with _LLAMA_LOCK:
        if _LLAMA_MODEL is not None and _LLAMA_MODEL_KEY == model_key:
            return _LLAMA_MODEL
        from llama_cpp import Llama

        log.info("Loading internal summary model repo_id=%s filename=%s", repo_id, filename)
        _LLAMA_MODEL = Llama.from_pretrained(
            repo_id=repo_id,
            filename=filename,
            n_ctx=int(os.getenv("GETOFFLINE_SUMMARY_CONTEXT_TOKENS", "8192")),
            n_threads=int(os.getenv("GETOFFLINE_SUMMARY_THREADS", "4")),
            verbose=False,
        )
        _LLAMA_MODEL_KEY = model_key
        return _LLAMA_MODEL


def _extract_json_summary(text: str) -> str:
    raw_response = str(text or "").strip()
    if not raw_response:
        return ""
    json_text = raw_response
    if not json_text.startswith("{"):
        match = re.search(r"\{.*\}", json_text, flags=re.DOTALL)
        if match:
            json_text = match.group(0)
    if json_text.startswith("{"):
        try:
            parsed_response = json.loads(json_text)
        except json.JSONDecodeError:
            return raw_response
        return str(parsed_response.get("summary") or "").strip()
    return raw_response


def _internal_llama_summary(text: str, model_name: str, timeout_seconds: int = DEFAULT_OLLAMA_TIMEOUT_SECONDS) -> str:
    del timeout_seconds
    llm = _load_llama_cpp_model(model_name)
    messages = [
        {"role": "system", "content": "Return strict JSON only: {\"summary\": \"...\"}."},
        {
            "role": "user",
            "content": (
                "Write a concise 1-2 sentence paraphrased summary (max 220 chars). "
                "Focus on topic + takeaway. Avoid filler, quotes, transcript-style wording, and any ad/promotional language. "
                "Never mention sponsors, products, offers, discounts, or marketing claims.\n\n"
                f"Transcript:\n{_truncate_for_prompt(text)}"
            ),
        },
    ]
    response = llm.create_chat_completion(
        messages=messages,
        max_tokens=120,
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    choices = response.get("choices") if isinstance(response, dict) else None
    content = ""
    if choices:
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "").strip()
    response_text = _extract_json_summary(content)
    if response_text:
        response_text = re.sub(r"\s+", " ", response_text)
        if len(response_text) > 280:
            response_text = response_text[:279].rstrip() + "…"
        return response_text
    raise RuntimeError("internal summary response did not include a usable summary")


def _ollama_summary(text: str, model_name: str, url: str | None = None, timeout_seconds: int = DEFAULT_OLLAMA_TIMEOUT_SECONDS) -> str:
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
        url or _ollama_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=max(1, int(timeout_seconds))) as resp:
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
        raise RuntimeError(f"ollama summary request failed model={model_name} timeout_seconds={int(timeout_seconds)} error={exc}") from exc
    raise RuntimeError("ollama summary response did not include a usable summary")


def ensure_local_summary_model(model_name: str = DEFAULT_OLLAMA_MODEL, ollama_path: str = "ollama") -> bool:
    del ollama_path
    global _MODEL_READY
    with _MODEL_READY_LOCK:
        if not _MODEL_READY:
            log.info("Summary model readiness will be checked backend=%s model=%s", _summary_backend(), model_name)
            _MODEL_READY = True
        return True


def summarize_segments(segments: List[str], model_name: str = DEFAULT_OLLAMA_MODEL, mode: str = "in_process", timeout_seconds: int = DEFAULT_OLLAMA_TIMEOUT_SECONDS) -> Dict[str, str]:
    cleaned_segments: List[str] = []
    for segment in segments:
        cleaned_segment = str(segment or "").strip()
        if cleaned_segment:
            cleaned_segments.append(cleaned_segment)
    joined_text = " ".join(cleaned_segments)
    ensure_local_summary_model(model_name=model_name)
    if mode != "in_process":
        log.info("Ignoring deprecated summary mode=%s; using native in-process summary generation", mode)
    backend = _summary_backend()
    try:
        if backend == "extractive":
            raise RuntimeError("extractive summary backend requested")
        if backend == "ollama":
            llm_summary = _ollama_summary(joined_text, model_name=model_name, timeout_seconds=timeout_seconds)
        else:
            llm_summary = _internal_llama_summary(joined_text, model_name=model_name, timeout_seconds=timeout_seconds)
        return {"summary_text": llm_summary, "model_name": f"{backend}:{model_name}", "updated_at": _utcnow_iso()}
    except RuntimeError as exc:
        fallback = _extractive_summary(joined_text)
        log.warning("Summary generation used extractive fallback backend=%s requested_model=%s transcript_chars=%s error=%s", backend, model_name, len(joined_text), exc)
        return {"summary_text": fallback, "model_name": "extractive-local", "updated_at": _utcnow_iso()}
