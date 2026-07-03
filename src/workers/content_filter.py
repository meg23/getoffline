"""Transcript-based explicit-content screening for downloaded media."""

import argparse
import importlib.util
import re
from dataclasses import dataclass
from glob import escape as glob_escape
from pathlib import Path
from typing import List, Optional

from workers.logger import get_logger

log = get_logger("content_filter")

_PROFANITY_FILTER = None
_PROFANITY_FILTER_ERROR: Optional[Exception] = None
_PROFANITY_FILTER_TERM = "profanityfilter"


def _predict_profanity(texts):
    """Return profanityfilter predictions, or None when the package is unavailable."""
    global _PROFANITY_FILTER, _PROFANITY_FILTER_ERROR
    if _PROFANITY_FILTER is None and _PROFANITY_FILTER_ERROR is None:
        try:
            if importlib.util.find_spec("profanityfilter") is None:
                raise ModuleNotFoundError("No module named 'profanityfilter'")
            from profanityfilter import ProfanityFilter

            _PROFANITY_FILTER = ProfanityFilter()
        except Exception as exc:  # pragma: no cover - depends on optional package availability
            _PROFANITY_FILTER_ERROR = exc
            log.debug("profanityfilter is unavailable: %r", exc)
    if _PROFANITY_FILTER is None:
        return None
    return [bool(_PROFANITY_FILTER.is_profane(text)) for text in texts]


_SRT_METADATA_RE = re.compile(
    r"^(?:\d+|\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}.*)$"
)
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ExplicitContentMatch:
    category: str
    term: str
    sentence: str = ""


def transcript_text(subtitle_path: Path) -> str:
    """Return normalized spoken text from an SRT/VTT-like subtitle file."""
    lines = (
        Path(subtitle_path).read_text(encoding="utf-8", errors="replace").splitlines()
    )
    spoken_lines = []
    for line in lines:
        stripped_line = line.strip()
        if not stripped_line or _SRT_METADATA_RE.match(stripped_line):
            continue
        spoken_lines.append(stripped_line)
    return " ".join(spoken_lines)


def find_explicit_content(text: str) -> Optional[ExplicitContentMatch]:
    """Find explicit language in transcript text."""
    transcript = str(text or "").strip()
    if not transcript:
        return None

    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_BOUNDARY_RE.split(transcript)
        if sentence.strip()
    ]
    if not sentences:
        sentences = [transcript]

    predictions = _predict_profanity(sentences)
    if predictions is not None:
        for sentence, prediction in zip(sentences, predictions):
            try:
                is_profane = int(prediction) == 1
            except (TypeError, ValueError):
                is_profane = bool(prediction)
            if is_profane:
                return ExplicitContentMatch(
                    category="profanity",
                    term=_PROFANITY_FILTER_TERM,
                    sentence=sentence,
                )

    return None


def screen_transcript(subtitle_path: Optional[Path]) -> Optional[ExplicitContentMatch]:
    if subtitle_path is None or not Path(subtitle_path).exists():
        return None
    return find_explicit_content(transcript_text(Path(subtitle_path)))


def delete_media_artifacts(media_path: Path) -> List[Path]:
    """Delete a media file and related artifacts, returning deleted paths."""
    media_path = Path(media_path).expanduser().resolve()
    candidates = {media_path}
    deleted_paths = []
    for candidate in media_path.parent.glob(f"{glob_escape(media_path.stem)}.*"):
        if candidate.is_file():
            candidates.add(candidate)
    for candidate in candidates:
        try:
            existed = candidate.exists()
            candidate.unlink(missing_ok=True)
            if existed:
                deleted_paths.append(candidate)
        except OSError as exc:
            log.warning(
                "Could not delete filtered media artifact %s: %s", candidate, exc
            )
    return sorted(deleted_paths, key=lambda path: str(path))


def log_filtered_deletion(
    *,
    source_type: str,
    source_name: str,
    title: str,
    media_path: Path,
    match: ExplicitContentMatch,
    deleted_paths: List[Path],
) -> None:
    """Write a stable audit event after explicit-content artifacts are deleted."""
    deleted_artifacts = ", ".join(str(path) for path in deleted_paths) or "none"
    log.warning(
        "CONTENT_FILTER_DELETION source_type=%s source_name=%r title=%r "
        "category=%r matched_term=%r matched_sentence=%r media_path=%s deleted_artifacts=%s",
        source_type,
        source_name,
        title,
        match.category,
        match.term,
        match.sentence,
        Path(media_path).expanduser().resolve(),
        deleted_artifacts,
    )


def _screen_text_or_file(*, text: Optional[str], subtitle_path: Optional[str]):
    if text is not None:
        return find_explicit_content(text)
    return screen_transcript(Path(str(subtitle_path)))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Test GetOffline's transcript explicit-content screening against text "
            "or an SRT/VTT subtitle file."
        )
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--check-model",
        action="store_true",
        help="Verify that profanityfilter loads successfully.",
    )
    input_group.add_argument(
        "--text",
        help="Text to screen directly. Useful for checking whether profanityfilter loads.",
    )
    input_group.add_argument(
        "subtitle_path",
        nargs="?",
        help="Path to an SRT/VTT subtitle file to screen.",
    )
    parser.add_argument(
        "--fail-on-match",
        action="store_true",
        help="Exit with status 1 when explicit content is matched.",
    )
    args = parser.parse_args(argv)

    if args.check_model:
        predictions = _predict_profanity(["plain words"])
        if predictions is None:
            error = (
                f"{type(_PROFANITY_FILTER_ERROR).__name__}: {_PROFANITY_FILTER_ERROR!r}"
                if _PROFANITY_FILTER_ERROR is not None
                else "unknown"
            )
            print(f"model=fallback error={error}", flush=True)
            return 2
        print("model=profanityfilter", flush=True)
        return 0

    match = _screen_text_or_file(text=args.text, subtitle_path=args.subtitle_path)
    if match is None:
        print("clean", flush=True)
        return 0
    print(f"matched category={match.category} term={match.term!r}", flush=True)
    if match.sentence:
        print(f"sentence={match.sentence}", flush=True)
    return 1 if args.fail_on_match else 0


if __name__ == "__main__":
    raise SystemExit(main())
