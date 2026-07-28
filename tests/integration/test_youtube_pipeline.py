"""End-to-end Docker Compose verification for the YouTube download pipeline.

This test intentionally exercises the real runtime stack while replacing only
the network-facing yt-dlp YouTube extractor with a deterministic local test
double. MySQL, RabbitMQ, the frontend, downloader, FFmpeg, transcript, transfer,
scheduler, and cleanup services still run through Docker Compose. It queues a
YouTube-shaped download and verifies that the queued pipeline creates media,
generates a transcript, and runs the profanity screening path without contacting
YouTube.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

YOUTUBE_URL = "https://www.youtube.com/watch?v=BB49x_uMlGA"
PROFILE_ID = "integration"
SOURCE_NAME = "Integration YouTube Runtime"
DEFAULT_TIMEOUT_SECONDS = 1800
CONTAINER_DOWNLOAD_ROOT = Path("/app/downloads")
_WRITABLE_DOWNLOAD_STACKS: set[str] = set()

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
COMPOSE_SERVICES = (
    "rabbitmq",
    "mysql",
    "frontend",
    "api",
    "worker-updates",
    "worker-downloader-youtube",
    "worker-downloader-podcast",
    "worker-ffmpeg",
    "worker-transcripts",
    "scheduler",
    "worker-cleanup",
)


def _run(
    cmd: list[str],
    *,
    env: dict[str, str],
    timeout: int = 300,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print(f"+ {' '.join(cmd)}", flush=True)
    completed = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, flush=True)
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed with exit code {completed.returncode}: {' '.join(cmd)}"
        )
    return completed


def _log_check(message: str) -> None:
    print(f"[integration-test] PASS: {message}", flush=True)


def _compose_up_command(compose: list[str]) -> list[str]:
    cmd = [*compose, "up", "-d", "--build"]
    for service in COMPOSE_SERVICES:
        cmd.extend(["--scale", f"{service}=1"])
    return cmd


def _start_log_stream(compose: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    print("+ " + " ".join([*compose, "logs", "-f", "--tail", "100"]), flush=True)
    return subprocess.Popen(
        [*compose, "logs", "-f", "--tail", "100"],
        cwd=ROOT,
        env=env,
        text=True,
    )


def _stop_log_stream(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _compose_cmd() -> list[str]:
    if shutil.which("docker") is None:
        raise AssertionError("docker is required for integration-test")
    probe = subprocess.run(
        ["docker", "compose", "version"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise AssertionError("docker compose v2 or docker-compose is required")


def _django_setup(env: dict[str, str]) -> None:
    os.environ.update(env)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
    sys.path.insert(0, str(SRC))
    import django

    django.setup()


def _free_tcp_port(excluded: set[int]) -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in excluded:
            excluded.add(port)
            return port


def _wait_for_service(deadline: float, url: str, name: str) -> None:
    import urllib.request

    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return
        except Exception as exc:
            last_error = exc
        time.sleep(5)
    raise AssertionError(f"{name} did not become reachable: {last_error}")


def _wait_for_frontend(deadline: float, frontend_port: int) -> None:
    # The library is authenticated and intentionally returns a 401/redirect
    # for anonymous requests. Probe the public login page so readiness checks
    # do not create misleading authentication errors in the frontend logs.
    _wait_for_service(deadline, f"http://127.0.0.1:{frontend_port}/login/", "frontend")


def _wait_for_api(deadline: float, api_port: int) -> None:
    _wait_for_service(deadline, f"http://127.0.0.1:{api_port}/api/health", "api")


def _verify_profanity_model() -> None:
    from workers.content_filter import find_explicit_content

    match = find_explicit_content("This sentence is fucking profane.")
    if match is None or match.category != "profanity":
        raise AssertionError("profanity model did not detect the runtime sample")


def _queue_download_job() -> int:
    from frontend.queue import publish_job
    from models.domain import SourceType
    from models.jobs import create_job
    from models.models import AppConfigValue, ProfileConfigValue, SourceConfig

    SourceConfig.objects.filter(profile_id=PROFILE_ID, name=SOURCE_NAME).delete()
    source = SourceConfig.objects.create(
        profile_id=PROFILE_ID,
        source_type=SourceType.YOUTUBE,
        name=SOURCE_NAME,
        url=YOUTUBE_URL,
        media_type="audio",
        enabled=True,
        subtitles=True,
        max_downloads=1,
        delete_explicit_content=True,
        include_shorts=False,
        include_livestreams=False,
    )
    for key, value in {
        "output_root": "/app/downloads/integration",
        "audio_format": "mp3",
        "audio_quality": "5",
        "subtitle_transcription_mode": "in_process",
        "js_runtime_path": "qjs",
    }.items():
        ProfileConfigValue.objects.update_or_create(
            profile_id=PROFILE_ID, key=key, defaults={"value": value}
        )
    AppConfigValue.objects.update_or_create(
        key="manual_upload_delete_explicit_content", defaults={"value": "1"}
    )
    job = create_job(
        profile_id=PROFILE_ID,
        job_type="download_single",
        payload={
            "source_id": source.id,
            "source_type": SourceType.YOUTUBE,
            "source_name": source.name,
            "source_url": source.url,
            "item_uid": "BB49x_uMlGA",
            "item_url": YOUTUBE_URL,
            "media_url": YOUTUBE_URL,
            "url": YOUTUBE_URL,
            "title": "Integration YouTube runtime video",
            "media_type": "audio",
            "subtitles": True,
            "delete_explicit_content": True,
            "manual_enqueue": True,
            "redownload": True,
        },
        idempotency_key=f"integration:{uuid.uuid4()}",
    )
    publish_job(
        {
            "job_id": job.id,
            "job_type": job.job_type,
            "profile_id": job.profile_id,
            "payload": job.payload,
        }
    )
    print(
        f"[integration-test] QUEUED: job_id={job.id} url={YOUTUBE_URL}",
        flush=True,
    )
    return job.id


def _make_downloads_host_writable(
    compose: list[str], compose_env: dict[str, str], *, force: bool = False
) -> None:
    """Allow host cleanup to remove Docker-created download artifacts.

    The frontend is intentionally stateless and no longer mounts the downloads
    directory. The API still mounts it, so use the API container for this
    ownership/permission adjustment.
    """
    project = str(compose_env.get("COMPOSE_PROJECT_NAME") or "")
    if not force and project in _WRITABLE_DOWNLOAD_STACKS:
        return
    _run(
        [
            *compose,
            "exec",
            "-T",
            "api",
            "chmod",
            "-R",
            "a+rwX",
            str(CONTAINER_DOWNLOAD_ROOT),
        ],
        env=compose_env,
        timeout=120,
        check=False,
    )
    if project:
        _WRITABLE_DOWNLOAD_STACKS.add(project)


def _host_download_path(raw_path: str | None, host_downloads_dir: Path) -> Path:
    candidate = Path(raw_path or "")
    if candidate.is_absolute():
        try:
            relative_path = candidate.relative_to(CONTAINER_DOWNLOAD_ROOT)
        except ValueError:
            return candidate
        return host_downloads_dir / relative_path
    return candidate


def _assert_pipeline_result(
    *,
    download,
    media_path: Path,
    subtitle_path: Path,
    transcript_count: int,
    jobs: list,
) -> None:
    from models.domain import JobStatus

    active_jobs = [
        job for job in jobs if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
    ]
    if active_jobs:
        details = ", ".join(f"{job.id}:{job.job_type}" for job in active_jobs)
        raise AssertionError(f"pipeline still has active jobs: {details}")
    _log_check("no queued or running jobs remain for integration profile")

    succeeded_job_types = {
        job.job_type for job in jobs if JobStatus(job.status) is JobStatus.SUCCEEDED
    }
    required_job_types = {"download_single", "transcode_media", "generate_transcript"}
    missing_job_types = required_job_types - succeeded_job_types
    if missing_job_types:
        raise AssertionError(
            "pipeline did not complete expected job types: "
            + ", ".join(sorted(missing_job_types))
        )
    _log_check("expected job types succeeded: " + ", ".join(sorted(required_job_types)))

    if download.download_status != "downloaded":
        raise AssertionError(
            f"unexpected download status: {download.download_status!r}"
        )
    _log_check("download row status is downloaded")

    if (download.file_ext or "").lower() != "mp3":
        raise AssertionError(f"expected mp3 download, got {download.file_ext!r}")
    _log_check("download row records an mp3 artifact")

    if not str(download.subtitle_path or "").strip():
        raise AssertionError("download row did not record a subtitle path")
    _log_check("download row records a subtitle path")

    if not media_path.exists() or media_path.stat().st_size <= 0:
        raise AssertionError(f"media artifact missing or empty: {media_path}")
    _log_check(f"media artifact exists and is non-empty: {media_path}")

    if not subtitle_path.exists() or subtitle_path.stat().st_size <= 0:
        raise AssertionError(f"subtitle artifact missing or empty: {subtitle_path}")
    _log_check(f"subtitle artifact exists and is non-empty: {subtitle_path}")

    if transcript_count <= 0:
        raise AssertionError("transcript segments were not saved")
    _log_check(f"transcript segments were saved: count={transcript_count}")


def _wait_for_pipeline(job_id: int, deadline: float, host_downloads_dir: Path) -> None:
    from models.domain import JobStatus
    from models.models import Download, Job, TranscriptSegment
    from workers.content_filter import screen_transcript

    terminal = {JobStatus.SUCCEEDED, JobStatus.FAILED}
    while time.monotonic() < deadline:
        job = Job.objects.get(pk=job_id)
        downloads = list(
            Download.objects.filter(profile_id=PROFILE_ID, item_uid="BB49x_uMlGA")
        )
        child_jobs = list(Job.objects.filter(profile_id=PROFILE_ID).order_by("id"))
        failed = [
            candidate
            for candidate in child_jobs
            if JobStatus(candidate.status) is JobStatus.FAILED
        ]
        if failed:
            details = "; ".join(
                f"{j.id}:{j.job_type}:{j.error_message}" for j in failed
            )
            raise AssertionError(f"pipeline job failed: {details}")
        if downloads:
            download = downloads[-1]
            media_path = _host_download_path(download.file_path, host_downloads_dir)
            subtitle_path = _host_download_path(
                download.subtitle_path, host_downloads_dir
            )
            transcript_count = TranscriptSegment.objects.filter(
                download=download
            ).count()
            active_jobs_done = not Job.objects.filter(
                profile_id=PROFILE_ID,
                status__in=[JobStatus.QUEUED, JobStatus.RUNNING],
            ).exists()
            if job.status in terminal and active_jobs_done:
                _assert_pipeline_result(
                    download=download,
                    media_path=media_path,
                    subtitle_path=subtitle_path,
                    transcript_count=transcript_count,
                    jobs=child_jobs,
                )
                match = screen_transcript(subtitle_path)
                if match is not None:
                    raise AssertionError(
                        f"download transcript unexpectedly matched profanity: {match}"
                    )
                _log_check("profanity screening completed with clean result")
                return
        time.sleep(10)
    raise AssertionError(f"pipeline did not finish within timeout for job_id={job_id}")


def main() -> int:
    timeout_seconds = int(
        os.getenv("GETOFFLINE_INTEGRATION_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    )
    compose = _compose_cmd()
    with tempfile.TemporaryDirectory(prefix="getoffline-integration-") as tmp:
        downloads_dir = Path(tmp) / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        reserved_ports: set[int] = set()
        frontend_port = _free_tcp_port(reserved_ports)
        api_port = _free_tcp_port(reserved_ports)
        mysql_port = _free_tcp_port(reserved_ports)
        rabbitmq_port = _free_tcp_port(reserved_ports)
        project = f"getoffline-it-{uuid.uuid4().hex[:8]}"
        compose_env = os.environ.copy()
        compose_env.update(
            {
                "COMPOSE_PROJECT_NAME": project,
                "GETOFFLINE_DJANGO_SECRET_KEY": "integration-test-secret",
                "GETOFFLINE_DOWNLOADS_DIR": str(downloads_dir),
                "GETOFFLINE_FRONTEND_PUBLISHED_PORT": str(frontend_port),
                "GETOFFLINE_API_PUBLISHED_PORT": str(api_port),
                "GETOFFLINE_DB_PUBLISHED_PORT": str(mysql_port),
                "GETOFFLINE_RABBITMQ_PUBLISHED_PORT": str(rabbitmq_port),
                "GETOFFLINE_DB_HOST": "mysql",
                "GETOFFLINE_DB_PORT": "3306",
                "GETOFFLINE_DB_NAME": "getoffline",
                "GETOFFLINE_DB_USER": "getoffline",
                "GETOFFLINE_DB_PASSWORD": "getoffline",
                "GETOFFLINE_RABBITMQ_URL": "amqp://guest:guest@rabbitmq:5672/%2F",
                "GETOFFLINE_RABBITMQ_EXCHANGE": "getoffline",
                "GETOFFLINE_YTDLP_MODULE": "workers.fake_ytdlp",
            }
        )
        host_env = compose_env.copy()
        host_env.update(
            {
                "GETOFFLINE_DB_HOST": "127.0.0.1",
                "GETOFFLINE_DB_PORT": str(mysql_port),
                "GETOFFLINE_RABBITMQ_URL": (
                    f"amqp://guest:guest@127.0.0.1:{rabbitmq_port}/%2F"
                ),
                "GETOFFLINE_DJANGO_ROLE": "api",
                "DJANGO_SETTINGS_MODULE": "frontend.settings",
                "PYTHONPATH": str(SRC),
            }
        )
        log_stream = None
        try:
            try:
                _run(_compose_up_command(compose), env=compose_env, timeout=1800)
            except AssertionError:
                _run([*compose, "ps"], env=compose_env, timeout=60, check=False)
                _run(
                    [*compose, "logs", "--tail", "200"],
                    env=compose_env,
                    timeout=120,
                    check=False,
                )
                raise
            log_stream = _start_log_stream(compose, compose_env)
            deadline = time.monotonic() + timeout_seconds
            _wait_for_frontend(deadline, frontend_port)
            _wait_for_api(deadline, api_port)
            _django_setup(host_env)
            _verify_profanity_model()
            job_id = _queue_download_job()
            _wait_for_pipeline(job_id, deadline, downloads_dir)
        finally:
            _stop_log_stream(log_stream)
            _make_downloads_host_writable(compose, compose_env, force=True)
            keep_stack = os.getenv("GETOFFLINE_INTEGRATION_KEEP_STACK", "0")
            if keep_stack.lower() not in {"1", "true", "yes"}:
                _run(
                    [*compose, "down", "-v", "--remove-orphans"],
                    env=compose_env,
                    timeout=600,
                    check=False,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
