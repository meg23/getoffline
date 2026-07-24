"""Combined Docker Compose integration test for podcast and YouTube pipelines.

This starts the runtime stack once, runs the podcast RSS source scenario, then
runs the YouTube-shaped downloader scenario with the deterministic fake yt-dlp
module. `make integration-test` uses this entry point to avoid rebuilding and
starting a second Compose project for the two end-to-end checks.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

youtube = importlib.import_module("tests.integration.test_youtube_pipeline")
podcast = importlib.import_module("tests.integration.test_podcast_source_pipeline")

DEFAULT_TIMEOUT_SECONDS = 2400


def main() -> int:
    timeout_seconds = int(
        os.getenv("GETOFFLINE_INTEGRATION_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    )
    compose = youtube._compose_cmd()
    with tempfile.TemporaryDirectory(prefix="getoffline-integration-") as tmp:
        downloads_dir = Path(tmp) / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        reserved_ports: set[int] = set()
        frontend_port = youtube._free_tcp_port(reserved_ports)
        api_port = youtube._free_tcp_port(reserved_ports)
        mysql_port = youtube._free_tcp_port(reserved_ports)
        rabbitmq_port = youtube._free_tcp_port(reserved_ports)
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
                "DJANGO_SETTINGS_MODULE": "frontend.settings",
                "PYTHONPATH": str(youtube.SRC),
            }
        )
        log_stream = None
        try:
            try:
                youtube._run(
                    youtube._compose_up_command(compose),
                    env=compose_env,
                    timeout=1800,
                )
            except AssertionError:
                youtube._run([*compose, "ps"], env=compose_env, timeout=60, check=False)
                youtube._run(
                    [*compose, "logs", "--tail", "200"],
                    env=compose_env,
                    timeout=120,
                    check=False,
                )
                raise
            log_stream = youtube._start_log_stream(compose, compose_env)
            deadline = time.monotonic() + timeout_seconds
            youtube._wait_for_frontend(deadline, frontend_port)
            youtube._wait_for_api(deadline, api_port)
            youtube._django_setup(host_env)
            youtube._verify_profanity_model()

            podcast_job_id = podcast._queue_episode_check()
            podcast._wait_for_podcast_source(
                podcast_job_id, deadline, downloads_dir, compose, compose_env
            )

            youtube_job_id = youtube._queue_download_job()
            youtube._wait_for_pipeline(youtube_job_id, deadline, downloads_dir)
        finally:
            youtube._stop_log_stream(log_stream)
            youtube._make_downloads_host_writable(compose, compose_env, force=True)
            keep_stack = os.getenv("GETOFFLINE_INTEGRATION_KEEP_STACK", "0")
            if keep_stack.lower() not in {"1", "true", "yes"}:
                youtube._run(
                    [*compose, "down", "-v", "--remove-orphans"],
                    env=compose_env,
                    timeout=600,
                    check=False,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
