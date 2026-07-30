import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")

import django

django.setup()

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory

from frontend.views import _api_proxy, _request_headers, _upstream_response
from packages.getoffline_sdk import Response


class FrontendProxyTests(unittest.TestCase):
    def test_upstream_set_cookie_is_forwarded_to_browser_response(self):
        response = _upstream_response(
            200,
            {"Content-Type": "text/html"},
            b"ok",
            cookies=("csrftoken=abc123; Path=/; SameSite=Lax",),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.cookies["csrftoken"].value, "abc123")
        self.assertEqual(response.cookies["csrftoken"]["path"], "/")
        self.assertEqual(response.cookies["csrftoken"]["samesite"], "Lax")

    def test_upstream_response_preserves_streaming_body(self):
        response = _upstream_response(
            206,
            {
                "Content-Type": "video/mp4",
                "Content-Length": "4",
                "Content-Range": "bytes 0-3/4",
                "Content-Security-Policy": "default-src 'none'",
                "X-Frame-Options": "SAMEORIGIN",
            },
            b"",
            streaming=True,
            streaming_content=iter((b"ab", b"cd")),
        )

        self.assertTrue(response.streaming)
        self.assertEqual(b"".join(response.streaming_content), b"abcd")
        self.assertEqual(response["Content-Range"], "bytes 0-3/4")
        self.assertEqual(response["Content-Security-Policy"], "default-src 'none'")
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_media_proxy_requests_a_streaming_api_response(self):
        class RecordingClient:
            def __init__(self):
                self.kwargs = None

            def raw_request(self, *args, **kwargs):
                self.kwargs = kwargs
                return Response(
                    206,
                    b"",
                    {"Content-Type": "video/mp4"},
                    streaming=True,
                    streaming_content=iter((b"media",)),
                )

        client = RecordingClient()
        request = RequestFactory().get("/media/7", HTTP_RANGE="bytes=0-")

        with patch("frontend.views._sdk_client", return_value=client):
            response = _api_proxy(request, "api_stream", 7)

        self.assertTrue(client.kwargs["streaming"])
        self.assertEqual(b"".join(response.streaming_content), b"media")

    def test_proxy_forwards_browser_host_to_api(self):
        request = RequestFactory().get(
            "/settings/",
            HTTP_HOST="192.168.86.26:8080",
            HTTP_COOKIE="sessionid=abc",
        )

        headers = _request_headers(request)

        self.assertEqual(headers["Host"], "192.168.86.26:8080")
        self.assertEqual(headers["Cookie"], "sessionid=abc")

    def test_proxy_preserves_uploaded_files_for_multipart_forwarding(self):
        request = RequestFactory().post(
            "/manual-upload/",
            {
                "files": SimpleUploadedFile(
                    "notes.pdf", b"pdf-bytes", content_type="application/pdf"
                )
            },
        )

        class RecordingClient:
            def __init__(self):
                self.kwargs = None

            def raw_request(self, *args, **kwargs):
                self.kwargs = kwargs
                return Response(201, b'{"ok": true}', {"Content-Type": "application/json"})

        client = RecordingClient()
        with patch("frontend.views._sdk_client", return_value=client):
            response = _api_proxy(request, "api_dashboard_manual_upload")

        self.assertEqual(response.status_code, 201)
        uploaded = client.kwargs["data"]["files"]
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0].name, "notes.pdf")
        self.assertEqual(uploaded[0].content_type, "application/pdf")


if __name__ == "__main__":
    unittest.main()
