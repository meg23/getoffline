import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from config import _build_bootstrap_defaults, load_bootstrap_config, load_config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def test_bootstrap_defaults_use_cwd_downloads_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            previous_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                defaults = _build_bootstrap_defaults()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(defaults["output_root"], os.path.join(tmpdir, "downloads"))
        self.assertEqual(defaults["database_path"], os.path.join(tmpdir, "downloads", "downloads.sqlite3"))

    def test_load_config_reads_paths_from_config_yml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yml"
            config_path.write_text(
                """
defaults:
  output_root: ./media
  database_path: ./state/library.sqlite3
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config["defaults"]["output_root"], os.path.join(tmpdir, "media"))
        self.assertEqual(config["defaults"]["database_path"], os.path.join(tmpdir, "state", "library.sqlite3"))

    def test_load_bootstrap_config_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.yml"
            database_path = Path(tmpdir) / "state" / "library.sqlite3"
            config_path.write_text(
                """
defaults:
  output_root: ./media
  database_path: ./state/library.sqlite3
""".strip()
                + "\n",
                encoding="utf-8",
            )

            config = load_bootstrap_config(config_path)

            self.assertEqual(config["defaults"]["database_path"], str(database_path))
            self.assertFalse(database_path.exists())


if __name__ == "__main__":
    unittest.main()
