import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


class MainCliTests(unittest.TestCase):
    def test_parse_args_defaults_port_8080(self):
        with patch.object(sys, "argv", ["getoffline"]):
            args = main.parse_args()

        self.assertIsNone(args.command)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8080)

    def test_main_without_args_runs_server(self):
        with patch.object(sys, "argv", ["getoffline"]), patch("main.run_server") as run_server, patch(
            "main.run_downloads"
        ) as run_downloads:
            main.main()

        run_server.assert_called_once_with(host="127.0.0.1", port=8080)
        run_downloads.assert_not_called()


if __name__ == "__main__":
    unittest.main()
