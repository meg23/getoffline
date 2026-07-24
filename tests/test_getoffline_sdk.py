# ruff: noqa: E402
import os
import sys
import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import patch
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")

import django

django.setup()

from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection
from django.http import StreamingHttpResponse
from django.test import Client
from django.utils import timezone

from models.domain import DownloadStatus
from models.models import Download
from packages.getoffline_sdk import DjangoTransport, GetOfflineClient, HttpTransport
from packages.getoffline_sdk.transports import Response


class GetOfflineSdkTests(unittest.TestCase):
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
        self.user = User.objects.create_user(username="sdk-user", password="pw")
        django_client = Client()
        django_client.force_login(self.user)
        self.sdk = GetOfflineClient(DjangoTransport(django_client))

    def test_sdk_reads_library(self):
        Download.objects.create(
            profile_id="sdk-user",
            item_uid="sdk-ep-1",
            source_type="podcast",
            source_name="SDK Feed",
            title="SDK Episode",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

        payload = self.sdk.library()

        self.assertEqual(payload["episodes"][0]["title"], "SDK Episode")

    def test_sdk_updates_playback_progress(self):
        download = Download.objects.create(
            profile_id="sdk-user",
            item_uid="sdk-ep-2",
            source_type="podcast",
            source_name="SDK Feed",
            title="Progress Episode",
            download_status=DownloadStatus.DOWNLOADED,
            last_seen_at=timezone.now(),
        )

        payload = self.sdk.playback_progress(download.id, 42.25, reason="sdk-test")

        self.assertTrue(payload["ok"])
        download.refresh_from_db()
        self.assertEqual(download.last_position_seconds, 42.25)

    def test_raw_request_exposes_response_for_streaming_proxy(self):
        response = self.sdk.raw_request("GET", "api_health")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"service": "api"', response.content)

    def test_http_transport_preserves_redirects_for_frontend_proxy(self):
        headers = Message()
        headers["Location"] = "/"
        error = urllib.error.HTTPError(
            "http://api:8000/api/dashboard/batch-update",
            302,
            "Found",
            headers,
            BytesIO(b""),
        )

        class RedirectingOpener:
            def open(self, *args, **kwargs):
                raise error

        with patch(
            "urllib.request.build_opener", return_value=RedirectingOpener()
        ) as build_opener:
            response = HttpTransport("http://api:8000/api").request(
                "POST",
                "/dashboard/batch-update",
                data={"next": "/"},
            )

        build_opener.assert_called_once()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")


class StreamingDjangoTransportTests(unittest.TestCase):
    def test_transport_reads_streaming_response_content(self):
        class StreamingClient:
            def get(self, *args, **kwargs):
                response = StreamingHttpResponse([b"abc", b"def"], status=206)
                response["Content-Range"] = "bytes 0-5/6"
                return response

        response = DjangoTransport(StreamingClient()).request("GET", "api_health")

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"abcdef")
        self.assertEqual(response.headers["Content-Range"], "bytes 0-5/6")
        self.assertTrue(response.streaming)


if __name__ == "__main__":
    unittest.main()
