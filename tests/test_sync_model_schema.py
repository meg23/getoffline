import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("GETOFFLINE_TEST_IN_MEMORY_DB", "1")
os.environ.setdefault("GETOFFLINE_DB_NAME", ":memory:")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend.settings")

import django
from django.core.exceptions import ValidationError

django.setup()

from models.management.commands.sync_model_schema import (
    _needs_database_default_repair,
)
from models.models import Download


class SyncModelSchemaTests(unittest.TestCase):
    def test_profanity_status_has_application_and_database_defaults(self):
        field = Download._meta.get_field("profanity_status")

        self.assertEqual(field.get_default(), "clean")
        self.assertTrue(field.has_db_default())

    def test_missing_database_default_is_detected(self):
        field = Download._meta.get_field("profanity_status")

        self.assertTrue(
            _needs_database_default_repair(field, SimpleNamespace(default=None))
        )
        self.assertFalse(
            _needs_database_default_repair(field, SimpleNamespace(default="clean"))
        )

    def test_default_download_passes_model_validation(self):
        download = Download(
            profile_id="profile",
            source_type="youtube",
            source_name="source",
        )

        try:
            download.full_clean()
        except ValidationError as exc:  # pragma: no cover - assertion detail
            self.fail(f"Default Download should validate: {exc.message_dict}")
