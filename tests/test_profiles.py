import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from workers.download_store import (
    get_stored_config,
    init_database,
    update_stored_defaults,
)  # noqa: E402
from workers.profiles import ProfileManager  # noqa: E402
from support import DatabaseCleanupTestCase  # noqa: E402


class ProfileManagerTests(DatabaseCleanupTestCase):
    def test_default_profile_uses_profiles_directory_without_moving_existing_paths(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_root = root / "downloads"
            database_path = output_root / "downloads.sqlite3"
            output_root.mkdir()
            (output_root / "existing.mp3").write_bytes(b"media")

            manager = ProfileManager(root / "profiles.json", output_root, database_path)

            profile = manager.get_active()
            self.assertEqual(profile.profile_id, "default")
            self.assertEqual(profile.name, "default")
            self.assertEqual(
                profile.output_root, (root / "downloads" / "default").resolve()
            )
            self.assertEqual(
                profile.database_path,
                (root / "profiles" / "default" / "downloads.sqlite3").resolve(),
            )
            self.assertTrue(profile.database_path.exists())
            self.assertTrue((output_root / "existing.mp3").exists())
            self.assertFalse((profile.output_root / "existing.mp3").exists())

    def test_registered_default_profile_is_normalized_without_moving_existing_paths(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy_output = root / "legacy-media"
            legacy_output.mkdir()
            legacy_database = root / "legacy.sqlite3"
            registry = root / "profiles.json"
            registry.write_text(
                (
                    '{"active_profile_id":"default","profiles":[{'
                    '"id":"default","name":"Home","output_root":"'
                    f'{legacy_output}","database_path":"{legacy_database}"'
                    "}]}\n"
                ),
                encoding="utf-8",
            )

            manager = ProfileManager(registry, root / "unused", root / "unused.sqlite3")

            profile = manager.get_active()
            self.assertEqual(profile.name, "Home")
            self.assertEqual(
                profile.output_root, (root / "downloads" / "default").resolve()
            )
            self.assertEqual(
                profile.database_path,
                (root / "profiles" / "default" / "downloads.sqlite3").resolve(),
            )
            self.assertTrue(legacy_output.exists())

    def test_existing_profile_directories_are_discovered_without_a_registry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profiles_root = root / "profiles"
            for profile_id in ("default", "max", "ozzie"):
                profile_root = profiles_root / profile_id
                (root / "downloads" / profile_id).mkdir(parents=True)
                profile_root.mkdir(parents=True)
                (profile_root / "downloads.sqlite3").touch()
            (profiles_root / ".DS_Store").write_text("", encoding="utf-8")

            manager = ProfileManager(
                root / "profiles.json", root / "legacy", root / "legacy.sqlite3"
            )

            self.assertEqual(
                [profile.profile_id for profile in manager.list_profiles()],
                ["default", "max", "ozzie"],
            )
            self.assertEqual(
                manager.profiles["max"].output_root, root / "downloads" / "max"
            )
            self.assertEqual(
                manager.profiles["ozzie"].database_path,
                profiles_root / "ozzie" / "downloads.sqlite3",
            )

    def test_profile_directory_with_content_at_root_uses_profile_root_as_output_root(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_root = root / "profiles" / "max"
            channel_root = profile_root / "My Channel"
            channel_root.mkdir(parents=True)
            (channel_root / "episode.mp3").write_bytes(b"audio")
            (root / "downloads" / "max").mkdir(parents=True)
            (profile_root / "downloads.sqlite3").touch()

            manager = ProfileManager(
                root / "profiles.json", root / "legacy", root / "legacy.sqlite3"
            )

            self.assertEqual(manager.profiles["max"].output_root, profile_root)

    def test_registry_name_is_preserved_for_discovered_profile_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_root = root / "profiles" / "max"
            (root / "downloads" / "max").mkdir(parents=True)
            registry = root / "profiles.json"
            registry.write_text(
                (
                    '{"profiles":[{"id":"max","name":"Max",'
                    f'"output_root":"{profile_root / "downloads"}",'
                    f'"database_path":"{profile_root / "downloads.sqlite3"}"'
                    "}]}\n"
                ),
                encoding="utf-8",
            )

            manager = ProfileManager(registry, root / "legacy", root / "legacy.sqlite3")

            self.assertEqual(manager.profiles["max"].name, "Max")

    def test_registered_profile_paths_are_recomputed_from_content_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_root = root / "profiles" / "max"
            channel_root = profile_root / "XboxReady"
            channel_root.mkdir(parents=True)
            (channel_root / "episode.mp3").write_bytes(b"audio")
            (root / "downloads" / "max").mkdir(parents=True)
            database_path = profile_root / "downloads.sqlite3"
            database_path.touch()
            registry = root / "profiles.json"
            registry.write_text(
                (
                    '{"profiles":[{"id":"max","name":"Max",'
                    f'"output_root":"{profile_root / "downloads"}",'
                    f'"database_path":"{database_path}"'
                    "}]}\n"
                ),
                encoding="utf-8",
            )

            manager = ProfileManager(registry, root / "legacy", root / "legacy.sqlite3")

            self.assertEqual(manager.profiles["max"].name, "Max")
            self.assertEqual(manager.profiles["max"].output_root, profile_root)
            self.assertEqual(manager.profiles["max"].database_path, database_path)

    def test_discovered_profile_database_paths_are_updated_to_canonical_locations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile_root = root / "profiles" / "max"
            (root / "downloads" / "max").mkdir(parents=True)
            database_path = profile_root / "downloads.sqlite3"
            init_database(str(database_path))
            update_stored_defaults(
                str(database_path),
                {
                    "output_root": str(root / "old-max"),
                    "database_path": str(root / "old-max.sqlite3"),
                },
            )

            ProfileManager(
                root / "profiles.json", root / "legacy", root / "legacy.sqlite3"
            )

            defaults = get_stored_config(str(database_path))["defaults"]
            self.assertEqual(Path(defaults["output_root"]), root / "downloads" / "max")
            self.assertEqual(Path(defaults["database_path"]), database_path)

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
            self.assertNotEqual(
                created.database_path, manager.profiles["default"].database_path
            )
            self.assertTrue(created.database_path.exists())
            self.assertEqual(
                created.output_root, root / "downloads" / created.profile_id
            )
            self.assertEqual(
                Path(config["defaults"]["output_root"]), created.output_root
            )
            self.assertEqual(config["youtube"], [])
            self.assertEqual(config["podcasts"], [])

    def test_switch_and_rename_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = root / "profiles.json"
            manager = ProfileManager(
                registry, root / "downloads", root / "downloads" / "downloads.sqlite3"
            )
            created = manager.create("Alice")
            renamed = manager.rename_active("Family")

            restored = ProfileManager(
                registry, root / "downloads", root / "downloads" / "downloads.sqlite3"
            )

            self.assertEqual(renamed.profile_id, created.profile_id)
            self.assertEqual(restored.get_active().name, "Family")
            restored.switch("default")
            self.assertEqual(restored.get_active().name, "default")

    def test_profile_names_are_unique_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = ProfileManager(
                root / "profiles.json", root / "downloads", root / "downloads.sqlite3"
            )
            manager.create("Alice")

            with self.assertRaises(ValueError):
                manager.create("alice")

    def test_profile_pin_is_hashed_verified_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = root / "profiles.json"
            manager = ProfileManager(
                registry, root / "downloads", root / "downloads.sqlite3"
            )
            profile = manager.set_pin("default", "1234")

            self.assertTrue(profile.has_pin)
            self.assertNotIn("1234", registry.read_text(encoding="utf-8"))
            self.assertTrue(manager.verify_pin("default", "1234"))
            self.assertFalse(manager.verify_pin("default", "9999"))

            restored = ProfileManager(
                registry, root / "downloads", root / "downloads.sqlite3"
            )
            self.assertTrue(restored.get_active().has_pin)
            self.assertTrue(restored.verify_pin("default", "1234"))

    def test_blank_profile_pin_removes_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manager = ProfileManager(
                root / "profiles.json", root / "downloads", root / "downloads.sqlite3"
            )
            manager.set_pin("default", "1234")

            profile = manager.set_pin("default", "")

            self.assertFalse(profile.has_pin)
            self.assertTrue(manager.verify_pin("default", ""))


if __name__ == "__main__":
    unittest.main()
