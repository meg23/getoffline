"""Subprocess entrypoint for isolated Whisper transcription work."""

import gc
import json
import sys
import traceback

from faster_whisper import WhisperModel

from workers.transcription import _normalize_faster_whisper_result


def transcribe_worker_once(
    input_file: str, model_name: str, language: str | None = None
):
    model = None
    segments = None
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        transcribe_kwargs = {"vad_filter": True}
        if language:
            transcribe_kwargs["language"] = language
        try:
            segments, _info = model.transcribe(str(input_file), **transcribe_kwargs)
        except IndexError as exc:
            if "tuple index out of range" in str(exc):
                raise RuntimeError(
                    f"No decodable audio stream found in media file: {input_file}"
                ) from exc
            raise
        return _normalize_faster_whisper_result(segments)
    finally:
        del segments
        if model is not None:
            del model
        gc.collect()


def main() -> None:
    try:
        args = json.loads(sys.argv[1])
        result = transcribe_worker_once(
            input_file=str(args["input_file"]),
            model_name=str(args["model_name"]),
            language=args.get("language"),
        )
        sys.stdout.write(json.dumps(result))
    except Exception:
        sys.stderr.write(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
