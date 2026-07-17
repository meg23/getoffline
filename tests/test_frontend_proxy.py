# ruff: noqa: E402
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

import django

django.setup()

from app.views import _upstream_response


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


if __name__ == "__main__":
    unittest.main()
