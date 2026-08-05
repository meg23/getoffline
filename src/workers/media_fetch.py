"""Fetch worker inputs through the API when shared media storage is absent."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from models.models import Download
from workers.logger import get_logger

log = get_logger("workers.media_fetch")
_CHUNK_SIZE = 1024 * 1024


def _api_url() -> str:
    return os.getenv("GETOFFLINE_WORKER_API_URL", "http://api:8000/api").rstrip("/")


def _cache_root() -> Path:
    return Path(
        os.getenv(
            "GETOFFLINE_WORKER_MEDIA_CACHE_DIR",
            str(Path(tempfile.gettempdir()) / "getoffline-worker-media"),
        )
    ).expanduser()


def _cache_path(download: Download) -> Path:
    filename = Path(str(download.file_path or "media")).name or "media"
    return _cache_root() / str(download.profile_id) / str(download.id) / filename


def _download_from_api(download: Download, destination: Path) -> Path:
    token = str(os.getenv("GETOFFLINE_WORKER_API_TOKEN", "")).strip()
    if not token:
        raise FileNotFoundError(
            "Worker media is not mounted and GETOFFLINE_WORKER_API_TOKEN is unset"
        )
    url = (
        f"{_api_url()}/internal/worker/media/"
        f"{quote(str(download.profile_id), safe='')}/{download.id}"
    )
    request = Request(url, headers={"X-GetOffline-Worker-Token": token})
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
    )
    temporary_path = Path(temporary_name)
    bytes_written = 0
    try:
        with os.fdopen(fd, "wb") as output, urlopen(  # nosec B310
            request, timeout=300
        ) as response:
            while True:
                chunk = response.read(_CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                bytes_written += len(chunk)
        expected_size = int(download.file_size_bytes or 0)
        if expected_size and bytes_written != expected_size:
            raise OSError(
                f"Worker media size mismatch: expected {expected_size}, got {bytes_written}"
            )
        temporary_path.replace(destination)
    except (HTTPError, URLError, OSError):
        temporary_path.unlink(missing_ok=True)
        raise
    log.info(
        "Worker fetched media through API download_id=%s profile_id=%s bytes=%s path=%s",
        download.id,
        download.profile_id,
        bytes_written,
        destination,
    )
    return destination


def ensure_local_media(download: Download, path: Path | None = None) -> Path:
    """Return a usable local path, falling back to an authenticated API fetch."""
    candidate = (path or Path(str(download.file_path or ""))).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    cached = _cache_path(download)
    if cached.is_file():
        return cached
    return _download_from_api(download, cached)
