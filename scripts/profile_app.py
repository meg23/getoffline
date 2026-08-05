"""Profile the main GetOffline runtime paths with Scalene.

The suite uses an in-memory Django database and deterministic local inputs. It
does not contact YouTube, RabbitMQ, MySQL, or an external API. Use ``all`` for
a broad profile, or select one subsystem when investigating a hotspot.

Example:

    scalene run --profile-all scripts/profile_app.py --scenario all --iterations 10
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for import_path in (str(SRC), str(SCRIPTS)):
    if import_path not in sys.path:
        sys.path.insert(0, import_path)

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")
os.environ.setdefault("GETOFFLINE_LOG_FILE", "/tmp/getoffline-scalene.log")


def _setup_django(record_count: int) -> tuple[Any, int]:
    import django

    django.setup()
    from django.apps import apps
    from django.contrib.auth.models import User
    from django.db import connection
    from django.test import Client
    from django.utils import timezone

    from models.domain import DownloadStatus, JobStatus
    from models.models import Download, Job

    existing_tables = set(connection.introspection.table_names())
    with connection.schema_editor() as schema_editor:
        for model in apps.get_models():
            if model._meta.db_table not in existing_tables:
                schema_editor.create_model(model)
                existing_tables.add(model._meta.db_table)

    user = User.objects.create_user(username="scalene", password="scalene")
    now = timezone.now()
    Download.objects.bulk_create(
        [
            Download(
                profile_id="scalene",
                source_type="youtube" if index % 2 else "podcast",
                source_name=f"Performance source {index % 10}",
                item_uid=f"performance-{index}",
                title=f"Performance Episode {index}",
                description="Synthetic benchmark episode " * 4,
                duration_seconds=120 + index,
                file_path=f"/tmp/performance-{index}.mp4",
                file_ext="mp4",
                file_size_bytes=1024 * 1024 * (index + 1),
                download_status=DownloadStatus.DOWNLOADED,
                last_seen_at=now - timedelta(seconds=index),
                played=index % 3 == 0,
                favorite=index % 5 == 0,
                last_position_seconds=float(index % 90),
                total_listened_seconds=float(index * 4),
            )
            for index in range(record_count)
        ]
    )
    Job.objects.bulk_create(
        [
            Job(
                profile_id="scalene",
                job_type="download_single",
                status=JobStatus.SUCCEEDED,
                payload={"source_type": "youtube", "index": index},
                created_at=now - timedelta(seconds=index),
                updated_at=now - timedelta(seconds=index),
            )
            for index in range(max(record_count // 10, 10))
        ]
    )

    client = Client()
    client.force_login(user)
    return client, Download.objects.order_by("id").first().id


def _assert_success(response: Any) -> None:
    status_code = int(getattr(response, "status_code", 500))
    if status_code >= 400:
        raise RuntimeError(f"benchmark request failed with HTTP {status_code}")


def _profile_api(client: Any, episode_id: int, iterations: int) -> None:
    for _ in range(iterations):
        for path in (
            "/api/frontend/library?filter=all",
            "/api/frontend/jobs",
            "/api/search?q=Performance",
            f"/api/frontend/player/{episode_id}",
        ):
            response = client.get(path)
            _assert_success(response)
            _ = response.content


def _profile_frontend(client: Any, iterations: int) -> None:
    for _ in range(iterations):
        for path in ("/", "/jobs/", "/settings/"):
            response = client.get(path)
            _assert_success(response)
            _ = response.content


def _profile_sdk(client: Any, episode_id: int, iterations: int) -> None:
    from packages.getoffline_sdk import DjangoTransport, GetOfflineClient

    sdk = GetOfflineClient(DjangoTransport(client))
    for _ in range(iterations):
        sdk.frontend_library(filter_mode="all")
        sdk.frontend_jobs()
        sdk.search("Performance")
        sdk.frontend_player(episode_id)


def _profile_media(iterations: int, media_mb: int) -> None:
    from api.streaming.media import media_response

    with tempfile.NamedTemporaryFile(suffix=".mp4") as media_file:
        media_file.truncate(media_mb * 1024 * 1024)
        media_path = Path(media_file.name)
        for _ in range(iterations):
            response = media_response(media_path, "bytes=0-")
            total = sum(len(chunk) for chunk in response.streaming_content)
            response.close()
            if total != media_mb * 1024 * 1024:
                raise AssertionError("media benchmark did not consume the full range")


def _profile_multipart(iterations: int, upload_mb: int) -> None:
    from profile_multipart_upload import SyntheticUploadedFile

    from packages.getoffline_sdk.transports import _encoded_body

    for _ in range(iterations):
        upload = SyntheticUploadedFile(
            name="scalene-video.mp4",
            content_type="video/mp4",
            size_bytes=upload_mb * 1024 * 1024,
            chunk_size=1024 * 1024,
        )
        body, _ = _encoded_body({"title": "Scalene video", "file": upload})
        if body is None or isinstance(body, bytes):
            raise AssertionError("multipart benchmark received an eager body")
        sum(len(part) for part in body)


def _profile_workers(iterations: int, record_count: int) -> None:
    from frontend.queue import job_priority
    from frontend.routing import queue_name
    from workers.utils import (
        sanitize_channel_name,
        split_title_filter_terms,
        title_matches_filter,
    )
    from workers.ytdlp_helpers import clean_log_title, extract_youtube_video_id

    payloads: tuple[dict[str, object], ...] = (
        {"source_type": "youtube", "media_type": "video"},
        {"source_type": "podcast", "media_type": "audio"},
        {"media_type": "audio"},
    )
    sanitize = cast(Any, sanitize_channel_name)
    for index in range(iterations * max(record_count, 1)):
        payload = payloads[index % len(payloads)]
        queue_name("download_single", payload)
        job_priority({"job_type": "download_single", "payload": payload})
        title = f"Episode {index}: A noisy / channel name"
        terms = split_title_filter_terms("noisy, channel")
        title_matches_filter(title, terms)
        sanitize(title)
        clean_log_title(title)
        extract_youtube_video_id(f"https://www.youtube.com/watch?v=video{index}")


def _profile_cli(iterations: int, record_count: int) -> None:
    from cli.app import (
        default_media_extension,
        format_jobs,
        media_download_path,
        safe_filename,
    )

    item = {"title": "A performance video", "media_kind": "video"}
    jobs = [
        {"id": index, "job_type": "download_single", "status": "succeeded"}
        for index in range(max(record_count, 1))
    ]
    for index in range(iterations * max(record_count, 1)):
        safe_filename(f"Episode {index}: a title with / punctuation")
        default_media_extension(item)
        media_download_path(index, item, download_dir=Path("/tmp/scalene"))
        format_jobs(jobs)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("all", "api", "frontend", "sdk", "media", "multipart", "workers", "cli"),
        default="all",
        help="Subsystem to profile (default: all).",
    )
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--media-mb", type=int, default=8)
    parser.add_argument("--upload-mb", type=int, default=64)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if min(args.iterations, args.records, args.media_mb, args.upload_mb) <= 0:
        raise SystemExit("benchmark sizes and iterations must be positive")

    client, episode_id = _setup_django(args.records)
    scenarios: dict[str, Callable[[], None]] = {
        "api": lambda: _profile_api(client, episode_id, args.iterations),
        "frontend": lambda: _profile_frontend(client, args.iterations),
        "sdk": lambda: _profile_sdk(client, episode_id, args.iterations),
        "media": lambda: _profile_media(args.iterations, args.media_mb),
        "multipart": lambda: _profile_multipart(args.iterations, args.upload_mb),
        "workers": lambda: _profile_workers(args.iterations, args.records),
        "cli": lambda: _profile_cli(args.iterations, args.records),
    }
    selected = tuple(scenarios) if args.scenario == "all" else (args.scenario,)
    for name in selected:
        started = time.perf_counter()
        scenarios[name]()
        elapsed = time.perf_counter() - started
        print(f"{name}: {elapsed:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
