import gc
import json
import re
import subprocess
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List

STOP_WORDS = {
    "a","an","and","are","as","at","be","by","for","from","has","he","in","is","it","its","of","on","that","the","to","was","were","will","with","you","your","we","they","this","those","these","or","if","but"
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_sentences(text: str) -> List[str]:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized:
        return []
    parts = re.split(r"(?<=[.!?])\s+", normalized)
    return [p.strip() for p in parts if p.strip()]


def _extractive_summary(text: str, max_sentences: int = 2, max_chars: int = 280) -> str:
    sentences = _split_sentences(text)
    if not sentences:
        return "No transcript content available yet."
    words = re.findall(r"[a-zA-Z']+", text.lower())
    freq = Counter(w for w in words if w not in STOP_WORDS and len(w) > 2)
    scored = []
    for idx, sentence in enumerate(sentences):
        tokens = re.findall(r"[a-zA-Z']+", sentence.lower())
        score = sum(freq.get(tok, 0) for tok in tokens)
        scored.append((score, idx, sentence))
    top = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_sentences]
    ordered = sorted(top, key=lambda item: item[1])
    summary = " ".join(item[2] for item in ordered).strip()
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return summary


def summarize_segments(segments: List[str], model_name: str = "extractive-local", mode: str = "subprocess") -> Dict[str, str]:
    joined_text = " ".join((s or "").strip() for s in segments if (s or "").strip())
    if mode == "in_process":
        return {
            "summary_text": _extractive_summary(joined_text),
            "model_name": model_name,
            "updated_at": _utcnow_iso(),
        }
    payload = {"text": joined_text, "model_name": model_name}
    cmd = [sys.executable, "-m", "summarization", "--worker", json.dumps(payload)]
    completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"summary subprocess failed: {details}")
    return json.loads(completed.stdout)


def _worker_once(text: str, model_name: str = "extractive-local") -> Dict[str, str]:
    try:
        return {
            "summary_text": _extractive_summary(text),
            "model_name": model_name,
            "updated_at": _utcnow_iso(),
        }
    finally:
        gc.collect()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        try:
            args = json.loads(sys.argv[2])
            result = _worker_once(text=str(args.get("text") or ""), model_name=str(args.get("model_name") or "extractive-local"))
            sys.stdout.write(json.dumps(result))
        except Exception:
            sys.stderr.write(traceback.format_exc())
            sys.exit(1)
