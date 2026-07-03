import gc
import unittest
import warnings


warnings.simplefilter("ignore", ResourceWarning)


class DatabaseCleanupTestCase(unittest.TestCase):
    """TestCase base that keeps Django test database state isolated between tests."""

    def tearDown(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()
