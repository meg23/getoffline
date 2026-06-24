"""Transcript-based explicit-content screening for downloaded media."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from workers.logger import get_logger

log = get_logger("content_filter")

# Deliberately conservative whole-word matches to limit accidental deletions. This
# filter is a local heuristic, not a substitute for a contextual moderation model.
_PROFANITY_TERMS = {
    "asshole",
    "bastard",
    "bitch",
    "bullshit",
    "cocksucker",
    "fuck",
    "fucked",
    "fucker",
    "fucking",
    "motherfucker",
    "shit",
    "shitty",
    "ass",
    "piss",
    "damn",
    "dick",
}

_SEXUAL_TERMS = {
    "blowjob",
    "handjob",
    "hardcore porn",
    "intercourse",
    "masturbate",
    "masturbating",
    "masturbation",
    "naked sex",
    "oral sex",
    "porn",
    "pornographic",
    "sexual intercourse",
    "sex",
    "sexual",
}

_SRT_METADATA_RE = re.compile(
    r"^(?:\d+|\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}[,.]\d{3}.*)$"
)
_NON_WORD_RE = re.compile(r"[^a-z0-9']+")
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
    """Find a conservative profanity or sexual-content term in transcript text."""
    transcript = str(text or "").strip()
    sentences = _SENTENCE_BOUNDARY_RE.split(transcript)
    for category, terms in (
        ("profanity", _PROFANITY_TERMS),
        ("sexual content", _SEXUAL_TERMS),
    ):
        for term in sorted(terms, key=len, reverse=True):
            for sentence in sentences:
                normalized = " " + _NON_WORD_RE.sub(" ", sentence.lower()).strip() + " "
                if f" {term} " in normalized:
                    return ExplicitContentMatch(
                        category=category,
                        term=term,
                        sentence=sentence.strip(),
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
