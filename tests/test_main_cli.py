import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import workers.main as main  # noqa: E402


class MainCliTests(unittest.TestCase):
    def test_parse_args_defaults_port_8080(self):
        with patch.object(sys, "argv", ["getoffline"]):
            args = main.parse_args()

        self.assertIsNone(args.command)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8080)

    def test_main_without_args_runs_server(self):
        with patch.object(sys, "argv", ["getoffline"]), patch("workers.main.run_server") as run_server, patch(
            "workers.main.run_downloads"
        ) as run_downloads:
            main.main()

        run_server.assert_called_once_with(host="127.0.0.1", port=8080)
        run_downloads.assert_not_called()

    def test_run_server_delegates_to_django_app(self):
        with patch("workers.main._execute_django_runserver") as execute:
            main.run_server(host="127.0.0.1", port=8080)

        self.assertEqual(os.environ["GETOFFLINE_APP_ADDR"], "127.0.0.1:8080")
        execute.assert_called_once_with("127.0.0.1:8080")

    def test_parse_import_directory_arguments(self):
        with patch.object(sys, "argv", ["getoffline", "import-directory", "/media/incoming", "--recursive"]):
            args = main.parse_args()

        self.assertEqual(args.command, "import-directory")
        self.assertEqual(args.directory, "/media/incoming")
        self.assertTrue(args.recursive)

    def test_main_runs_directory_import_and_returns_its_status(self):
        with patch.object(sys, "argv", ["getoffline", "import-directory", "/media/incoming"]), patch(
            "workers.main.run_directory_import", return_value=3
        ) as run_directory_import:
            with self.assertRaises(SystemExit) as exit_context:
                main.main()

        self.assertEqual(exit_context.exception.code, 3)
        run_directory_import.assert_called_once_with("/media/incoming", recursive=False)

    def test_directory_video_files_filters_types_recursion_and_destination(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "one.mp4").write_bytes(b"one")
            (root / "notes.txt").write_text("notes", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "two.mkv").write_bytes(b"two")
            destination = root / "manual"
            destination.mkdir()
            (destination / "already.webm").write_bytes(b"existing")

            shallow = main._directory_video_files(root, recursive=False, excluded_root=destination)
            recursive = main._directory_video_files(root, recursive=True, excluded_root=destination)

            self.assertEqual(shallow, [(root / "one.mp4").resolve()])
            self.assertEqual(recursive, [(nested / "two.mkv").resolve(), (root / "one.mp4").resolve()])

    def test_run_directory_import_uses_manual_import_workflow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "incoming"
            source.mkdir()
            video = source / "movie.mp4"
            video.write_bytes(b"video")
            output_root = root / "output"
            database_path = output_root / "downloads.sqlite3"
            config = {
                "defaults": {
                    "output_root": str(output_root),
                    "database_path": str(database_path),
                }
            }
            destination = output_root / "manual" / "movie.mp4"

            with patch("workers.main.load_config", return_value=config), patch(
                "workers.main.import_local_media_file", return_value=destination
            ) as import_local_media_file:
                result = main.run_directory_import(str(source))

            self.assertEqual(result, 0)
            import_local_media_file.assert_called_once()
            state, imported_path = import_local_media_file.call_args.args
            self.assertEqual(imported_path, video.resolve())
            self.assertEqual(state.output_root, output_root.resolve())
            self.assertEqual(state.database_path, database_path.resolve())


if __name__ == "__main__":
    unittest.main()
