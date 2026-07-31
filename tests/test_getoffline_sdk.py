import os
import sys
import unittest
import urllib.error
from email.message import Message
from io import BytesIO
from unittest.mock import patch

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
from packages.getoffline_sdk.transports import Response, _encoded_body


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

    def test_client_convenience_methods_forward_to_transport(self):
        class RecordingTransport:
            def __init__(self):
                self.calls = []

            def request(self, method, target, args=(), **kwargs):
                self.calls.append((method, target, args, kwargs))
                return Response(200, b'{"ok": true}')

        transport = RecordingTransport()
        client = GetOfflineClient(transport)
        self.assertEqual(client.frontend_library(filter_mode="played"), {"ok": True})
        self.assertEqual(client.frontend_jobs(), {"ok": True})
        self.assertEqual(client.frontend_player(3, start_seconds="4"), {"ok": True})
        self.assertEqual(client.search("term"), {"ok": True})
        self.assertEqual(client.library(), {"ok": True})
        self.assertEqual(client.history(), {"ok": True})
        self.assertEqual(client.user(), {"ok": True})
        self.assertEqual(client.csrf(), {"ok": True})
        self.assertEqual(client.download("https://example", media_type="audio"), {"ok": True})
        self.assertEqual(client.playback_start(3), {"ok": True})
        self.assertEqual(client.playback_progress(3, 2.5), {"ok": True})
        self.assertEqual(client.playback_complete(3, 5), {"ok": True})
        self.assertEqual(len(transport.calls), 12)

        transport.request = lambda *args, **kwargs: Response(500, b"error")
        self.assertEqual(client.json_request("GET", "/error"), {})
        transport.request = lambda *args, **kwargs: Response(200, b"[]")
        self.assertEqual(client.json_request("GET", "/list"), {})

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

    def test_http_transport_streams_media_without_reading_the_whole_response(self):
        class ChunkedResponse:
            status = 206
            headers = Message()

            def __init__(self):
                self.read_sizes = []
                self.closed = False
                self.chunks = iter((b"abc", b"def", b""))

            def read(self, size=-1):
                self.read_sizes.append(size)
                return next(self.chunks)

            def close(self):
                self.closed = True

        upstream = ChunkedResponse()

        class StreamingOpener:
            def open(self, *args, **kwargs):
                return upstream

        with patch(
            "urllib.request.build_opener", return_value=StreamingOpener()
        ):
            response = HttpTransport("http://api:8000/api").request(
                "GET",
                "api_stream",
                (7,),
                headers={"Range": "bytes=0-"},
                streaming=True,
            )

        self.assertTrue(response.streaming)
        self.assertEqual(response.content, b"")
        self.assertEqual(upstream.read_sizes, [])
        self.assertIsNotNone(response.streaming_content)
        self.assertEqual(b"".join(response.streaming_content or ()), b"abcdef")
        self.assertEqual(upstream.read_sizes, [65536, 65536, 65536])
        self.assertTrue(upstream.closed)

    def test_multipart_upload_body_is_lazy(self):
        class Upload:
            name = "large-video.mp4"
            content_type = "video/mp4"

            def __init__(self):
                self.chunks_calls = 0

            def chunks(self):
                self.chunks_calls += 1
                yield b"first-upload-chunk"
                yield b"second-upload-chunk"

        upload = Upload()
        body, content_type = _encoded_body(
            {"title": "Large video", "file": upload}
        )

        self.assertIn("multipart/form-data", content_type)
        self.assertNotIsInstance(body, bytes)
        self.assertEqual(upload.chunks_calls, 0)

        body_iterator = iter(body or ())
        parts = [next(body_iterator), next(body_iterator)]
        self.assertEqual(upload.chunks_calls, 0)
        parts.extend(body_iterator)

        self.assertEqual(upload.chunks_calls, 1)
        encoded = b"".join(parts)
        self.assertIn(b'filename="large-video.mp4"', encoded)
        self.assertIn(b"first-upload-chunk", encoded)
        self.assertIn(b"second-upload-chunk", encoded)

    def test_http_transport_sets_content_length_for_streamed_multipart_upload(self):
        class Upload:
            name = "notes.pdf"
            content_type = "application/pdf"
            size = 9

            def chunks(self):
                yield b"pdf-bytes"

        class Upstream:
            status = 201
            headers = Message()

            def read(self):
                return b"{}"

            def close(self):
                pass

        class Opener:
            def __init__(self):
                self.request = None

            def open(self, request, **kwargs):
                self.request = request
                return Upstream()

        opener = Opener()
        with patch("urllib.request.build_opener", return_value=opener):
            response = HttpTransport("http://api:8000/api").request(
                "POST", "api_dashboard_manual_upload", data={"files": [Upload()]}
            )

        self.assertEqual(response.status_code, 201)
        self.assertIsNotNone(opener.request)
        self.assertEqual(
            opener.request.get_header("Content-length"),
            "176",
        )


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
