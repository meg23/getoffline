import re
from pathlib import Path


def sanitize(name):
    sanitized = re.sub(r"[^\w.-]", "_", name)
    sanitized = re.sub(r"\.{2,}", ".", sanitized)
    sanitized = sanitized.strip(". ")
    return sanitized or "item"


def sanitize_channel_name(name):
    sanitized = sanitize(name).replace("_", "")
    return sanitized or "channel"


def ensure_dir(path):
    Path(path).expanduser().mkdir(parents=True, exist_ok=True)


def normalize_media_filename(path: Path) -> Path:
    path = Path(path)
    normalized_stem = re.sub(r"\.{2,}", ".", path.stem).rstrip(". ")
    if not normalized_stem:
        normalized_stem = "item"

    normalized_path = path.with_name(f"{normalized_stem}{path.suffix}")
    if normalized_path == path:
        return path

    counter = 1
    candidate = normalized_path
    while candidate.exists():
        candidate = path.with_name(f"{normalized_stem}_{counter}{path.suffix}")
        counter += 1

    path.rename(candidate)
    return candidate


def split_title_filter_terms(value: object) -> list[str]:
    """Return normalized title exclusion terms split on commas or newlines."""
    raw = str(value or "")
    return [
        term.strip().casefold() for term in re.split(r"[,\n\r]+", raw) if term.strip()
    ]


def title_matches_filter(title: object, terms: list[str]) -> str:
    """Return the matching exclusion term for a title, or an empty string."""
    normalized_title = str(title or "").casefold()
    if not normalized_title:
        return ""
    for term in terms:
        if term and term in normalized_title:
            return term
    return ""
