from django.conf import settings
from django.test import SimpleTestCase


class DjangoHostDefaultsTests(SimpleTestCase):
    def test_default_allowed_hosts_accepts_lan_addresses(self):
        self.assertIn("*", settings.ALLOWED_HOSTS)
