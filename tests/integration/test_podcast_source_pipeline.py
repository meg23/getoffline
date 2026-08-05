"""End-to-end Docker Compose verification for a podcast RSS source.

This test exercises source discovery with a real podcast RSS feed. It adds the
Kids Short Stories ART19 feed as an enabled podcast source, sets the source max
downloads to 2, leaves explicit-content deletion disabled, and verifies that the
runtime only enqueues and downloads two episodes from that source.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models.models import Download

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pipeline = importlib.import_module("tests.integration.test_youtube_pipeline")

PODCAST_RSS_URL = "https://rss.art19.com/kids-short-stories"
PROFILE_ID = "integration-podcast"
SOURCE_NAME = "Integration Kids Short Stories Podcast"
EXPECTED_DOWNLOADS = 2
DEFAULT_TIMEOUT_SECONDS = 1800


def _queue_episode_check() -> int:
    from frontend.queue import publish_job
    from models.domain import SourceType
    from models.jobs import create_job
    from models.models import ProfileConfigValue, SourceConfig

    SourceConfig.objects.filter(profile_id=PROFILE_ID, name=SOURCE_NAME).delete()
    source = SourceConfig.objects.create(
        profile_id=PROFILE_ID,
        source_type=SourceType.PODCAST,
        name=SOURCE_NAME,
        url=PODCAST_RSS_URL,
        media_type="audio",
        enabled=True,
        subtitles=False,
        max_downloads=EXPECTED_DOWNLOADS,
        delete_explicit_content=False,
    )
    for key, value in {
        "output_root": "/app/downloads/integration-podcast",
        "audio_format": "mp3",
        "audio_quality": "5",
        "subtitle_transcription_mode": "in_process",
    }.items():
        ProfileConfigValue.objects.update_or_create(
            profile_id=PROFILE_ID, key=key, defaults={"value": value}
        )
    job = create_job(
        profile_id=PROFILE_ID,
        job_type="check_for_episodes",
        payload={"integration_source_id": source.id},
        idempotency_key=f"integration-podcast:{uuid.uuid4()}",
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
        "[integration-test] QUEUED: "
        f"job_id={job.id} source_id={source.id} url={PODCAST_RSS_URL}",
        flush=True,
    )
    return job.id


def _wait_for_podcast_source(
    job_id: int,
    deadline: float,
    host_downloads_dir: Path,
    compose: list[str],
    compose_env: dict[str, str],
) -> None:
    from django.contrib.auth.models import User
    from django.test import Client

    from models.domain import JobStatus
    from models.models import Download, Job, SourceConfig, TranscriptSegment

    logged_item_job_ids: set[int] = set()
    while time.monotonic() < deadline:
        parent_job = Job.objects.get(pk=job_id)
        jobs = list(Job.objects.filter(profile_id=PROFILE_ID).order_by("id"))
        failed = [job for job in jobs if JobStatus(job.status) is JobStatus.FAILED]
        if failed:
            details = "; ".join(
                f"{job.id}:{job.job_type}:{job.error_message}" for job in failed
            )
            raise AssertionError(f"podcast source pipeline job failed: {details}")
        active_jobs = [
            job
            for job in jobs
            if JobStatus(job.status) in {JobStatus.QUEUED, JobStatus.RUNNING}
        ]
        source = SourceConfig.objects.filter(
            profile_id=PROFILE_ID,
            source_type="podcast",
            name=SOURCE_NAME,
            url=PODCAST_RSS_URL,
            max_downloads=EXPECTED_DOWNLOADS,
            delete_explicit_content=False,
        ).first()
        if source is None:
            raise AssertionError("expected podcast source configuration was not saved")
        download_episode_jobs = [
            job
            for job in jobs
            if job.job_type == "download_episode"
            and isinstance(job.payload, dict)
            and int(job.payload.get("source_id") or 0) == source.id
        ]
        if len(download_episode_jobs) > EXPECTED_DOWNLOADS:
            raise AssertionError(
                "podcast source enqueued more download items than the max: "
                f"{len(download_episode_jobs)} > {EXPECTED_DOWNLOADS}"
            )
        for item_job in download_episode_jobs:
            payload = item_job.payload
            missing = [
                key
                for key in ("item_uid", "item_url", "media_url", "title")
                if not str(payload.get(key) or "").strip()
            ]
            if missing:
                raise AssertionError(
                    f"podcast item job {item_job.id} is missing item fields: {missing}"
                )
            if item_job.id not in logged_item_job_ids:
                logged_item_job_ids.add(item_job.id)
                print(
                    "[integration-test] PODCAST ITEM: "
                    f"job_id={item_job.id} item_uid={payload['item_uid']} "
                    f"title={payload['title']} media_url={payload['media_url']}",
                    flush=True,
                )
        downloads = list(
            Download.objects.filter(
                profile_id=PROFILE_ID,
                source_type="podcast",
                source_name=SOURCE_NAME,
                download_status="downloaded",
            ).order_by("id")
        )
        if (
            JobStatus(parent_job.status) is JobStatus.SUCCEEDED
            and not active_jobs
            and len(downloads) == EXPECTED_DOWNLOADS
        ):
            if len(download_episode_jobs) != EXPECTED_DOWNLOADS:
                raise AssertionError(
                    "expected exactly two download episode jobs, got "
                    f"{len(download_episode_jobs)}"
                )
            if any(
                bool(job.payload.get("delete_explicit_content"))
                for job in download_episode_jobs
                if isinstance(job.payload, dict)
            ):
                raise AssertionError("podcast source enabled profanity screening")
            downloaded_item_uids = {download.item_uid for download in downloads}
            queued_item_uids = {
                job.payload["item_uid"] for job in download_episode_jobs
            }
            if downloaded_item_uids != queued_item_uids:
                raise AssertionError(
                    "downloaded podcast items did not match queued RSS items: "
                    f"downloaded={sorted(downloaded_item_uids)} queued={sorted(queued_item_uids)}"
                )
            for download in downloads:
                if not str(download.item_uid or "").strip():
                    raise AssertionError("download row is missing item_uid")
                if not str(download.item_url or "").strip():
                    raise AssertionError("download row is missing item_url")
                if not str(download.title or "").strip():
                    raise AssertionError("download row is missing title")
                media_path = pipeline._host_download_path(
                    download.file_path, host_downloads_dir
                )
                if not media_path.exists() or media_path.stat().st_size <= 0:
                    raise AssertionError(
                        f"podcast media missing or empty: {media_path}"
                    )
                if (download.file_ext or "").lower() != "mp3":
                    raise AssertionError(
                        f"expected podcast mp3 artifact, got {download.file_ext!r}"
                    )
                print(
                    "[integration-test] PODCAST DOWNLOADED: "
                    f"download_id={download.id} item_uid={download.item_uid} "
                    f"title={download.title} path={media_path}",
                    flush=True,
                )
            transcript_count = TranscriptSegment.objects.filter(
                download__in=downloads
            ).count()
            if transcript_count != 0:
                raise AssertionError(
                    f"podcast source created transcript segments despite subtitles disabled: {transcript_count}"
                )
            if any(str(download.subtitle_path or "").strip() for download in downloads):
                raise AssertionError(
                    "podcast source recorded subtitle paths despite subtitles disabled"
                )
            _exercise_podcast_library_actions(
                client=Client(),
                user_model=User,
                downloads=downloads,
                host_downloads_dir=host_downloads_dir,
                compose=compose,
                compose_env=compose_env,
            )
            pipeline._log_check(
                "podcast source saved two RSS item jobs with item metadata"
            )
            pipeline._log_check(
                "podcast source downloaded exactly two episodes with profanity check disabled"
            )
            pipeline._log_check("podcast source kept transcripts disabled")
            return
        if len(downloads) > EXPECTED_DOWNLOADS:
            raise AssertionError(
                f"podcast source exceeded max downloads: {len(downloads)} > {EXPECTED_DOWNLOADS}"
            )
        time.sleep(10)
    raise AssertionError(
        f"podcast source pipeline did not finish within timeout for job_id={job_id}"
    )


def _make_downloads_host_writable(
    compose: list[str], compose_env: dict[str, str]
) -> None:
    pipeline._make_downloads_host_writable(compose, compose_env)


def _exercise_podcast_library_actions(
    *,
    client,
    user_model,
    downloads: list[Download],
    host_downloads_dir: Path,
    compose: list[str],
    compose_env: dict[str, str],
) -> None:
    """Exercise user-facing played/delete/purge actions against podcast items."""
    from models.models import Download

    user, _created = user_model.objects.get_or_create(username=PROFILE_ID)
    user.set_password("integration-pass")
    user.save(update_fields=["password"])
    if not client.login(username=PROFILE_ID, password="integration-pass"):
        raise AssertionError("could not log in for podcast library action checks")

    played_download = downloads[0]
    response = client.post(f"/downloads/{played_download.id}/played/", follow=False)
    if response.status_code != 302:
        raise AssertionError(f"mark played returned {response.status_code}")
    played_download.refresh_from_db()
    if not played_download.played or played_download.played_at is None:
        raise AssertionError("podcast item was not marked played")
    pipeline._log_check(f"podcast item marked played: download_id={played_download.id}")

    _make_downloads_host_writable(compose, compose_env)

    deleted_download = downloads[1]
    deleted_media_path = pipeline._host_download_path(
        deleted_download.file_path, host_downloads_dir
    )
    deleted_download.file_path = str(deleted_media_path)
    deleted_download.save(update_fields=["file_path"])
    response = client.post(
        f"/downloads/{deleted_download.id}/delete-file/", follow=False
    )
    if response.status_code != 302:
        raise AssertionError(f"delete file returned {response.status_code}")
    deleted_download.refresh_from_db()
    if deleted_download.download_status != "missing":
        raise AssertionError(
            f"podcast delete did not mark item missing: {deleted_download.download_status}"
        )
    if deleted_media_path.exists():
        raise AssertionError(
            f"podcast delete did not remove media file: {deleted_media_path}"
        )
    pipeline._log_check(
        f"podcast item deleted media and marked missing: download_id={deleted_download.id}"
    )

    response = client.post(
        "/batch-update/",
        {"ids": [str(deleted_download.id)], "batch_action": "purge"},
        follow=False,
    )
    if response.status_code != 302:
        raise AssertionError(f"purge returned {response.status_code}")
    if Download.objects.filter(pk=deleted_download.pk).exists():
        raise AssertionError("podcast purge did not remove download row")
    pipeline._log_check(
        f"podcast item purged from database: download_id={deleted_download.id}"
    )


def main() -> int:
    timeout_seconds = int(
        os.getenv("GETOFFLINE_INTEGRATION_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    )
    compose = pipeline._compose_cmd()
    with tempfile.TemporaryDirectory(prefix="getoffline-podcast-integration-") as tmp:
        downloads_dir = Path(tmp) / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        reserved_ports: set[int] = set()
        frontend_port = pipeline._free_tcp_port(reserved_ports)
        api_port = pipeline._free_tcp_port(reserved_ports)
        mysql_port = pipeline._free_tcp_port(reserved_ports)
        rabbitmq_port = pipeline._free_tcp_port(reserved_ports)
        registry_port = pipeline._free_tcp_port(reserved_ports)
        project = f"getoffline-podcast-it-{uuid.uuid4().hex[:8]}"
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
                "GETOFFLINE_REGISTRY_PUBLISHED_PORT": str(registry_port),
                "GETOFFLINE_MYSQL_VOLUME_NAME": f"{project}_mysql-data",
                "GETOFFLINE_RABBITMQ_VOLUME_NAME": f"{project}_rabbitmq-data",
                "GETOFFLINE_REGISTRY_VOLUME_NAME": f"{project}_registry-data",
                "GETOFFLINE_DB_HOST": "mysql",
                "GETOFFLINE_DB_PORT": "3306",
                "GETOFFLINE_DB_NAME": "getoffline",
                "GETOFFLINE_DB_USER": "getoffline",
                "GETOFFLINE_DB_PASSWORD": "getoffline",
                "GETOFFLINE_RABBITMQ_URL": "amqp://guest:guest@rabbitmq:5672/%2F",
                "GETOFFLINE_RABBITMQ_EXCHANGE": "getoffline",
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
                "PYTHONPATH": str(pipeline.SRC),
            }
        )
        log_stream = None
        try:
            try:
                pipeline._run(
                    pipeline._compose_up_command(compose),
                    env=compose_env,
                    timeout=1800,
                    stream_output=True,
                )
            except AssertionError:
                pipeline._run(
                    [*compose, "ps"], env=compose_env, timeout=60, check=False
                )
                pipeline._run(
                    [*compose, "logs", "--tail", "200"],
                    env=compose_env,
                    timeout=120,
                    check=False,
                )
                raise
            log_stream = pipeline._start_log_stream(compose, compose_env)
            deadline = time.monotonic() + timeout_seconds
            pipeline._wait_for_frontend(deadline, frontend_port)
            pipeline._wait_for_api(deadline, api_port)
            pipeline._django_setup(host_env)
            job_id = _queue_episode_check()
            _wait_for_podcast_source(
                job_id, deadline, downloads_dir, compose, compose_env
            )
        finally:
            pipeline._stop_log_stream(log_stream)
            _make_downloads_host_writable(compose, compose_env)
            keep_stack = os.getenv("GETOFFLINE_INTEGRATION_KEEP_STACK", "0")
            if keep_stack.lower() not in {"1", "true", "yes"}:
                pipeline._run(
                    [*compose, "down", "-v", "--remove-orphans"],
                    env=compose_env,
                    timeout=600,
                    check=False,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
