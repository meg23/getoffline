"""Subprocess entrypoint for isolated summary generation work."""

import gc
import json
import sys
from typing import Dict

from workers.summarization import DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_TIMEOUT_SECONDS, _ollama_summary, _utcnow_iso


def summarize_worker_once(
    text: str,
    model_name: str = DEFAULT_OLLAMA_MODEL,
    timeout_seconds: int = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
) -> Dict[str, str]:
    try:
        summary = _ollama_summary(text, model_name=model_name, timeout_seconds=timeout_seconds)
        return {"summary_text": summary, "model_name": model_name, "updated_at": _utcnow_iso()}
    finally:
        gc.collect()


def main() -> None:
    try:
        args = json.loads(sys.argv[1])
        result = summarize_worker_once(
            text=str(args.get("text") or ""),
            model_name=str(args.get("model_name") or DEFAULT_OLLAMA_MODEL),
            timeout_seconds=int(args.get("timeout_seconds") or DEFAULT_OLLAMA_TIMEOUT_SECONDS),
        )
        sys.stdout.write(json.dumps(result))
    except Exception as exc:
        sys.stderr.write(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
