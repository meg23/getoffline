"""Fetch worker inputs through the API when shared media storage is absent."""

from __future__ import annotations

import os
import tempfile
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
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


def _download_job_artifact(
    profile_id: str, job_id: int, remote_path: Path, destination: Path
) -> Path:
    token = str(os.getenv("GETOFFLINE_WORKER_API_TOKEN", "")).strip()
    if not token:
        raise FileNotFoundError(
            "Worker media is not mounted and GETOFFLINE_WORKER_API_TOKEN is unset"
        )
    url = (
        f"{_api_url()}/internal/worker/job-media/"
        f"{quote(str(profile_id), safe='')}/{job_id}"
        f"?path={quote(str(remote_path), safe='')}"
    )
    return _download_url(url, destination, token)


def _download_url(url: str, destination: Path, token: str) -> Path:
    request = Request(url, headers={"X-GetOffline-Worker-Token": token})
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
    )
    temporary_path = Path(temporary_name)
    bytes_written = 0
    try:
        with os.fdopen(fd, "wb") as output, urlopen(request, timeout=300) as response:  # nosec B310
            while True:
                chunk = response.read(_CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                bytes_written += len(chunk)
        temporary_path.replace(destination)
    except (HTTPError, URLError, OSError):
        temporary_path.unlink(missing_ok=True)
        raise
    log.info("Worker fetched API artifact bytes=%s path=%s", bytes_written, destination)
    return destination


def _cache_artifact_path(namespace: str, identifier: int, remote_path: Path) -> Path:
    return _cache_root() / namespace / str(identifier) / remote_path.name


def worker_cache_output_path(
    profile_id: str, job_id: int | str, remote_path: Path
) -> Path:
    """Return the local cache destination corresponding to a manager path."""
    return _cache_root() / str(profile_id) / "jobs" / str(job_id) / remote_path.name


def ensure_local_job_media(
    profile_id: str, job_id: int, remote_path: Path
) -> Path:
    """Fetch a deferred job artifact into the worker cache when not mounted."""
    candidate = Path(remote_path).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    destination = _cache_artifact_path(profile_id, job_id, candidate)
    return _download_job_artifact(profile_id, job_id, candidate, destination)


def upload_worker_artifact(
    profile_id: str,
    local_path: Path,
    remote_path: Path,
    *,
    download_id: int | None = None,
    job_id: int | None = None,
) -> None:
    """Upload a processed artifact to its canonical manager-side path."""
    token = str(os.getenv("GETOFFLINE_WORKER_API_TOKEN", "")).strip()
    if not token:
        raise FileNotFoundError("GETOFFLINE_WORKER_API_TOKEN is unset")
    if download_id is not None:
        endpoint = f"{_api_url()}/internal/worker/media/{quote(str(profile_id), safe='')}/{download_id}"
    elif job_id is not None:
        endpoint = f"{_api_url()}/internal/worker/job-media/{quote(str(profile_id), safe='')}/{job_id}"
    else:
        raise ValueError("download_id or job_id is required")
    parsed = urlparse(endpoint)
    connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_type(parsed.netloc, timeout=300)
    request_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection.putrequest("POST", request_path)
    connection.putheader("X-GetOffline-Worker-Token", token)
    connection.putheader("X-GetOffline-Worker-Path", str(remote_path))
    connection.putheader("Content-Type", "application/octet-stream")
    connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    try:
        with Path(local_path).open("rb") as source:
            while chunk := source.read(_CHUNK_SIZE):
                connection.send(f"{len(chunk):X}\r\n".encode("ascii"))
                connection.send(chunk)
                connection.send(b"\r\n")
        connection.send(b"0\r\n\r\n")
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raise OSError(
                f"Worker artifact upload failed: HTTP {response.status} {response.reason}"
            )
        response.read()
    finally:
        connection.close()
    log.info("Worker uploaded API artifact local=%s remote=%s", local_path, remote_path)


def delete_worker_artifact(
    profile_id: str,
    remote_path: Path,
    *,
    download_id: int | None = None,
    job_id: int | None = None,
) -> None:
    """Delete a processed source artifact from the manager."""
    token = str(os.getenv("GETOFFLINE_WORKER_API_TOKEN", "")).strip()
    if download_id is not None:
        endpoint = f"{_api_url()}/internal/worker/media/{quote(str(profile_id), safe='')}/{download_id}"
    elif job_id is not None:
        endpoint = f"{_api_url()}/internal/worker/job-media/{quote(str(profile_id), safe='')}/{job_id}"
    else:
        raise ValueError("download_id or job_id is required")
    parsed = urlparse(endpoint)
    connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    connection = connection_type(parsed.netloc, timeout=300)
    request_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        connection.putrequest("DELETE", request_path)
        connection.putheader("X-GetOffline-Worker-Token", token)
        connection.putheader("X-GetOffline-Worker-Path", str(remote_path))
        connection.endheaders()
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raise OSError(
                f"Worker artifact delete failed: HTTP {response.status} {response.reason}"
            )
        response.read()
    finally:
        connection.close()


def ensure_local_media(download: Download, path: Path | None = None) -> Path:
    """Return a usable local path, falling back to an authenticated API fetch."""
    candidate = (path or Path(str(download.file_path or ""))).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    cached = _cache_path(download)
    if cached.is_file():
        return cached
    return _download_from_api(download, cached)
