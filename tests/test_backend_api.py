# ruff: noqa: E402
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

import django

django.setup()

from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from models.domain import DownloadStatus
from models.models import Download


class BackendApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        existing_tables = set(connection.introspection.table_names())
        with connection.schema_editor() as schema_editor:
            for model in apps.get_models():
                if model._meta.db_table not in existing_tables:
                    schema_editor.create_model(model)
                    existing_tables.add(model._meta.db_table)

    def setUp(self):
        for model in reversed(apps.get_models()):
            model.objects.all().delete()
        self.user = User.objects.create_user(username="api-user", password="pw")
        self.client = Client()
        self.client.force_login(self.user)

    def test_health_endpoint_is_public_for_container_readiness(self):
        response = Client(enforce_csrf_checks=True).get(reverse("api_health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "service": "api"})

    def test_library_returns_episode_json(self):
        Download.objects.create(
            profile_id="api-user",
            item_uid="ep-1",
            source_type="podcast",
            source_name="Feed",
            title="Episode One",
            description="A test episode",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

        response = self.client.get(reverse("api_library"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["episodes"][0]["title"], "Episode One")
        self.assertIn("stream_url", payload["episodes"][0])

    def _create_download(self) -> Download:
        return Download.objects.create(
            profile_id="api-user",
            item_uid="ep-2",
            source_type="podcast",
            source_name="Feed",
            title="Episode Two",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

    def test_frontend_player_falls_back_to_file_path_extension_for_video_kind(self):
        download = Download.objects.create(
            profile_id="api-user",
            item_uid="ep-video",
            source_type="manual",
            source_name="Imports",
            title="Imported Video",
            file_path="/media/imported-video.webm",
            file_ext="",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

        response = self.client.get(reverse("api_frontend_player", args=[download.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["media_kind"], "video")

    def test_playback_progress_updates_episode_state(self):
        download = self._create_download()

        response = self.client.post(
            reverse("api_playback_progress"),
            data={
                "episode_id": download.id,
                "position_seconds": "12.5",
                "reason": "timeupdate",
            },
        )

        self.assertEqual(response.status_code, 200)
        download.refresh_from_db()
        self.assertEqual(download.last_position_seconds, 12.5)
        self.assertFalse(download.played)

    def test_session_api_post_requires_csrf_when_checks_are_enforced(self):
        download = self._create_download()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)

        response = client.post(
            reverse("api_playback_progress"),
            data={"episode_id": download.id, "position_seconds": "12.5"},
        )

        self.assertEqual(response.status_code, 403)

    def test_session_api_post_accepts_explicit_csrf_token(self):
        download = self._create_download()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        token = client.get(reverse("api_csrf")).json()["csrf_token"]

        response = client.post(
            reverse("api_playback_progress"),
            data={"episode_id": download.id, "position_seconds": "12.5"},
            HTTP_X_CSRFTOKEN=token,
        )

        self.assertEqual(response.status_code, 200)
        download.refresh_from_db()
        self.assertEqual(download.last_position_seconds, 12.5)

    def test_basic_auth_api_post_does_not_require_csrf_token(self):
        download = self._create_download()
        client = Client(enforce_csrf_checks=True)
        credentials = base64.b64encode(b"api-user:pw").decode("ascii")

        response = client.post(
            reverse("api_playback_progress"),
            data={"episode_id": download.id, "position_seconds": "12.5"},
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )

        self.assertEqual(response.status_code, 200)
        download.refresh_from_db()
        self.assertEqual(download.last_position_seconds, 12.5)


if __name__ == "__main__":
    unittest.main()
