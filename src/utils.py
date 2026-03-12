import re
from pathlib import Path


def sanitize(name):
    sanitized = re.sub(r"[^\w.-]", "_", name)
    sanitized = re.sub(r"\.{2,}", ".", sanitized)
    sanitized = sanitized.strip(". ")
    return sanitized or "item"


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
