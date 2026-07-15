import os
from unittest.mock import patch

from django.conf import settings

from app import settings as app_settings
from django.test import Client, SimpleTestCase, override_settings


class DjangoHostDefaultsTests(SimpleTestCase):
    def test_default_allowed_hosts_accepts_lan_addresses(self):
        self.assertIn("*", settings.ALLOWED_HOSTS)

    def test_internal_api_host_is_included_with_existing_strict_env(self):
        with patch.dict(
            os.environ,
            {"GETOFFLINE_DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1"},
        ):
            allowed_hosts = app_settings._allowed_hosts()

        self.assertIn("api", allowed_hosts)
        self.assertIn("frontend", allowed_hosts)

    @override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1"])
    def test_private_lan_host_is_allowed_even_with_existing_strict_env(self):
        response = Client().get("/settings/", HTTP_HOST="192.168.86.26:8080")

        self.assertEqual(response.status_code, 302)
        self.assertIn("192.168.86.26", settings.ALLOWED_HOSTS)

    @override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1"])
    def test_public_ip_host_still_uses_django_host_validation(self):
        response = Client().get("/settings/", HTTP_HOST="8.8.8.8:8080")

        self.assertEqual(response.status_code, 400)
