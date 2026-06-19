import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import workers.utils as utils


class SanitizeChannelNameTests(unittest.TestCase):
    def test_sanitize_channel_name_removes_underscores(self):
        self.assertEqual(utils.sanitize_channel_name("Saturday_Night_Live"), "SaturdayNightLive")

    def test_sanitize_channel_name_falls_back_when_empty(self):
        self.assertEqual(utils.sanitize_channel_name("___"), "channel")


if __name__ == "__main__":
    unittest.main()
