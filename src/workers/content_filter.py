"""Transcript-based explicit-content screening for downloaded media."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from workers.logger import get_logger

log = get_logger("content_filter")

_PROFANITY_MODEL = None
_PROFANITY_MODEL_ERROR: Optional[Exception] = None
_EXPLICIT_TERM_PATTERNS = [
    re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    for term in (
        "anal",
        "bitch",
        "blowjob",
        "bullshit",
        "cocksucker",
        "cunt",
        "dick",
        "fag",
        "faggot",
        "fuck",
        "fucked",
        "fucker",
        "fucking",
        "handjob",
        "motherfucker",
        "nigger",
        "porn",
        "pornography",
        "pussy",
        "shit",
        "slut",
        "twat",
        "whore",
    )
]


def _patch_sklearn_externals_joblib() -> None:
    """Expose joblib at sklearn.externals.joblib for legacy profanity-check."""
    import joblib
    import sklearn.externals

    if not hasattr(sklearn.externals, "joblib"):
        sklearn.externals.joblib = joblib


def _predict_profanity(texts):
    """Return profanity-check predictions, falling back to explicit terms if unavailable."""
    global _PROFANITY_MODEL, _PROFANITY_MODEL_ERROR
    if _PROFANITY_MODEL is None and _PROFANITY_MODEL_ERROR is None:
        try:
            _patch_sklearn_externals_joblib()
            from profanity_check import predict

            _PROFANITY_MODEL = predict
        except (
            Exception
        ) as exc:  # pragma: no cover - depends on optional package availability
            _PROFANITY_MODEL_ERROR = exc
            log.warning(
                "profanity-check is unavailable; using explicit-term fallback for transcript screening: %s",
                exc,
            )
    if _PROFANITY_MODEL is None:
        return None
    return _PROFANITY_MODEL(list(texts))


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
                    term="profanity-check",
                    sentence=sentence,
                )

    for sentence in sentences:
        for pattern in _EXPLICIT_TERM_PATTERNS:
            match = pattern.search(sentence)
            if match:
                return ExplicitContentMatch(
                    category="profanity",
                    term=match.group(0).lower(),
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
    for candidate in media_path.parent.glob(f"{media_path.stem}.*"):
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
