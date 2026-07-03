"""Pytest configuration for reliable unit test isolation."""

import os
import sys
from pathlib import Path

import django
from django.test.utils import setup_test_environment
from django.test.utils import teardown_test_environment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Unit tests should not depend on external MySQL/RabbitMQ services. Configure
# Django to use its in-memory test database before any test module imports
# app.settings or calls django.setup().
os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")

django.setup()
setup_test_environment()


def pytest_unconfigure(config):
    teardown_test_environment()
