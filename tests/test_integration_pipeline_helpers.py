import tempfile
import unittest
from pathlib import Path

import yaml

from tests.integration import test_youtube_pipeline as pipeline


class IntegrationPipelineHelperTests(unittest.TestCase):
    def test_compose_up_command_scales_every_service_to_one(self):
        command = pipeline._compose_up_command(["docker", "compose"])

        self.assertEqual(command[:4], ["docker", "compose", "up", "-d"])
        self.assertIn("--build", command)
        for service in pipeline.COMPOSE_SERVICES:
            with self.subTest(service=service):
                self.assertIn("--scale", command)
                self.assertIn(f"{service}=1", command)

    def test_api_compose_command_expands_worker_defaults_in_container_shell(self):
        compose = yaml.safe_load((pipeline.ROOT / "docker-compose.yml").read_text())
        command = compose["services"]["api"]["command"]

        self.assertIn("sh -c", command)
        self.assertIn("GETOFFLINE_API_GUNICORN_WORKERS:-3", command)
        self.assertIn("GETOFFLINE_GUNICORN_TIMEOUT:-300", command)

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
