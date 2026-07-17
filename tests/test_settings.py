import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class SettingsEnvironmentTests(unittest.TestCase):
    def test_default_allowed_hosts_accept_lan_browser_hosts(self):
        old_value = os.environ.pop("GETOFFLINE_DJANGO_ALLOWED_HOSTS", None)
        try:
            settings = importlib.import_module("app.settings")
            settings = importlib.reload(settings)
            self.assertIn("*", settings.ALLOWED_HOSTS)
        finally:
            if old_value is not None:
                os.environ["GETOFFLINE_DJANGO_ALLOWED_HOSTS"] = old_value
            importlib.reload(settings)

    def test_allowed_hosts_env_keeps_internal_api_hosts_available(self):
        old_value = os.environ.get("GETOFFLINE_DJANGO_ALLOWED_HOSTS")
        os.environ["GETOFFLINE_DJANGO_ALLOWED_HOSTS"] = "example.test"
        try:
            settings = importlib.import_module("app.settings")
            settings = importlib.reload(settings)
            self.assertIn("example.test", settings.ALLOWED_HOSTS)
            self.assertIn("api", settings.ALLOWED_HOSTS)
            self.assertIn("frontend", settings.ALLOWED_HOSTS)
            self.assertIn("localhost", settings.ALLOWED_HOSTS)
            self.assertNotIn("*", settings.ALLOWED_HOSTS)
        finally:
            if old_value is None:
                os.environ.pop("GETOFFLINE_DJANGO_ALLOWED_HOSTS", None)
            else:
                os.environ["GETOFFLINE_DJANGO_ALLOWED_HOSTS"] = old_value
            importlib.reload(settings)


if __name__ == "__main__":
    unittest.main()
