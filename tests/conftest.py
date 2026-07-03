"""Pytest configuration for reliable unit test isolation."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Unit tests should not depend on external MySQL/RabbitMQ services. Configure
# Django to use SQLite before any test module imports app.settings or calls
# django.setup().
os.environ.setdefault("GETOFFLINE_DB_ENGINE", "sqlite")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

try:
    import django
    from django.test.utils import setup_test_environment, teardown_test_environment
except ModuleNotFoundError:  # pragma: no cover - Django is optional for some tests
    django = None
else:
    django.setup()
    setup_test_environment()


def pytest_unconfigure(config):
    if django is not None:
        teardown_test_environment()
