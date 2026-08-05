import inspect
import tempfile
import unittest
from pathlib import Path

import yaml

from tests.integration import test_youtube_pipeline as pipeline


class IntegrationPipelineHelperTests(unittest.TestCase):
    def test_compose_up_command_scales_every_service_to_one(self):
        command = pipeline._compose_up_command(
            [
                "docker",
                "compose",
                "-f",
                "stacks/docker-compose.yml",
                "-f",
                "stacks/docker-compose.build.yml",
            ]
        )

        self.assertEqual(
            command[:6],
            [
                "docker",
                "compose",
                "-f",
                "stacks/docker-compose.yml",
                "-f",
                "stacks/docker-compose.build.yml",
            ],
        )
        self.assertIn("up", command)
        self.assertIn("-d", command)
        self.assertIn("--build", command)
        for service in ("registry", *pipeline.COMPOSE_SERVICES):
            with self.subTest(service=service):
                self.assertIn("--scale", command)
                self.assertIn(f"{service}=1", command)

    def test_original_compose_up_command_does_not_scale_registry(self):
        command = pipeline._compose_up_command(
            ["docker", "compose", "-f", "docker-compose.yml"]
        )

        self.assertNotIn("registry=1", command)
        for service in pipeline.COMPOSE_SERVICES:
            self.assertIn(f"{service}=1", command)

    def test_run_can_stream_compose_output(self):
        self.assertIn("stream_output", inspect.signature(pipeline._run).parameters)

    def test_api_compose_command_uses_api_startup_entrypoint(self):
        compose = yaml.safe_load(
            (pipeline.ROOT / "stacks/docker-compose.yml").read_text()
        )
        command = compose["services"]["api"]["command"]

        self.assertEqual(command, "api-entrypoint.sh")
        entrypoint = (pipeline.ROOT / "deploy/docker/api-entrypoint.sh").read_text()
        self.assertIn("migrate --run-syncdb", entrypoint)
        self.assertIn("sync_model_schema", entrypoint)
        self.assertIn("GETOFFLINE_API_GUNICORN_WORKERS:-3", entrypoint)
        self.assertIn("GETOFFLINE_GUNICORN_TIMEOUT:-300", entrypoint)

    def test_api_healthcheck_uses_public_health_endpoint(self):
        compose = yaml.safe_load(
            (pipeline.ROOT / "stacks/docker-compose.yml").read_text()
        )
        healthcheck = compose["services"]["api"]["healthcheck"]["test"]

        self.assertIn("http://localhost:8000/api/health", healthcheck[-1])

    def test_runtime_services_use_the_in_cluster_registry(self):
        compose = yaml.safe_load(
            (pipeline.ROOT / "stacks/docker-compose.yml").read_text()
        )

        self.assertEqual(compose["services"]["registry"]["image"], "registry:2")
        self.assertNotIn("build", compose["services"]["frontend"])
        self.assertNotIn("build", compose["services"]["api"])
        self.assertIn("/getoffline/app:", compose["services"]["frontend"]["image"])
        self.assertIn(
            "/getoffline/worker-ffmpeg:",
            compose["services"]["worker-ffmpeg"]["image"],
        )
        self.assertEqual(
            compose["volumes"]["mysql-data"]["name"],
            "${GETOFFLINE_MYSQL_VOLUME_NAME:-getoffline_mysql-data}",
        )
        self.assertEqual(
            compose["volumes"]["rabbitmq-data"]["name"],
            "${GETOFFLINE_RABBITMQ_VOLUME_NAME:-getoffline_rabbitmq-data}",
        )

    def test_host_download_path_maps_container_downloads_to_host_mount(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            host_root = Path(tmpdir)
            mapped = pipeline._host_download_path(
                "/app/downloads/integration/source/item.converted.mp3",
                host_root,
            )

        self.assertEqual(
            mapped,
            host_root / "integration" / "source" / "item.converted.mp3",
        )

    def test_host_download_path_leaves_external_absolute_path_unchanged(self):
        host_root = Path("/tmp/host-downloads")
        external = "/var/lib/getoffline/item.mp3"

        self.assertEqual(
            pipeline._host_download_path(external, host_root),
            Path(external),
        )


if __name__ == "__main__":
    unittest.main()
