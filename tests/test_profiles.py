import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from profiles import ProfileManager  # noqa: E402


class ProfileManagerTests(unittest.TestCase):
    def test_default_profile_reuses_existing_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_root = root / "downloads"
            database_path = output_root / "downloads.sqlite3"

            manager = ProfileManager(root / "profiles.json", output_root, database_path)

            profile = manager.get_active()
            self.assertEqual(profile.profile_id, "default")
            self.assertEqual(profile.name, "default")
            self.assertEqual(profile.output_root, output_root.resolve())
            self.assertEqual(profile.database_path, database_path.resolve())
            self.assertTrue(database_path.exists())

    def test_create_profile_has_isolated_database_and_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = ProfileManager(
                root / "profiles.json",
                root / "downloads",
                root / "downloads" / "downloads.sqlite3",
            )

            created = manager.create("Alice")
            config = manager.load_config(created)

            self.assertEqual(created.name, "Alice")
            self.assertNotEqual(created.database_path, manager.profiles["default"].database_path)
            self.assertTrue(created.database_path.exists())
            self.assertEqual(Path(config["defaults"]["output_root"]), created.output_root)
            self.assertEqual(config["youtube"], [])
            self.assertEqual(config["podcasts"], [])

    def test_switch_and_rename_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = root / "profiles.json"
            manager = ProfileManager(registry, root / "downloads", root / "downloads" / "downloads.sqlite3")
            created = manager.create("Alice")
            renamed = manager.rename_active("Family")

            restored = ProfileManager(registry, root / "downloads", root / "downloads" / "downloads.sqlite3")

            self.assertEqual(renamed.profile_id, created.profile_id)
            self.assertEqual(restored.get_active().name, "Family")
            restored.switch("default")
            self.assertEqual(restored.get_active().name, "default")

    def test_profile_names_are_unique_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = ProfileManager(root / "profiles.json", root / "downloads", root / "downloads.sqlite3")
            manager.create("Alice")

            with self.assertRaises(ValueError):
                manager.create("alice")


if __name__ == "__main__":
    unittest.main()
