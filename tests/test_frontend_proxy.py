# ruff: noqa: E402
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

import django

django.setup()

from django.test import RequestFactory

from app.views import _request_headers, _upstream_response


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

    def test_proxy_forwards_browser_host_to_api(self):
        request = RequestFactory().get(
            "/settings/",
            HTTP_HOST="192.168.86.26:8080",
            HTTP_COOKIE="sessionid=abc",
        )

        headers = _request_headers(request)

        self.assertEqual(headers["Host"], "192.168.86.26:8080")
        self.assertEqual(headers["Cookie"], "sessionid=abc")


if __name__ == "__main__":
    unittest.main()
